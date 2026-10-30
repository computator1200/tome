"""DB-driven processing pipeline: classify → OCR/vision → translate.

Unlike the old version, all progress is written to the Job row in the database,
so a page refresh (or a process restart) never loses it.
"""
from __future__ import annotations

import asyncio
import time
import traceback

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.database import SessionLocal
from db.models import (
    EngineMode,
    GlossaryTerm,
    Job,
    JobStatus,
    Page,
    PageType,
    Region,
    Series,
    Volume,
    VolumeKind,
    VolumeStatus,
)
from logging_config import get_logger
from services import language, llm, memory as memgraph, provider
from services.page_classifier import classify_page
from storage_backend import get_store

log = get_logger("pipeline")

_TEXT_TYPES = (PageType.TEXT.value, PageType.MIXED.value)

# Volume ids with a pending cancel request. The pipeline checks this at safe
# checkpoints (between pages/batches) and stops cleanly rather than mid-write.
_cancel_requested: set[str] = set()


class _Cancelled(Exception):
    """Raised internally to unwind the pipeline when a stop was requested."""


def request_cancel(volume_id: str) -> None:
    _cancel_requested.add(volume_id)


def _check_cancel(volume_id: str) -> None:
    if volume_id in _cancel_requested:
        raise _Cancelled()


def _unexpected(volume_id: str, results: list, weights: list[int] | None = None) -> int:
    """Failures that escaped a task's own handling, as a count of units.

    gather(return_exceptions=True) keeps one bad task from cancelling its
    siblings, but it also hides whatever that task raised. Anything other than a
    stop request is a failed unit and is counted, never silently dropped.
    """
    failed = 0
    for i, res in enumerate(results):
        if isinstance(res, BaseException) and not isinstance(res, _Cancelled):
            log.error(f"[{volume_id}] unexpected task failure: {res!r}")
            failed += weights[i] if weights else 1
    return failed


def _concurrency() -> int:
    """Max in-flight LLM requests per stage (configurable via Settings)."""
    return provider.get()["concurrency"]


async def _set_job(session: AsyncSession, job: Job, *, step=None, current=None,
                   total=None, message=None, status=None, error=None) -> None:
    if step is not None:
        job.step = step
    if current is not None:
        job.current = current
    if total is not None:
        job.total = total
    if message is not None:
        job.message = message
    if status is not None:
        job.status = status
    if error is not None:
        job.error = error
    await session.commit()


def _series_ctx(series: Series) -> dict:
    return {
        "title": series.title,
        "author": series.author,
        "genres": series.genres or [],
        "associated_names": series.associated_names or [],
        "description": series.description or "",
        "source_language": language.resolve(
            series.source_language, getattr(series, "detected_language", "")
        ) or "auto",
        "target_language": series.target_language or "English",
    }


async def _prompt_parts(series_id: str, series_ctx: dict, volume_order: int, page: int,
                        texts: list[str] | None) -> tuple[dict, list[dict]]:
    """The memory block and glossary for one model call, scoped to its position.

    Built per call, not per volume: what earlier pages of this run taught is
    already in it, nothing from later pages or volumes is, and the names that
    occur in `texts` come first. Its own session, so concurrent calls can read.
    """
    async with SessionLocal() as s:
        ctx = await memgraph.build_context(s, series_id, volume_order, page, texts)
        glossary = await memgraph.glossary_for(s, series_id, volume_order, page, texts)
    return {**series_ctx, "memory": memgraph.render_context(ctx)}, glossary



async def _maybe_detect_language(session: AsyncSession, series: Series,
                                 texts: list[str]) -> None:
    """Record the language the pages are actually written in.

    Only runs when the series has not declared one, and only once: the value is
    stored on the series so later pages skip detection entirely.
    """
    if not language.is_auto(series.source_language):
        return
    if (getattr(series, "detected_language", "") or "").strip():
        return
    detected = language.detect_language(" ".join(t for t in texts if t))
    if detected:
        series.detected_language = detected
        log.info(f"detected source language for series {series.id}: {detected}")

async def _merge_new_terms(session: AsyncSession, series: Series, new_terms: dict,
                           volume: Volume | None = None, memory_payload: dict | None = None,
                           page_id: str | None = None, page: int = memgraph.END) -> None:
    """Remember what this page taught us: new renderings, and the world graph.

    The glossary keeps names consistent; the graph keeps *knowledge* consistent,
    and is what lets a later volume read a relation the earlier one did not
    establish yet. Both are stamped with this page's position, so neither can
    reach a prompt for an earlier one. A failure here must never fail the
    translation: the graph is written in its own session, so a bad payload rolls
    back only itself.
    """
    series_id = series.id
    pos = (max(1, volume.order) if volume is not None else memgraph.HIDDEN, page)
    renderings: dict[str, str] = {}
    if isinstance(new_terms, dict):
        existing = {t.jp: t for t in series.glossary}
        for kind, key in (("character", "characters"), ("term", "terms")):
            items = new_terms.get(key)
            if not isinstance(items, dict):
                continue
            for jp, en in items.items():
                if not (isinstance(jp, str) and isinstance(en, str) and jp and en):
                    continue
                renderings[jp] = en
                term = existing.get(jp)
                if term is None:
                    term = GlossaryTerm(series_id=series_id, kind=kind, jp=jp, en=en,
                                        first_volume=pos[0], first_page=pos[1])
                    session.add(term)
                    series.glossary.append(term)
                    existing[jp] = term
                elif term.first_volume == memgraph.UNKNOWN or pos < (term.first_volume,
                                                                     term.first_page):
                    term.first_volume, term.first_page = pos
        await session.commit()

    if volume is None or not (memory_payload or renderings):
        return
    volume_id = volume.id
    async with SessionLocal() as ms:
        try:
            added = await memgraph.record_extraction(
                ms, series_id, pos[0], memory_payload, page_id, page=pos[1])
            await memgraph.record_renderings(ms, series_id, renderings)
            await ms.commit()
            if added["entities"] or added["edges"]:
                log.info(f"[{volume_id}] memory +{added['entities']} entities "
                         f"+{added['edges']} edges (vol {pos[0]}, page {pos[1]})")
        except Exception as exc:  # noqa: BLE001 - memory is a side effect, not the job
            await ms.rollback()
            log.warning(f"[{volume_id}] memory extraction skipped: {exc}")


async def _previous_summaries(session: AsyncSession, series_id: str, order: int) -> str:
    """Concatenated summaries of earlier volumes in this series (by order)."""
    rows = (await session.execute(
        select(Volume.label, Volume.summary)
        .where(Volume.series_id == series_id, Volume.order < order, Volume.summary != "")
        .order_by(Volume.order)
    )).all()
    return "\n\n".join(f"{label}: {summary}" for label, summary in rows)


async def _count_regions(session, volume_id: str) -> int:
    """How many regions this volume produced (0 means nothing readable)."""
    stmt = (select(func.count(Region.id))
            .join(Page, Region.page_id == Page.id)
            .where(Page.volume_id == volume_id))
    return int((await session.execute(stmt)).scalar() or 0)


async def _summarize_volume(session: AsyncSession, volume: Volume, job: Job, series_ctx: dict) -> None:
    """Generate a summary from this volume's translated text (used as context
    for the next volume and shown on the card)."""
    _check_cancel(volume.id)
    await _set_job(session, job, step="summarizing", current=0, total=0,
                   message="Summarizing volume...")
    rows = (await session.execute(
        select(Region.english_text)
        .join(Page, Region.page_id == Page.id)
        .where(Page.volume_id == volume.id, Region.english_text != "")
        .order_by(Page.index, Region.order)
    )).scalars().all()
    text = "\n".join(rows)
    summary = await asyncio.to_thread(llm.summarize, series_ctx, text)
    if summary:
        volume.summary = summary
        await session.commit()


def _is_translated(page: Page) -> bool:
    """A page is done when it has regions and every region has English text."""
    return bool(page.regions) and all(r.english_text.strip() for r in page.regions)


def _has_ocr(page: Page) -> bool:
    return bool(page.regions) and any(r.japanese_text.strip() for r in page.regions)


async def process_volume(volume_id: str, resume: bool = False) -> None:
    """Run the pipeline for one volume. Opens its own DB session.

    When resume=True, already-finished pages are skipped so a paused or failed
    run continues where it left off instead of redoing expensive work.
    """
    # Date anything written before positions existed, before the volume is
    # loaded, so this run reads the dated rows and not stale copies of them.
    async with SessionLocal() as s:
        series_id = (await s.execute(
            select(Volume.series_id).where(Volume.id == volume_id))).scalar_one_or_none()
        if series_id and await memgraph.ensure_positions(s, series_id):
            await s.commit()

    async with SessionLocal() as session:
        volume = await _load_volume(session, volume_id)
        if volume is None:
            log.error(f"[{volume_id}] volume not found; skipping")
            return
        series = volume.series
        job = volume.job or Job(volume_id=volume.id)
        if job.id is None:
            session.add(job)

        store = get_store()
        log.info(f"[{volume_id}] === pipeline start: kind={volume.kind} "
                 f"engine={volume.engine_mode} resume={resume} ===")
        if not resume:
            # A fresh run starts the bill again; a resume adds to it.
            job.llm_calls = job.prompt_tokens = job.completion_tokens = 0
            job.llm_seconds = job.wall_seconds = 0.0
        usage = llm.track_usage()
        started = time.monotonic()
        try:
            _check_cancel(volume_id)
            volume.status = VolumeStatus.PROCESSING.value
            await _set_job(session, job, status=JobStatus.RUNNING.value, error="",
                           step="classifying", current=0, total=len(volume.pages),
                           message="Classifying pages...")

            # The series context, with summaries of earlier volumes. The memory
            # block and glossary are added per call (_prompt_parts), scoped to
            # the page being translated.
            series_ctx = _series_ctx(series)
            series_ctx["previous_summary"] = await _previous_summaries(session, series.id, volume.order)

            unit = "page(s)"
            if volume.kind == VolumeKind.TEXT.value:
                # Digital source: chapters/blocks already sit in pages/regions,
                # so no classify and no OCR, straight to text translation.
                unit = "chunk(s)"
                failed = await _run_text_volume(session, volume, series, job, resume, series_ctx)
            else:
                # --- Stage 1: classify (cheap, always re-run) ---
                for i, page in enumerate(volume.pages):
                    _check_cancel(volume_id)
                    img = await asyncio.to_thread(store.get, page.image_key)
                    page.page_type = (await asyncio.to_thread(
                        classify_page, img, page.image_key.rsplit("/", 1)[-1]
                    )).value
                    await _set_job(session, job, current=i + 1,
                                   message=f"{page.index}: {page.page_type}")

                text_pages = [p for p in volume.pages if p.page_type in _TEXT_TYPES]
                log.info(f"[{volume_id}] {len(text_pages)}/{len(volume.pages)} pages have text")

                if volume.engine_mode == EngineMode.VISION.value:
                    failed = await _run_vision(session, store, volume, series, job, text_pages, resume, series_ctx)
                else:
                    failed = await _run_ocr_text(session, store, volume, series, job, text_pages, resume, series_ctx)

            await _summarize_volume(session, volume, job, series_ctx)

            # A volume with failed pages is not "done": it is done with holes,
            # and saying so is the difference between a demo that lies and one
            # that tells you where to look.
            if failed:
                volume.status = VolumeStatus.ERROR.value
                await _set_job(
                    session, job, status=JobStatus.ERROR.value, step="done",
                    message=f"{failed} {unit} failed. Retranslate or Resume to retry them.",
                    error=f"{failed} {unit} failed",
                )
            else:
                # "Done" with nothing to show is the most misleading state the app
                # has: a scan of artwork, or a bad crop, finishes silently and
                # looks translated. Say out loud that nothing was found.
                produced = await _count_regions(session, volume.id)
                volume.status = VolumeStatus.DONE.value
                await _set_job(session, job, status=JobStatus.DONE.value, step="done",
                               message=("Finished, but no text was found on these pages. "
                                        "Check the scan, or pick another engine.")
                               if produced == 0 else "Processing complete!",
                               error="")
            log.info(f"[{volume_id}] === pipeline done (failed={failed}) ===")
        except _Cancelled:
            log.info(f"[{volume_id}] stopped by user; pausing")
            volume.status = VolumeStatus.PAUSED.value
            # Keep job.step as the stage it stopped at (classify/ocr/vision/translating)
            # so the UI can show "Stopped at <stage> (current/total)".
            await _set_job(session, job, status=JobStatus.CANCELLED.value,
                           message="Stopped. Resume to continue.", error="")
        except Exception as e:  # noqa: BLE001
            log.error(f"[{volume_id}] pipeline FAILED: {e}\n{traceback.format_exc()}")
            volume.status = VolumeStatus.ERROR.value
            # job.step still holds the stage that failed (classifying/ocr/vision/translating)
            failed_step = job.step or "processing"
            await _set_job(session, job, status=JobStatus.ERROR.value, step="error",
                           message=f"Failed during {failed_step}", error=str(e))
        finally:
            _cancel_requested.discard(volume_id)
            await _save_usage(volume_id, usage, time.monotonic() - started)


# Text-volume translation chunking: a chapter can hold hundreds of blocks, so
# requests are sliced to keep prompts well inside the model's context.
_TEXT_CHUNK_BLOCKS = 40
_TEXT_CHUNK_CHARS = 4000


async def _run_text_volume(session, volume, series, job, resume, series_ctx) -> int:
    """Translate a digital volume: chapters are pages, blocks are regions with
    japanese_text already present. Chunks run concurrently; a failed chunk is
    isolated (its blocks stay empty and Resume retries just those)."""
    chunks: list[tuple[object, list[Region]]] = []  # (page, regions-slice)
    for page in volume.pages:
        pending = [r for r in page.regions
                   if r.japanese_text.strip() and (not resume or not r.english_text.strip())]
        cur: list[Region] = []
        size = 0
        for r in pending:
            if cur and (len(cur) >= _TEXT_CHUNK_BLOCKS or size > _TEXT_CHUNK_CHARS):
                chunks.append((page, cur))
                cur, size = [], 0
            cur.append(r)
            size += len(r.japanese_text)
        if cur:
            chunks.append((page, cur))

    total = len(chunks)
    await _set_job(session, job, step="translating", current=0, total=total,
                   message=f"Translating {total} chunk(s) across {len(volume.pages)} chapters...")
    sem = asyncio.Semaphore(_concurrency())
    lock = asyncio.Lock()
    state = {"done": 0, "failed": 0}

    async def handle(page, regions):
        _check_cancel(volume.id)
        payload = [{
            "page_index": page.index,
            "regions": [{"id": f"b{r.order}", "japanese_text": r.japanese_text}
                        for r in regions],
        }]
        async with sem:
            _check_cancel(volume.id)
            ctx, glossary = await _prompt_parts(series.id, series_ctx, volume.order, page.index,
                                                [r.japanese_text for r in regions])
            try:
                result = await asyncio.to_thread(llm.translate_text, ctx, glossary, payload)
            except llm.LLMError as e:
                log.warning(f"[{volume.id}] text chunk failed (ch {page.index}): {e}")
                async with lock:
                    state["failed"] += 1
                    state["done"] += 1
                    await _set_job(session, job, current=state["done"],
                                   message=f"chapter {page.index}: a chunk failed (Resume retries it)")
                return
        async with lock:
            translations: dict[str, str] = {}
            for res in result.get("results", []):
                translations.update(res.get("translations", {}))
            for r in regions:
                t = translations.get(f"b{r.order}")
                if t:
                    r.english_text = t
            await session.commit()
            await _merge_new_terms(session, series, result.get("new_terms", {}),
                                   volume=volume, memory_payload=result.get("memory"),
                                   page_id=page.id, page=page.index)
            state["done"] += 1
            await _set_job(session, job, current=state["done"],
                           message=f"chapter {page.index}: {len(regions)} blocks translated")

    results = await asyncio.gather(*(handle(p, rs) for p, rs in chunks), return_exceptions=True)
    return state["failed"] + _unexpected(volume.id, results)


async def _run_vision(session, store, volume, series, job, text_pages, resume, series_ctx) -> int:
    """Translate each text page with the vision model, concurrently. Returns the
    number of pages that failed (isolated — one bad page doesn't kill the run)."""
    total = len(text_pages)
    todo = [p for p in text_pages if not (resume and _is_translated(p))]
    state = {"done": total - len(todo), "failed": 0}
    await _set_job(session, job, step="vision", current=state["done"], total=total,
                   message="Vision OCR + translating...")
    sem = asyncio.Semaphore(_concurrency())
    lock = asyncio.Lock()  # the AsyncSession is not concurrency-safe; serialize DB writes

    async def handle(page):
        _check_cancel(volume.id)
        async with sem:
            _check_cancel(volume.id)
            img = await asyncio.to_thread(store.get, page.image_key)
            # The page's text is unknown until the model reads it, so the block
            # leads with the most connected names instead of the ones on the page.
            ctx, glossary = await _prompt_parts(series.id, series_ctx, volume.order, page.index, None)
            try:
                result = await asyncio.to_thread(llm.vision_ocr_translate, ctx, glossary, img)
            except llm.LLMError as e:
                log.warning(f"[{volume.id}] vision page {page.index} failed: {e}")
                async with lock:
                    state["failed"] += 1
                    state["done"] += 1
                    await _set_job(session, job, current=state["done"],
                                   message=f"page {page.index} failed (will retry on re-run)")
                return
        async with lock:
            await _replace_regions(session, page, result.get("regions", []))
            # Commit the page before the memory step: memory failures roll back,
            # and an uncommitted translation would be rolled back with them.
            await session.commit()
            await _merge_new_terms(session, series, result.get("new_terms", {}),
                                   volume=volume, memory_payload=result.get("memory"),
                                   page_id=page.id, page=page.index)
            await _maybe_detect_language(
                session, series, [r.get("japanese_text", "") for r in result.get("regions", [])]
            )
            state["done"] += 1
            await _set_job(session, job, current=state["done"],
                           message=f"page {page.index}: {len(result.get('regions', []))} regions")

    # One page raising must never cancel its siblings mid-flight.
    results = await asyncio.gather(*(handle(p) for p in todo), return_exceptions=True)
    return state["failed"] + _unexpected(volume.id, results)


async def _run_ocr_text(session, store, volume, series, job, text_pages, resume, series_ctx) -> int:
    # Stage 2: OCR. Reuse existing OCR on resume; otherwise OCR each page with the
    # configured backend: "vision" (a vision model reads the page, concurrent) or
    # "manga_ocr" (local local OCR + OpenCV columns, sequential/GPU).
    backend = provider.get()["ocr_backend"]
    await _set_job(session, job, step="ocr", current=0, total=len(text_pages),
                   message="Reading text (OCR)...")
    page_regions: dict[str, list[dict]] = {}
    to_ocr = []
    for page in text_pages:
        if resume and _has_ocr(page):
            page_regions[page.id] = [
                {"id": f"col_{r.order}", "order": r.order, "japanese_text": r.japanese_text}
                for r in page.regions
            ]
        else:
            to_ocr.append(page)
    state = {"done": len(text_pages) - len(to_ocr), "failed": 0}

    if backend == "vision":
        sem = asyncio.Semaphore(_concurrency())
        lock = asyncio.Lock()

        async def ocr_one(page):
            _check_cancel(volume.id)
            async with sem:
                _check_cancel(volume.id)
                img = await asyncio.to_thread(store.get, page.image_key)
                try:
                    regions = await asyncio.to_thread(llm.ocr_page_vision, series_ctx, img)
                except llm.LLMError as e:
                    # A page that could not be read is a failed page, not an
                    # empty one: counting it keeps the volume out of "done".
                    log.warning(f"[{volume.id}] OCR page {page.index} failed: {e}")
                    async with lock:
                        state["failed"] += 1
                        state["done"] += 1
                        await _set_job(session, job, current=state["done"],
                                       message=f"page {page.index}: OCR failed (Resume retries it)")
                    return
            async with lock:
                page_regions[page.id] = regions
                await _replace_regions(session, page, regions)
                await _maybe_detect_language(
                    session, series, [r.get("japanese_text", "") for r in regions]
                )
                state["done"] += 1
                await _set_job(session, job, current=state["done"],
                               message=f"page {page.index}: {len(regions)} regions")

        results = await asyncio.gather(*(ocr_one(p) for p in to_ocr), return_exceptions=True)
        state["failed"] += _unexpected(volume.id, results)
    else:
        from services.ocr import ocr_page, unload_ocr
        for page in to_ocr:
            _check_cancel(volume.id)
            img = await asyncio.to_thread(store.get, page.image_key)
            try:
                regions = await asyncio.to_thread(ocr_page, img)
            except Exception as exc:  # noqa: BLE001 - OCR is the least reliable stage
                log.warning(f"[{volume.id}] OCR failed on page {page.index}: {exc}")
                state["failed"] += 1
                state["done"] += 1
                await _set_job(session, job, current=state["done"],
                               message=f"page {page.index}: OCR failed (Resume retries it)")
                continue
            page_regions[page.id] = regions
            await _replace_regions(session, page, regions)
            state["done"] += 1
            await _set_job(session, job, current=state["done"],
                           message=f"page {page.index}: {len(regions)} regions")
        if to_ocr:
            await asyncio.to_thread(unload_ocr)  # free VRAM before the LLM runs

    # Stage 3: translate in batches (concurrently), skipping already-done pages.
    pages_with_text = [p for p in text_pages if page_regions.get(p.id)]
    pending = [p for p in pages_with_text if not (resume and _is_translated(p))]
    batch_size = max(1, min(volume.batch_size, 20))
    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    total_batches = len(batches)
    ocr_failed = state["failed"]
    await _set_job(session, job, step="translating", current=0, total=total_batches,
                   message="Translating...")
    sem = asyncio.Semaphore(_concurrency())
    lock = asyncio.Lock()
    state = {"done": 0, "failed": 0}

    def texts_of(pages):
        return [r["japanese_text"] for p in pages for r in page_regions[p.id]]

    async def handle(batch):
        _check_cancel(volume.id)
        payload = [
            {"page_index": p.index,
             "regions": [{"id": r["id"], "japanese_text": r["japanese_text"]}
                         for r in page_regions[p.id]]}
            for p in batch
        ]
        async with sem:
            _check_cancel(volume.id)
            # A batch reads as of its first page, and what it teaches is stamped
            # at its last: no page in it sees what a later page established.
            first, last = min(p.index for p in batch), max(p.index for p in batch)
            ctx, glossary = await _prompt_parts(series.id, series_ctx, volume.order, first,
                                                texts_of(batch))
            try:
                result = await asyncio.to_thread(llm.translate_text, ctx, glossary, payload)
            except llm.LLMError as e:
                # A batch is several pages in one request. When that request
                # fails it used to take the whole batch down; retry page by page
                # so one poisoned page costs exactly one page.
                log.warning(f"[{volume.id}] translate batch failed ({e}); retrying per page")
                for p in batch:
                    _check_cancel(volume.id)
                    single = [{"page_index": p.index,
                               "regions": [{"id": r["id"], "japanese_text": r["japanese_text"]}
                                           for r in page_regions[p.id]]}]
                    one_ctx, one_glossary = await _prompt_parts(
                        series.id, series_ctx, volume.order, p.index, texts_of([p]))
                    try:
                        one = await asyncio.to_thread(
                            llm.translate_text, one_ctx, one_glossary, single)
                    except llm.LLMError as pe:
                        log.warning(f"[{volume.id}] page {p.index} failed: {pe}")
                        async with lock:
                            state["failed"] += 1
                            state["done"] += 1
                            await _set_job(session, job, current=state["done"],
                                           message=f"page {p.index} failed (Resume retries it)")
                        continue
                    async with lock:
                        for res in one.get("results", []):
                            if res.get("page_index") != p.index:
                                continue
                            await session.refresh(p, ["regions"])
                            order_to_id = {r["order"]: r["id"] for r in page_regions[p.id]}
                            for region in p.regions:
                                rid = order_to_id.get(region.order)
                                t = res["translations"].get(rid) if rid else None
                                if t:
                                    region.english_text = t
                        await session.commit()
                        await _merge_new_terms(session, series, one.get("new_terms", {}),
                                               volume=volume, memory_payload=one.get("memory"),
                                               page_id=p.id, page=p.index)
                        state["done"] += 1
                        await _set_job(session, job, current=state["done"],
                                       message=f"page {p.index} translated (batch retry)")
                return
        async with lock:
            # Map translations back via region order (col_<order>).
            idx_to_page = {p.index: p for p in batch}
            for res in result.get("results", []):
                page = idx_to_page.get(res["page_index"])
                if not page:
                    continue
                await session.refresh(page, ["regions"])
                order_to_id = {r["order"]: r["id"] for r in page_regions[page.id]}
                translations = res["translations"]
                for region in page.regions:
                    rid = order_to_id.get(region.order)
                    if rid and rid in translations:
                        region.english_text = translations[rid]
            await session.commit()
            await _merge_new_terms(session, series, result.get("new_terms", {}),
                                   volume=volume, memory_payload=result.get("memory"),
                                   page_id=None, page=last)
            state["done"] += 1
            await _set_job(session, job, current=state["done"], message=f"batch {state['done']}/{total_batches}")

    results = await asyncio.gather(*(handle(b) for b in batches), return_exceptions=True)
    return ocr_failed + state["failed"] + _unexpected(volume.id, results, [len(b) for b in batches])


async def _save_usage(volume_id: str, usage: dict, wall: float) -> None:
    """Add this run's model usage and wall time to the volume's job.

    Uses its own session: the run's session may be mid-rollback when this runs.
    """
    async with SessionLocal() as s:
        job = (await s.execute(select(Job).where(Job.volume_id == volume_id))).scalar_one_or_none()
        if job is None:
            return
        job.llm_calls = (job.llm_calls or 0) + usage["calls"]
        job.prompt_tokens = (job.prompt_tokens or 0) + usage["prompt_tokens"]
        job.completion_tokens = (job.completion_tokens or 0) + usage["completion_tokens"]
        job.llm_seconds = (job.llm_seconds or 0.0) + usage["seconds"]
        job.wall_seconds = (job.wall_seconds or 0.0) + wall
        await s.commit()


async def _replace_regions(session: AsyncSession, page: Page, regions: list[dict]) -> None:
    """Replace a page's regions with a fresh set (idempotent on re-runs)."""
    await session.refresh(page, ["regions"])
    for r in list(page.regions):
        await session.delete(r)
    for r in regions:
        session.add(Region(
            page_id=page.id, order=r.get("order", 0),
            x=r.get("x", 0), y=r.get("y", 0),
            width=r.get("width", 0), height=r.get("height", 0),
            japanese_text=r.get("japanese_text", ""),
            english_text=r.get("english_text", ""),
            confidence=r.get("confidence", 0.0),
        ))
    await session.commit()


async def _load_volume(session: AsyncSession, volume_id: str) -> Volume | None:
    stmt = (
        select(Volume)
        .where(Volume.id == volume_id)
        .options(
            selectinload(Volume.pages).selectinload(Page.regions),
            selectinload(Volume.job),
            selectinload(Volume.series).selectinload(Series.glossary),
        )
    )
    return (await session.execute(stmt)).scalar_one_or_none()

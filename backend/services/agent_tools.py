"""Tool registry for the master agent (services/agent.py).

One central registry, but each session kind only *sees* its own toolset
(Hermes-style registration vs exposure): the ingest agent gets curation tools,
the chat agent gets lookup tools (the text, the glossary, the series memory)
plus the repairs and metadata edits a user asks for in chat. The one
irreversible tool, delete_volume, refuses to run unless the user has said yes
through ask_user. enqueue_processing is registered but in neither toolset:
translation costs money, so only the user starts it.

Handlers are async, open their own short-lived DB sessions (SQLite is WAL'd),
and return strings (usually JSON) that go back to the model verbatim. ask_user
is special: it raises AskUser to suspend the run until the user answers.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, text

from db.database import SessionLocal
from db.models import (
    AgentMessage,
    AgentSession,
    GlossaryTerm,
    Job,
    JobStatus,
    Page,
    Series,
    SeriesSource,
    Volume,
    VolumeStatus,
)
from logging_config import get_logger
from services import documents, ingest, llm, maintenance, memory, metadata, web_search, webnovel
from storage_backend import get_store


# A browser-ish agent string gets ordinary pages from Wikipedia and publisher
# sites; an empty or bot-flagged one gets 403s.
_UA_PAGE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
# Wikimedia blocks spoofed browser agents and wants a real, contactable one.
_UA_WIKI = "TomeBot/0.2 (https://github.com/computator1200/tome; book metadata)"

log = get_logger("agent_tools")

_MAX_RESULT_CHARS = 8000  # per tool result fed back to the model


class AskUser(Exception):
    """Raised by the ask_user tool to suspend the run pending a user answer."""

    def __init__(self, question: str, options: list[str] | None) -> None:
        super().__init__(question)
        self.question = question
        self.options = options or []


@dataclass
class ToolContext:
    session_id: str
    kind: str
    series_id: str | None = None
    upload_id: str | None = None


def _dump(obj: Any) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) > _MAX_RESULT_CHARS:
        s = s[:_MAX_RESULT_CHARS] + '… (truncated)"'
    return s


async def _get_series(session, series_id: str | None) -> Series:
    if not series_id:
        raise ValueError("No series is attached to this session yet — create one first.")
    series = (await session.execute(
        select(Series).where(Series.id == series_id)
    )).scalar_one_or_none()
    if series is None:
        raise ValueError("Series not found (was it deleted?)")
    return series


# --- ingest tools -------------------------------------------------------------

async def _list_groups(ctx: ToolContext, args: dict) -> str:
    manifest = ingest.load_manifest(ctx.upload_id or "")
    if manifest is None:
        return "Error: upload staging not found (already cleaned up)."
    return _dump(manifest)


async def _sample_pages(ctx: ToolContext, args: dict) -> str:
    count = max(1, min(int(args.get("count", 3)), 5))
    files = ingest.group_files(ctx.upload_id or "", args["group_id"])
    if not files:
        return "Error: group has no image files."
    step = max(1, len(files) // count)
    picked = files[::step][:count]
    out = []
    for path in picked:
        try:
            desc = await asyncio.to_thread(llm.describe_page, path.read_bytes())
        except llm.LLMError as e:
            desc = {"error": str(e)}
        out.append({"file": path.name, **desc})
    return _dump(out)


async def _meta_search(ctx: ToolContext, args: dict) -> str:
    results, notes = await asyncio.to_thread(metadata.search, args["query"])
    out: dict = {"results": [
        {"source": r.source, "ref": r.ref, "title": r.title, "year": r.year,
         "description": (r.description or "")[:300], "genres": r.genres}
        for r in results[:8]
    ]}
    if notes:
        out["notes"] = notes
    return _dump(out)


async def _meta_get(ctx: ToolContext, args: dict) -> str:
    d = await asyncio.to_thread(metadata.fetch_detail, args["source"], args["ref"])
    return _dump({
        "source": d.source, "ref": d.ref, "title": d.title, "author": d.author,
        "description": (d.description or "")[:1500], "genres": d.genres,
        "associated_names": d.associated_names, "status": d.status, "year": d.year,
        "has_poster": bool(d.image),
    })


async def _link_metadata(ctx: ToolContext, args: dict) -> str:
    """Fill the existing series from a metadata source (overwrites title,
    description, author, genres, names, status, year + poster)."""
    detail = await asyncio.to_thread(metadata.fetch_detail, args["source"], args["ref"])
    async with SessionLocal() as session:
        series = await _get_series(session, ctx.series_id)
        applied = await metadata.apply_to_series(series, detail, get_store())
        await session.commit()
    return _dump({"linked": detail.title, "source": detail.source, "applied": applied})


async def _web_search(ctx: ToolContext, args: dict) -> str:
    results = await asyncio.to_thread(web_search.search, args["query"])
    return _dump(results)


async def _create_series(ctx: ToolContext, args: dict) -> str:
    if ctx.series_id:
        return "Error: this session already has a series — use update_series instead."
    detail = None
    if args.get("source") and args.get("ref"):
        try:
            detail = await asyncio.to_thread(
                metadata.fetch_detail, args["source"], args["ref"])
        except Exception as e:  # noqa: BLE001 — metadata is best-effort, proceed manual
            log.warning(f"agent create_series: metadata fetch failed: {e}")
    async with SessionLocal() as session:
        series = Series(
            title=args.get("title") or (detail.title if detail else "Untitled"),
            source=(SeriesSource.NOVELUPDATES.value if detail and detail.nu_url
                    else SeriesSource.MANUAL.value),
            nu_url=(detail.nu_url if detail else None),
            description=args.get("description") or (detail.description if detail else ""),
            author=args.get("author") or (detail.author if detail else None),
            genres=args.get("genres") or (detail.genres if detail else []),
            associated_names=(detail.associated_names if detail else []),
            nu_status=(detail.status if detail else None),
            year=(detail.year if detail else None),
            source_language=args.get("source_language") or "auto",
            target_language=args.get("target_language") or "English",
        )
        session.add(series)
        await session.flush()
        if detail and detail.image:
            try:
                img = await asyncio.to_thread(metadata.fetch_cover, detail.source, detail.image)
                key = f"series/{series.id}/poster.jpg"
                await asyncio.to_thread(get_store().put, key, img, "image/jpeg")
                series.poster_key = key
            except Exception as e:  # noqa: BLE001
                log.warning(f"agent create_series: poster failed: {e}")
        # Attach the new series to this agent session so later tools (and the
        # UI link) see it.
        agent_row = (await session.execute(
            select(AgentSession).where(AgentSession.id == ctx.session_id)
        )).scalar_one_or_none()
        if agent_row is not None:
            agent_row.series_id = series.id
        await session.commit()
        ctx.series_id = series.id
        return _dump({"series_id": series.id, "title": series.title,
                      "source": series.source, "poster": bool(series.poster_key),
                      "metadata_used": bool(detail)})


_PATCHABLE = ("title", "description", "author", "genres", "year",
              "source_language", "target_language", "nu_url")


async def _update_series(ctx: ToolContext, args: dict) -> str:
    async with SessionLocal() as session:
        series = await _get_series(session, ctx.series_id)
        changed = []
        for field in _PATCHABLE:
            if field in args and args[field] is not None:
                setattr(series, field, args[field])
                changed.append(field)
        await session.commit()
        return _dump({"updated": changed})



def _page_meta(html: str) -> dict:
    """Pull the fields a book page actually carries."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    meta = {}
    for prop in ("og:title", "og:description", "og:image", "og:site_name"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            meta[prop] = tag["content"].strip()
    desc = soup.find("meta", attrs={"name": "description"})
    if desc and desc.get("content") and "og:description" not in meta:
        meta["og:description"] = desc["content"].strip()
    if soup.title and soup.title.get_text(strip=True):
        meta["title"] = soup.title.get_text(strip=True)
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    meta["text"] = text[:1500]
    return meta


async def _fetch_page(ctx: ToolContext, args: dict) -> str:
    import httpx

    url = (args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"

    # Wikipedia serves /wiki/ pages with 403 to anything that is not a browser;
    # its REST summary is the same information as clean JSON.
    m = re.match(r"https?://([a-z]{2,3}(?:\.[a-z]{2,3})?)\.wikipedia\.org/wiki/(.+)$", url)
    if m:
        lang, title = m.group(1), m.group(2)
        api = (f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/"
               f"{title.split('#')[0]}")
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                         headers={"User-Agent": _UA_WIKI}) as client:
                r = await client.get(api)
                r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return f"Error: could not fetch {url}: {e}"
        return _dump({
            "url": data.get("content_urls", {}).get("desktop", {}).get("page", url),
            "title": data.get("title") or "",
            "site_name": "Wikipedia",
            "description": data.get("description") or "",
            "summary": data.get("extract") or "",
            "image": (data.get("thumbnail") or {}).get("source")
                     or (data.get("originalimage") or {}).get("source") or "",
        })

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": _UA_PAGE}) as client:
            r = await client.get(url)
            r.raise_for_status()
    except Exception as e:  # noqa: BLE001 - the model needs the reason
        return f"Error: could not fetch {url}: {e}"
    meta = await asyncio.to_thread(_page_meta, r.text)
    meta["url"] = str(r.url)
    return _dump(meta)


async def _set_series_poster(ctx: ToolContext, args: dict) -> str:
    """Store a cover found on the web as this series' poster."""
    import httpx
    from PIL import Image
    import io

    url = (args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"
    async with SessionLocal() as session:
        series = await _get_series(session, ctx.series_id)
        try:
            ua = _UA_WIKI if "wikimedia.org" in url else _UA_PAGE
            async with httpx.AsyncClient(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": ua}) as client:
                r = await client.get(url)
                if r.status_code == 400 and "/thumb/" in url:
                    # Wikimedia only serves listed thumbnail widths; ask for the original.
                    original = url.split("/thumb/")[0] + "/" + url.split("/thumb/")[1].rsplit("/", 1)[0]
                    r = await client.get(original)
                r.raise_for_status()
            raw = r.content
            if len(raw) > 12 * 1024 * 1024:
                return "Error: image is larger than 12 MB."
            img = await asyncio.to_thread(Image.open, io.BytesIO(raw))
            img = await asyncio.to_thread(img.convert, "RGB")
            buf = io.BytesIO()
            await asyncio.to_thread(img.save, buf, format="JPEG", quality=88)
            key = f"series/{series.id}/poster.jpg"
            await asyncio.to_thread(get_store().put, key, buf.getvalue(), "image/jpeg")
            series.poster_key = key
            await session.commit()
            return _dump({"poster_set": True, "url": url, "size": f"{img.width}x{img.height}"})
        except Exception as e:  # noqa: BLE001
            return f"Error: could not use that image: {e}"



def _manifest_key(spec: dict) -> tuple:
    """Sort key for a volume spec: position in the upload, not the title.

    `group_ids` are g1..gN and documents are d1..dN, both assigned in natural
    order over the extracted files, so the numeric part is reading order.
    """
    tokens = list(spec.get("group_ids") or [])
    if spec.get("doc_id"):
        tokens.append(spec["doc_id"])
    numbers = []
    for tok in tokens:
        digits = "".join(ch for ch in str(tok) if ch.isdigit())
        numbers.append(int(digits) if digits else 10**6)
    # Only the position: ties keep the order the model listed them in, which is
    # the order the chapters were read out of the document.
    return (min(numbers) if numbers else 10**6,)


async def _create_volumes(ctx: ToolContext, args: dict) -> str:
    """Batch volume creation — one call organizes a whole upload, so a 15-volume
    tracker dump doesn't eat the iteration budget one create at a time."""
    engine_mode = args.get("engine_mode") or "vision"
    if engine_mode not in ("vision", "ocr_text"):
        return "Error: engine_mode must be 'vision' or 'ocr_text'."
    created, errors = [], []
    async with SessionLocal() as session:
        await _get_series(session, ctx.series_id)
        # Reading order comes from the files, never from the labels. A chapter
        # list sorted by title puts "Bailén" before "Trafalgar" and the series
        # then reads in the wrong order forever, so the manifest index decides
        # and the model's numbering is treated as a suggestion.
        specs = sorted(args["volumes"], key=_manifest_key)
        base = (await session.execute(
            select(func.max(Volume.order)).where(Volume.series_id == ctx.series_id)
        )).scalar() or 0
        renumbered = False
        for offset, spec in enumerate(specs, start=1):
            number = base + offset
            if int(spec.get("number") or 0) != number:
                renumbered = True
            try:
                volume = await ingest.extract_group_to_volume(
                    session, get_store(),
                    series_id=ctx.series_id, upload_id=ctx.upload_id or "",
                    group_ids=list(spec["group_ids"]),
                    label=spec["label"], number=number,
                    engine_mode=engine_mode,
                    start=spec.get("start"), end=spec.get("end"),
                )
                created.append({"volume_id": volume.id, "label": volume.label,
                                "number": volume.order, "page_count": volume.page_count})
            except Exception as e:  # noqa: BLE001 — report per-volume, keep going
                errors.append(f"{spec.get('label', '?')}: {e}")
    out: dict = {"created": created, "ordered_by": "file order"}
    if renumbered:
        out["note"] = "numbering was corrected to the order the files appear in"
    if errors:
        out["errors"] = errors
    return _dump(out)


async def _inspect_document(ctx: ToolContext, args: dict) -> str:
    """Peek inside a digital document (epub/txt/html/pdf/azw3): chapters and a
    text sample — the text-native equivalent of sample_pages."""
    path = ingest.document_path(ctx.upload_id or "", args["doc_id"])
    try:
        d = await asyncio.to_thread(documents.extract_document, path)
    except documents.DocumentError as e:
        return f"Error: {e}"
    chapters = d["chapters"]
    sample_ch = chapters[min(2, len(chapters) - 1)]
    return _dump({
        "format": d["format"], "title": d.get("title"), "author": d.get("author"),
        "language": d.get("language"), "chapter_count": len(chapters),
        "total_blocks": sum(len(c["blocks"]) for c in chapters),
        "chapter_titles": [c["title"] for c in chapters if c.get("title")][:20],
        "text_sample": sample_ch["blocks"][:4],
    })


async def _create_volumes_from_documents(ctx: ToolContext, args: dict) -> str:
    """Batch: one text volume per digital document."""
    created, errors = [], []
    async with SessionLocal() as session:
        await _get_series(session, ctx.series_id)
        # Same rule as scans: the order the documents were uploaded in wins.
        specs = sorted(args["volumes"], key=_manifest_key)
        base = (await session.execute(
            select(func.max(Volume.order)).where(Volume.series_id == ctx.series_id)
        )).scalar() or 0
        for offset, spec in enumerate(specs, start=1):
            number = base + offset
            try:
                path = ingest.document_path(ctx.upload_id or "", spec["doc_id"])
                extraction = await asyncio.to_thread(documents.extract_document, path)
                volume = await ingest.create_text_volume(
                    session, get_store(), series_id=ctx.series_id,
                    label=spec["label"], number=number,
                    extraction=extraction,
                )
                created.append({"volume_id": volume.id, "label": volume.label,
                                "number": volume.order, "chapters": volume.page_count})
            except Exception as e:  # noqa: BLE001 — report per-volume, keep going
                errors.append(f"{spec.get('label', spec.get('doc_id', '?'))}: {e}")
    out: dict = {"created": created, "ordered_by": "file order"}
    if errors:
        out["errors"] = errors
    return _dump(out)



async def _convert_pdf_to_scans(ctx: ToolContext, args: dict) -> str:
    """Render a PDF with an unusable text layer into page images, as a group."""
    if not ctx.upload_id:
        return "Error: no upload is attached to this session."
    try:
        result = await asyncio.to_thread(
            ingest.render_pdf_group, ctx.upload_id, args["doc_id"],
            int(args.get("dpi") or 140))
    except Exception as e:  # noqa: BLE001 — the model gets to see the reason
        return f"Error: could not render that document: {e}"
    return _dump(result)



async def _repair_volume_pages(ctx: ToolContext, args: dict) -> str:
    """Re-split a text volume whose pages are too big to read."""
    async with SessionLocal() as session:
        try:
            out = await maintenance.repaginate_text_volume(session, args["volume_id"])
        except Exception as e:  # noqa: BLE001
            return f"Error: could not re-paginate that volume: {e}"
    return _dump(out)


async def _reorder_volumes(ctx: ToolContext, args: dict) -> str:
    """Set the reading order of a series: the order the user names, or the one
    the content implies."""
    async with SessionLocal() as session:
        await _get_series(session, ctx.series_id)
        order = args.get("order") or []
        if not order:
            return "Error: pass at least two volume labels or ids in the order they should read."
        try:
            out = await maintenance.reorder_volumes(session, ctx.series_id, list(order))
        except Exception as e:  # noqa: BLE001
            return f"Error: could not reorder: {e}"
    return _dump(out)


# A reply that agrees. The option buttons the UI shows are usually "Yes, ..."
_YES = re.compile(r"^\s*(yes|y|yeah|yep|ok|okay|sure|confirm(ed)?|go ahead|do it|delete)\b"
                  r"|^\s*(はい|ええ|うん|お願いします)", re.IGNORECASE)


async def _user_confirmed(session_id: str, label: str, volume_id: str) -> bool:
    """Did the user's latest message answer yes to an ask_user naming this volume?

    Walks the transcript back from the newest message: the first user message is
    the latest reply, and the ask_user call before it is the question it answers.
    """
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(AgentMessage).where(AgentMessage.session_id == session_id)
            .order_by(AgentMessage.seq.desc()).limit(40))).scalars().all()
    reply = None
    for m in rows:
        if m.role == "user" and reply is None:
            reply = m.content or ""
            continue
        if reply is None or m.role != "assistant":
            continue
        for call in m.tool_calls or []:
            fn = call.get("function") or {}
            if fn.get("name") != "ask_user":
                continue
            try:
                question = str(json.loads(fn.get("arguments") or "{}").get("question", ""))
            except (ValueError, AttributeError):
                question = ""
            names_it = (label and label.lower() in question.lower()) or volume_id in question
            return bool(names_it and _YES.match(reply))
        if m.tool_calls:
            return False
    return False


async def _delete_volume(ctx: ToolContext, args: dict) -> str:
    """Remove one volume. Irreversible, so it runs only after the user has said
    yes to an ask_user question that named this volume."""
    async with SessionLocal() as session:
        volume = (await session.execute(
            select(Volume).where(Volume.id == args["volume_id"]))).scalar_one_or_none()
        if volume is None or volume.series_id != ctx.series_id:
            return "Error: no volume with that id in this series."
        label, vid = volume.label, volume.id
        if not await _user_confirmed(ctx.session_id, label, vid):
            return (f"Refused: deleting '{label}' cannot be undone. Call ask_user with a "
                    f"question that names '{label}', and delete only after the user says yes.")
        try:
            await asyncio.to_thread(get_store().delete_prefix, f"volumes/{vid}")
        except Exception:  # noqa: BLE001 — storage cleanup is best effort
            pass
        await session.delete(volume)
        await session.commit()
    return _dump({"deleted": vid, "label": label})


async def _fetch_webnovel_index(ctx: ToolContext, args: dict) -> str:
    idx = await asyncio.to_thread(webnovel.fetch_index, args["url"])
    return _dump({
        "url": idx.url, "site": idx.site, "title": idx.title, "author": idx.author,
        "synopsis": idx.synopsis[:800], "chapter_count": len(idx.chapters),
        "first_chapters": [c["title"] for c in idx.chapters[:10]],
        "last_chapters": [c["title"] for c in idx.chapters[-3:]],
    })


async def _create_volume_from_webnovel(ctx: ToolContext, args: dict) -> str:
    """Fetch webnovel chapters (rate-limited) into a text volume. start/end are
    1-based inclusive chapter positions — split long novels into arcs."""
    async with SessionLocal() as session:  # verify the series BEFORE fetching
        await _get_series(session, ctx.series_id)
    idx = await asyncio.to_thread(webnovel.fetch_index, args["url"])
    chapters = await asyncio.to_thread(
        webnovel.fetch_chapters, idx, int(args.get("start") or 1),
        int(args["end"]) if args.get("end") else None)
    async with SessionLocal() as session:
        volume = await ingest.create_text_volume(
            session, get_store(), series_id=ctx.series_id,
            label=args["label"], number=int(args["number"]),
            extraction={"chapters": chapters, "source_path": None},
        )
        return _dump({"volume_id": volume.id, "label": volume.label,
                      "number": volume.order, "chapters": volume.page_count})


async def _enqueue_processing(ctx: ToolContext, args: dict) -> str:
    from services.worker import enqueue  # late import — avoids cycle at module load

    queued = []
    async with SessionLocal() as session:
        for vid in args["volume_ids"]:
            volume = (await session.execute(
                select(Volume).where(Volume.id == vid)
            )).scalar_one_or_none()
            if volume is None or volume.series_id != ctx.series_id:
                return f"Error: volume '{vid}' not found in this series."
            if volume.status in (VolumeStatus.QUEUED.value, VolumeStatus.PROCESSING.value):
                continue
            volume.status = VolumeStatus.QUEUED.value
            job = (await session.execute(
                select(Job).where(Job.volume_id == vid)
            )).scalar_one_or_none()
            if job is None:
                job = Job(volume_id=vid)
                session.add(job)
            job.status = JobStatus.QUEUED.value
            job.step = ""
            job.current = 0
            job.total = 0
            job.message = "Queued by agent"
            job.error = ""
            queued.append(vid)
        await session.commit()
    for vid in queued:
        await enqueue(vid)
    return _dump({"queued": queued})


async def _ask_user(ctx: ToolContext, args: dict) -> str:
    raise AskUser(args["question"], args.get("options"))


# --- chat tools ----------------------------------------------------------------

async def _get_memory(ctx: ToolContext, args: dict) -> str:
    """The series graph as of the latest volume, optionally around one name."""
    name = (args.get("name") or "").strip()
    async with SessionLocal() as session:
        latest = (await session.execute(select(func.max(Volume.order)).where(
            Volume.series_id == ctx.series_id))).scalar() or 1
        view = await memory.build_context(session, ctx.series_id, latest,
                                          texts=[name] if name else None,
                                          max_entities=10 if name else 40, max_edges=40)
    if name:
        named = [e for e in view["entities"]
                 if name in (e["name"], *e["aliases"]) or (e["rendering"] or "").lower() == name.lower()]
        keep = {e["name"] for e in named}
        view["entities"] = named or view["entities"]
        view["edges"] = [e for e in view["edges"] if keep & {e["src"], e["dst"]}] or view["edges"]
    return _dump({"volume": view["volume"], "entities": view["entities"], "edges": view["edges"]})


async def _get_series_info(ctx: ToolContext, args: dict) -> str:
    async with SessionLocal() as session:
        series = await _get_series(session, ctx.series_id)
        volumes = (await session.execute(
            select(Volume).where(Volume.series_id == series.id).order_by(Volume.order)
        )).scalars().all()
        return _dump({
            "title": series.title, "author": series.author, "year": series.year,
            "genres": series.genres, "description": (series.description or "")[:1500],
            "languages": f"{series.source_language} -> {series.target_language}",
            "nu_url": series.nu_url,
            "volumes": [{"volume_id": v.id, "label": v.label, "number": v.order,
                         "status": v.status, "pages": v.page_count} for v in volumes],
        })


async def _get_summaries(ctx: ToolContext, args: dict) -> str:
    async with SessionLocal() as session:
        rows = (await session.execute(
            select(Volume.label, Volume.summary)
            .where(Volume.series_id == (ctx.series_id or ""), Volume.summary != "")
            .order_by(Volume.order)
        )).all()
        if not rows:
            return "No volume summaries exist yet (volumes may not be translated)."
        return _dump([{"volume": label, "summary": summary} for label, summary in rows])


async def _get_glossary(ctx: ToolContext, args: dict) -> str:
    async with SessionLocal() as session:
        terms = (await session.execute(
            select(GlossaryTerm).where(GlossaryTerm.series_id == (ctx.series_id or ""))
        )).scalars().all()
        if not terms:
            return "The glossary is empty."
        return _dump([{"kind": t.kind, "source": t.jp, "rendering": t.en} for t in terms])


def _fts_query(q: str) -> str:
    """Quote each term so user text can't break FTS5 MATCH syntax."""
    terms = [t.replace('"', "") for t in q.split() if t.strip('"')]
    return " ".join(f'"{t}"' for t in terms) or '""'


async def _search_text(ctx: ToolContext, args: dict) -> str:
    limit = max(1, min(int(args.get("limit", 8)), 20))
    async with SessionLocal() as session:
        try:
            rows = (await session.execute(text(
                'SELECT v.label, p."index", '
                "snippet(region_fts, 0, '[', ']', '…', 16) AS snip "
                "FROM region_fts "
                "JOIN regions r ON r.rowid = region_fts.rowid "
                "JOIN pages p ON p.id = r.page_id "
                "JOIN volumes v ON v.id = p.volume_id "
                "WHERE region_fts MATCH :q AND v.series_id = :sid "
                "ORDER BY rank LIMIT :n"
            ), {"q": _fts_query(args["query"]), "sid": ctx.series_id, "n": limit})).all()
        except Exception as e:  # noqa: BLE001 — non-SQLite or FTS missing: LIKE fallback
            log.warning(f"FTS search failed ({e}); falling back to LIKE")
            like = f"%{args['query']}%"
            rows = (await session.execute(text(
                'SELECT v.label, p."index", substr(r.english_text, 1, 200) AS snip '
                "FROM regions r "
                "JOIN pages p ON p.id = r.page_id "
                "JOIN volumes v ON v.id = p.volume_id "
                "WHERE v.series_id = :sid AND (r.english_text LIKE :like "
                "OR r.japanese_text LIKE :like) LIMIT :n"
            ), {"sid": ctx.series_id, "like": like, "n": limit})).all()
    if not rows:
        return "No matches in the translated text."
    return _dump([{"volume": label, "page": idx, "snippet": snip}
                  for label, idx, snip in rows])


async def _read_page(ctx: ToolContext, args: dict) -> str:
    async with SessionLocal() as session:
        volume = (await session.execute(
            select(Volume).where(Volume.id == args["volume_id"])
        )).scalar_one_or_none()
        if volume is None or volume.series_id != ctx.series_id:
            return "Error: volume not found in this series."
        page = (await session.execute(
            select(Page).where(Page.volume_id == volume.id,
                               Page.index == int(args["page_index"]))
        )).scalar_one_or_none()
        if page is None:
            return f"Error: no page {args['page_index']} in {volume.label}."
        rows = (await session.execute(text(
            'SELECT english_text FROM regions WHERE page_id = :pid ORDER BY "order"'
        ), {"pid": page.id})).scalars().all()
        body = "\n".join(r for r in rows if r)
        return body[:6000] if body else "(this page has no translated text)"


# --- registry -------------------------------------------------------------------

Handler = Callable[[ToolContext, dict], Awaitable[str]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


_S = {"type": "string"}
_I = {"type": "integer"}
_SL = {"type": "array", "items": {"type": "string"}}
# metadata sources are a closed set — models otherwise invent values like "syosetu"
_SRC = {"type": "string",
        "enum": ["openlibrary", "googlebooks", "internetarchive", "dnb", "bnf"]}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_groups",
         "List the image groups found in the uploaded files (directories of images, "
         "nested archives already extracted), plus extraction warnings.",
         _obj({}), _list_groups),
    Tool("sample_pages",
         "Look at a few sample pages from a group with the vision model to verify what "
         "the content actually is (novel scans? manga? unrelated documents?) and its language. "
         "Use before creating volumes.",
         _obj({"group_id": _S, "count": _I}, ["group_id"]), _sample_pages),
    Tool("search_series_metadata",
         "Search for series metadata by title (NovelUpdates first, AniList fallback — "
         "handles English, romaji and Japanese queries). Returns {source, ref} pairs to "
         "pass to get_series_metadata / create_series / link_series_metadata.",
         _obj({"query": _S}, ["query"]), _meta_search),
    Tool("get_series_metadata",
         "Fetch full metadata for one search result (source + ref).",
         _obj({"source": _SRC, "ref": _S}, ["source", "ref"]), _meta_get),
    Tool("link_series_metadata",
         "Fill/overwrite THIS series' metadata (title, description, author, genres, "
         "status, year, poster) from a metadata search result.",
         _obj({"source": _SRC, "ref": _S}, ["source", "ref"]), _link_metadata),
    Tool("web_search",
         "General web search (DuckDuckGo). Use for niche titles the metadata sources lack, "
         "or to resolve volume/chapter organization questions.",
         _obj({"query": _S}, ["query"]), _web_search),
    Tool("create_series",
         "Create the series entry for this workspace. Pass source+ref from "
         "search_series_metadata whenever a match exists — metadata and the poster are "
         "pulled automatically; explicit fields override.",
         _obj({"title": _S, "source": _SRC, "ref": _S, "description": _S, "author": _S,
               "genres": _SL, "source_language": _S, "target_language": _S}, ["title"]),
         _create_series),
    Tool("update_series",
         "Update series metadata fields (title, description, author, genres, "
         "source_language, target_language, nu_url).",
         _obj({"title": _S, "description": _S, "author": _S, "genres": _SL,
               "source_language": _S, "target_language": _S, "nu_url": _S}), _update_series),
    Tool("create_volumes",
         "Create volumes from image groups — pass ALL volumes in ONE call. Each spec: "
         "{label, number, group_ids, start?, end?}. Images combine in natural sort order; "
         "start/end (1-based, inclusive) slice that combined list when one folder holds "
         "multiple volumes. number is the volume's position in the series.",
         _obj({"volumes": {"type": "array", "items": _obj(
                   {"label": _S, "number": _I, "group_ids": _SL, "start": _I, "end": _I},
                   ["label", "number", "group_ids"])},
               "engine_mode": {"type": "string", "enum": ["vision", "ocr_text"]}},
              ["volumes"]), _create_volumes),
    Tool("inspect_document",
         "Peek inside a digital document from the manifest (epub/txt/html/pdf/azw3): "
         "title/author/language, chapter list and a text sample. The text-native "
         "equivalent of sample_pages — use it to verify content before creating volumes.",
         _obj({"doc_id": _S}, ["doc_id"]), _inspect_document),
    Tool("create_volumes_from_documents",
         "Create one text volume per digital document — pass ALL volumes in ONE call. "
         "Each spec: {doc_id, label, number}. Chapters and text are extracted "
         "automatically; epubs keep their original file for translated re-export.",
         _obj({"volumes": {"type": "array", "items": _obj(
                   {"doc_id": _S, "label": _S, "number": _I},
                   ["doc_id", "label", "number"])}}, ["volumes"]),
         _create_volumes_from_documents),
    Tool("fetch_webnovel_index",
         "Fetch a webnovel's index page from a URL (Syosetu supported natively, incl. "
         "novel18; generic pages fall back to single-page text). Returns title, author, "
         "synopsis and the chapter list — use for identification before creating volumes.",
         _obj({"url": _S}, ["url"]), _fetch_webnovel_index),
    Tool("create_volume_from_webnovel",
         "Download webnovel chapters into one text volume (rate-limited; max 200 chapters "
         "per call). start/end are 1-based inclusive chapter positions — split very long "
         "novels into multiple volumes by arc or by ~50-100 chapters.",
         _obj({"url": _S, "label": _S, "number": _I, "start": _I, "end": _I},
              ["url", "label", "number"]), _create_volume_from_webnovel),
    Tool("fetch_page",
         "Read a web page and return its title, meta description, og:image and a text "
         "excerpt. Use it to fill metadata no catalogue has: author, publication year, "
         "synopsis, cover image.",
         _obj({"url": _S}, ["url"]), _fetch_page),
    Tool("set_series_poster",
         "Set this series' cover from an image URL (any http(s) image). Use the og:image "
         "fetch_page returned, or a cover URL you found.",
         _obj({"url": _S}, ["url"]), _set_series_poster),
    Tool("repair_volume_pages",
         "Re-split a text volume whose pages are too big: one page holding a whole "
         "novel, or pages that clearly should be chapters. Region text and its "
         "translations are preserved; only page boundaries change. Use it when the "
         "user says a book reads as one endless page or the reader cannot paginate.",
         _obj({"volume_id": _S}, ["volume_id"]),
         _repair_volume_pages),
    Tool("reorder_volumes",
         "Set the reading order of the series. Pass volume labels (what the user sees) "
         "or ids, first to last. Use it when volumes are in the wrong order, for "
         "example a collection whose chapter titles were sorted alphabetically.",
         _obj({"order": {"type": "array", "items": {"type": "string"},
                         "description": "volume labels or ids, first to last"}},
              ["order"]),
         _reorder_volumes),
    Tool("delete_volume",
         "Delete one volume and its pages. Destructive and not undoable: the prompt "
         "requires ask_user confirmation before this is called.",
         _obj({"volume_id": _S}, ["volume_id"]),
         _delete_volume),
    Tool("convert_pdf_to_scans",
         "Render a PDF whose text layer is unusable (symbol soup from a font with no "
         "Unicode mapping) into page images, registered as an image group. Use this "
         "when inspect_document or an import reports a broken text layer, then create "
         "the volume from the returned group_id with create_volumes.",
         _obj({"doc_id": _S, "dpi": {"type": "integer", "minimum": 90, "maximum": 300}},
              ["doc_id"]),
         _convert_pdf_to_scans),
    Tool("enqueue_processing",
         "Queue volumes for translation on the processing pipeline (scans get OCR/vision "
         "first; text volumes translate directly). COSTS LLM USAGE — always confirm with "
         "ask_user first.",
         _obj({"volume_ids": _SL}, ["volume_ids"]), _enqueue_processing),
    Tool("ask_user",
         "Ask the user a question and wait for their answer (pauses this run and alerts "
         "them in the UI). Use when uncertain, when content looks wrong, and always before "
         "enqueue_processing.",
         _obj({"question": _S, "options": _SL}, ["question"]), _ask_user),
    Tool("get_series_info",
         "Get this series' metadata and its volumes (ids, labels, status, page counts).",
         _obj({}), _get_series_info),
    Tool("get_summaries",
         "Get the stored plot summary of each translated volume.",
         _obj({}), _get_summaries),
    Tool("get_glossary",
         "Get the series glossary (character names and term renderings).",
         _obj({}), _get_glossary),
    Tool("get_memory",
         "What the series memory knows: characters, places and organisations, the "
         "other names each goes by, how the translation renders them, and the "
         "relations between them with the volume that established each. Pass a name "
         "(source or translated) to focus on one entity and its relations.",
         _obj({"name": _S}), _get_memory),
    Tool("search_text",
         "Full-text search the series' translated (and source) text. Returns matching "
         "snippets with volume + page locations.",
         _obj({"query": _S, "limit": _I}, ["query"]), _search_text),
    Tool("read_page",
         "Read the full translated text of one page (volume_id + 0-based page_index).",
         _obj({"volume_id": _S, "page_index": _I}, ["volume_id", "page_index"]), _read_page),
]}

TOOLSETS: dict[str, list[str]] = {
    "ingest": ["list_groups", "sample_pages", "inspect_document",
               "fetch_webnovel_index", "search_series_metadata",
               "get_series_metadata", "link_series_metadata", "web_search",
               "fetch_page", "set_series_poster", "create_series",
               "update_series", "create_volumes",
               "create_volumes_from_documents", "create_volume_from_webnovel",
               "convert_pdf_to_scans", "repair_volume_pages", "reorder_volumes",
               "get_series_info", "ask_user"],
    "chat": ["get_series_info", "get_summaries", "get_glossary", "get_memory", "search_text",
             "read_page", "search_series_metadata", "get_series_metadata",
             "link_series_metadata", "web_search", "fetch_page",
             "set_series_poster", "update_series", "repair_volume_pages",
             "reorder_volumes", "delete_volume", "ask_user"],
}


def schemas_for(kind: str) -> list[dict]:
    return [
        {"type": "function",
         "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
        for t in (TOOLS[n] for n in TOOLSETS[kind])
    ]


async def run_tool(ctx: ToolContext, name: str, args: dict) -> str:
    """Execute one tool call. AskUser propagates (it suspends the run); every
    other failure comes back as an honest string so the model can react."""
    tool = TOOLS.get(name)
    if tool is None or name not in TOOLSETS[ctx.kind]:
        return f"Error: unknown tool '{name}'."
    try:
        return await tool.handler(ctx, args)
    except AskUser:
        raise
    except Exception as e:  # noqa: BLE001 — the model should see tool failures
        log.warning(f"tool {name} failed: {e}")
        return f"Error: {e}"

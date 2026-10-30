"""Quality checks on a translated volume, without trusting the model's word.

Two checks, both cheap enough to run on every volume:

- glossary_report: for every glossary term that occurs in a page's source text
  and was known by that page, does the page's translation use the agreed
  rendering? A term rendered one way in volume 2 and another in volume 7 is the
  failure a series translator exists to prevent, and this is where it shows.
  No model call.
- regression_sample: re-translate a fixed sample of pages with the current model
  and prompts, without saving anything, and measure how far the result moved
  from the stored translation. Run it before switching model or prompt; a low
  score means the change would alter what readers have already read.
"""
from __future__ import annotations

import asyncio
import difflib
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import EngineMode, GlossaryTerm, Page, Volume, VolumeKind
from services import llm, memory, pipeline
from storage_backend import get_store


def _uses_rendering(term: GlossaryTerm, text: str) -> bool:
    """Is the agreed rendering in this translation?

    A character is often called by one part of the full name ("Lizette" for
    "Lizette Arnault"), so for names any word of three letters or more counts.
    """
    folded = text.casefold()
    en = term.en.strip().casefold()
    if en and en in folded:
        return True
    if term.kind == "character":
        return any(len(w) >= 3 and re.search(rf"(?<!\w){re.escape(w)}(?!\w)", folded)
                   for w in re.split(r"[\s\-・]+", en))
    return False


async def glossary_report(session: AsyncSession, volume_id: str, limit: int = 50) -> dict:
    """How consistently this volume used the series glossary, and what it added."""
    volume = (await session.execute(
        select(Volume).where(Volume.id == volume_id)
        .options(selectinload(Volume.pages).selectinload(Page.regions)))).scalar_one_or_none()
    if volume is None:
        raise LookupError("volume not found")
    terms = (await session.execute(select(GlossaryTerm).where(
        GlossaryTerm.series_id == volume.series_id,
        GlossaryTerm.first_volume != memory.UNKNOWN))).scalars().all()
    order = max(1, volume.order)

    uses = misses = 0
    per_term: dict[str, dict] = {}
    for page in volume.pages:
        source = "\n".join(r.japanese_text for r in page.regions)
        target = "\n".join(r.english_text for r in page.regions)
        if not target.strip():
            continue
        for t in terms:
            if not t.jp or t.jp not in source:
                continue
            if (t.first_volume, t.first_page) > (order, page.index):
                continue  # not known yet on this page, so not a rule yet either
            uses += 1
            row = per_term.setdefault(t.id, {"jp": t.jp, "en": t.en, "kind": t.kind,
                                             "uses": 0, "misses": 0, "pages": []})
            row["uses"] += 1
            if not _uses_rendering(t, target):
                misses += 1
                row["misses"] += 1
                if len(row["pages"]) < 8:
                    row["pages"].append(page.index + 1)

    drift = sorted((r for r in per_term.values() if r["misses"]),
                   key=lambda r: (-r["misses"], r["jp"]))[:limit]
    new_terms = [{"jp": t.jp, "en": t.en, "kind": t.kind}
                 for t in sorted(terms, key=lambda t: (t.first_page, t.jp))
                 if t.first_volume == order][:limit]
    return {
        "volume": order,
        "uses": uses,
        "misses": misses,
        "consistency": round((uses - misses) / uses, 4) if uses else None,
        "drift": drift,
        "new_terms": new_terms,
        "new_term_count": sum(1 for t in terms if t.first_volume == order),
    }


def similarity(a: str, b: str) -> float:
    """Word-level similarity of two translations, 0 to 1."""
    wa, wb = re.findall(r"\w+", a.casefold()), re.findall(r"\w+", b.casefold())
    if not wa and not wb:
        return 1.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


async def regression_sample(session: AsyncSession, volume_id: str, pages: int = 5) -> dict:
    """Re-translate the first `pages` translated pages and compare with what is stored.

    The sample is fixed (the first pages in reading order), so two runs of this
    check compare like with like. Nothing is written. Costs one model call per page.
    """
    volume = (await session.execute(
        select(Volume).where(Volume.id == volume_id)
        .options(selectinload(Volume.pages).selectinload(Page.regions),
                 selectinload(Volume.series)))).scalar_one_or_none()
    if volume is None:
        raise LookupError("volume not found")
    sample = [p for p in volume.pages
              if any(r.english_text.strip() for r in p.regions)][:max(1, pages)]
    series_ctx = pipeline._series_ctx(volume.series)
    series_ctx["previous_summary"] = await pipeline._previous_summaries(
        session, volume.series_id, volume.order)
    usage = llm.track_usage()
    results = []
    for page in sample:
        stored = "\n".join(r.english_text for r in page.regions if r.english_text.strip())
        texts = [r.japanese_text for r in page.regions if r.japanese_text.strip()]
        vision = volume.kind != VolumeKind.TEXT.value and volume.engine_mode == EngineMode.VISION.value
        ctx, glossary = await pipeline._prompt_parts(
            volume.series_id, series_ctx, volume.order, page.index, None if vision else texts)
        try:
            if vision:
                img = await asyncio.to_thread(get_store().get, page.image_key)
                out = await asyncio.to_thread(llm.vision_ocr_translate, ctx, glossary, img)
                fresh = "\n".join(r["english_text"] for r in out["regions"] if r["english_text"])
            else:
                payload = [{"page_index": page.index,
                            "regions": [{"id": f"r{r.order}", "japanese_text": r.japanese_text}
                                        for r in page.regions if r.japanese_text.strip()]}]
                out = await asyncio.to_thread(llm.translate_text, ctx, glossary, payload)
                fresh = "\n".join(v for res in out["results"] for v in res["translations"].values())
        except llm.LLMError as e:
            results.append({"page": page.index + 1, "error": str(e)})
            continue
        results.append({"page": page.index + 1, "similarity": round(similarity(stored, fresh), 4)})
    scores = [r["similarity"] for r in results if "similarity" in r]
    return {
        "volume": volume.order,
        "pages": results,
        "mean_similarity": round(sum(scores) / len(scores), 4) if scores else None,
        "usage": usage,
    }

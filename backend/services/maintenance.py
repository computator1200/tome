"""Repairs the librarian can run on a library that went wrong.

These exist because the interesting failures are not import failures: a PDF whose
font has no Unicode map, a collection whose chapter titles got sorted
alphabetically, a book that arrived as a single page. Each one is fixable from
the tools here, so the librarian can answer "the order is wrong" with an action
rather than an explanation.
"""
from __future__ import annotations

import unicodedata
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Page, PageType, Region, Volume
from logging_config import get_logger
from services.documents import _chapterize

log = get_logger(__name__)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


async def repaginate_text_volume(session: AsyncSession, volume_id: str) -> dict:
    """Split a text volume's oversized pages into readable ones.

    Chapters are re-derived from the stored regions, so the text itself is never
    touched: only page boundaries move. Translations travel with their region.
    """
    volume = (await session.execute(
        select(Volume).where(Volume.id == volume_id))).scalar_one_or_none()
    if volume is None:
        raise ValueError("volume not found")
    if volume.kind != "text":
        raise ValueError("only text volumes can be re-paginated; scans are already one page per image")

    pages = (await session.execute(
        select(Page).where(Page.volume_id == volume_id).order_by(Page.index)
    )).scalars().all()
    if not pages:
        raise ValueError("volume has no pages")

    regions = (await session.execute(
        select(Region).join(Page, Page.id == Region.page_id)
        .where(Page.volume_id == volume_id)
        .order_by(Page.index, Region.order)
    )).scalars().all()
    if not regions:
        raise ValueError("volume has no text")

    before = len(pages)
    chapters = _chapterize([r.japanese_text or "" for r in regions])

    # A volume that already has the right shape must not be rebuilt: rebuilding
    # changes every page id for no gain, and the caller is told changed=false, so
    # the churn would be invisible.
    # Counted with SQL: touching p.regions lazily inside the async session raises
    # MissingGreenlet.
    counts = dict((await session.execute(
        select(Region.page_id, func.count()).join(Page, Page.id == Region.page_id)
        .where(Page.volume_id == volume_id).group_by(Region.page_id))).all())
    per_page = [counts.get(p.id, 0) for p in sorted(pages, key=lambda p: p.index)]
    if before == len(chapters) and per_page == [len(c["blocks"]) for c in chapters]:
        return {"volume_id": volume_id, "pages_before": before, "pages_after": before,
                "changed": False, "note": "already paginated"}

    # Illustration and cover pages carry an image_key and no regions; they are
    # not rebuilt here, and silently deleting them loses the only reference to
    # the file, so refuse instead.
    if any((p.page_type or "") != "text" for p in pages):
        raise ValueError("volume contains non-text pages (illustrations or covers); "
                         "re-pagination would drop them")

    old_page_ids = [p.id for p in pages]
    # Regions are moved onto the new pages before the old ones go, because the
    # foreign key cascades: deleting a page takes its regions with it, and losing
    # them would lose the translation this repair exists to preserve.
    offset = 0
    new_pages: list[Page] = []
    for index, chapter in enumerate(chapters):
        page = Page(volume_id=volume_id, index=index, image_key="",
                    chapter_title=(chapter.get("title") or "")[:300],
                    page_type=PageType.TEXT.value)
        session.add(page)
        new_pages.append(page)
    await session.flush()

    for page, chapter in zip(new_pages, chapters):
        for order, region in enumerate(regions[offset: offset + len(chapter["blocks"])]):
            region.page_id = page.id
            region.order = order
        offset += len(chapter["blocks"])
    await session.flush()

    await session.execute(delete(Page).where(Page.id.in_(old_page_ids)))
    await session.flush()

    volume.page_count = len(chapters)
    await session.commit()
    log.info(f"repair: repaginated volume {volume_id} {before} -> {len(chapters)} pages")
    return {"volume_id": volume_id, "pages_before": before,
            "pages_after": len(chapters), "changed": len(chapters) != before}


async def reorder_volumes(session: AsyncSession, series_id: str,
                          order: list[str]) -> dict:
    """Set volume order from a list of labels or ids, first item first.

    Accepts either the label the user sees or the id, because the librarian has
    both and the user has only the label. Volumes not named keep their relative
    position after the named ones.
    """
    volumes = (await session.execute(
        select(Volume).where(Volume.series_id == series_id).order_by(Volume.order)
    )).scalars().all()
    if not volumes:
        raise ValueError("series has no volumes")

    def _fold(text: str) -> str:
        # Users type "Bailen" for "Bailén"; matching on the exact accents turned a
        # correct order into a silent partial reorder.
        decomposed = unicodedata.normalize("NFKD", (text or "").strip().lower())
        return "".join(ch for ch in decomposed if not unicodedata.combining(ch))

    by_key: dict[str, Volume] = {}
    for v in volumes:
        by_key[v.id.lower()] = v
        by_key[_fold(v.label or "")] = v

    ordered: list[Volume] = []
    missing: list[str] = []
    for key in order:
        v = by_key.get(_fold(str(key)))
        if v is None:
            missing.append(key)
        elif v not in ordered:
            ordered.append(v)
    ordered.extend(v for v in volumes if v not in ordered)

    changes = []
    for position, v in enumerate(ordered, start=1):
        if v.order != position:
            changes.append({"label": v.label, "from": v.order, "to": position})
        v.order = position
    await session.commit()
    log.info(f"repair: reordered {len(changes)} volumes in series {series_id}")
    return {"series_id": series_id, "order": [v.label for v in ordered],
            "changed": changes, "unknown": missing}

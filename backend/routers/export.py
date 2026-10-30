import asyncio
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.database import get_session
from db.models import Page, PageType, Volume, VolumeKind
from services import documents
from services.export import build_pdf
from storage_backend import get_store

router = APIRouter(prefix="/api", tags=["export"])

_TEXT_TYPES = (PageType.TEXT.value, PageType.MIXED.value)


def _disposition(raw_name: str) -> dict:
    ascii_name = re.sub(r'[^A-Za-z0-9 ._-]', "_", raw_name)
    return {"Content-Disposition":
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(raw_name)}"}


def _load_source(key: str | None) -> bytes | None:
    """The original document, if we still have it.

    Older text volumes were imported before the object store was copied, so the
    key can point at an object that no longer exists. That must degrade to a
    generated EPUB, never to a 500.
    """
    if not key:
        return None
    try:
        return get_store().get(key)
    except (FileNotFoundError, OSError):
        return None


@router.get("/volumes/{volume_id}/export.epub")
async def export_epub(volume_id: str, session: AsyncSession = Depends(get_session)):
    """Translated EPUB for a text volume. When the volume came from an epub,
    the ORIGINAL file is repacked (same CSS/images/cover, text swapped,
    reading direction flipped LTR); otherwise (webnovel/txt/pdf) a clean epub
    is generated from the translated chapters."""
    stmt = (
        select(Volume).where(Volume.id == volume_id)
        .options(selectinload(Volume.pages).selectinload(Page.regions),
                 selectinload(Volume.series))
    )
    volume = (await session.execute(stmt)).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    if volume.kind != VolumeKind.TEXT.value:
        raise HTTPException(status_code=400,
                            detail="EPUB export is for digital (text) volumes — scans export as PDF.")

    series = volume.series
    title = f"{series.title} — {volume.label}"

    def _build() -> bytes:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.epub"
            source = _load_source(volume.source_key)
            if source is not None:
                src = Path(td) / "src.epub"
                src.write_bytes(source)
                translations = {
                    (p.index, r.order): r.english_text
                    for p in volume.pages for r in p.regions if r.english_text.strip()
                }
                documents.repack_epub(src, out, translations,
                                      series.target_language, title_override=title)
            else:
                chapters = [
                    {"title": p.chapter_title,
                     "blocks": [r.english_text or r.japanese_text
                                for r in p.regions
                                if (r.english_text or r.japanese_text).strip()]}
                    for p in volume.pages
                ]
                cover = None
                if series.poster_key:
                    try:
                        cover = get_store().get(series.poster_key)
                    except Exception:  # noqa: BLE001 — cover is optional
                        cover = None
                documents.generate_epub(out, title, series.author or "",
                                        series.target_language, chapters, cover)
            return out.read_bytes()

    data = await asyncio.to_thread(_build)
    return Response(content=data, media_type="application/epub+zip",
                    headers=_disposition(f"{series.title} - {volume.label}.epub"))


@router.get("/volumes/{volume_id}/export.pdf")
async def export_pdf(
    volume_id: str,
    theme: str = Query("dark"),
    session: AsyncSession = Depends(get_session),
):
    stmt = (
        select(Volume).where(Volume.id == volume_id)
        .options(
            selectinload(Volume.pages).selectinload(Page.regions),
            selectinload(Volume.series),
        )
    )
    volume = (await session.execute(stmt)).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")

    # An untranslated volume would export as a blank document; say so instead.
    if not any(r.english_text.strip() for p in volume.pages for r in p.regions):
        raise HTTPException(
            status_code=400,
            detail="This volume has no translation yet - process it before exporting.")

    # Flatten to plain data so the blocking PDF build doesn't touch the ORM.
    pages = [
        {
            "image_key": p.image_key,
            "is_text": p.page_type in _TEXT_TYPES,
            "lines": [r.english_text for r in p.regions if r.english_text.strip()],
        }
        for p in volume.pages
    ]
    pdf = await asyncio.to_thread(build_pdf, pages, 0.85, theme)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers=_disposition(f"{volume.series.title} - {volume.label}.pdf"),
    )

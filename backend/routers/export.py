import asyncio
import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import config
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


# Exports are built once per state of the translation and kept on disk, so a
# repeat download is a file read, and every download streams from a file
# instead of holding the whole PDF or EPUB in memory.
_EXPORT_DIR = Path(config.LOCAL_STORAGE_DIR) / "_exports"


def _fingerprint(volume, extra: str = "") -> str:
    """Changes whenever anything that ends up in the export changes."""
    h = hashlib.sha1(extra.encode())
    for p in volume.pages:
        h.update(f"{p.id}|{p.page_type}|{p.image_key}|{p.chapter_title}".encode())
        for r in p.regions:
            h.update(f"{r.order}|{r.x}|{r.y}|{r.width}|{r.height}|{r.english_text}"
                     f"|{r.japanese_text}".encode())
    return h.hexdigest()[:16]


async def _cached(volume_id: str, suffix: str, fingerprint: str, build) -> Path:
    """The export file for this fingerprint, building it first if needed.

    The build writes to a temporary name and is renamed into place, so a
    download never sees a half-written file. Older builds of the same volume
    and variant are removed.
    """
    _EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{volume_id}-{suffix}"
    target = _EXPORT_DIR / f"{stem}-{fingerprint}"
    if target.exists():
        return target
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    await asyncio.to_thread(build, tmp)
    os.replace(tmp, target)
    for old in _EXPORT_DIR.glob(f"{stem}-*"):
        if old != target and not old.name.endswith(".tmp"):
            old.unlink(missing_ok=True)
    return target


def _boxes(regions) -> list[dict]:
    """Translated regions with their boxes, or [] unless every one has a box.

    A page where only some blocks were located would mix placed text with text
    that has nowhere to go, so it is laid out as a column instead.
    """
    translated = [r for r in regions if r.english_text.strip()]
    if not translated or any(r.width <= 0 or r.height <= 0 for r in translated):
        return []
    return [{"x": r.x, "y": r.y, "width": r.width, "height": r.height,
             "text": r.english_text} for r in translated]


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

    def _build(out: Path) -> None:
        with tempfile.TemporaryDirectory() as td:
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

    path = await _cached(volume.id, "epub", _fingerprint(volume, title + series.target_language
                                                         + (volume.source_key or "")), _build)
    return FileResponse(path, media_type="application/epub+zip",
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
            "boxes": _boxes(p.regions),
        }
        for p in volume.pages
    ]
    theme = "light" if theme == "light" else "dark"
    path = await _cached(volume.id, f"pdf-{theme}", _fingerprint(volume, theme),
                         lambda out: build_pdf(pages, 0.85, theme, path=str(out)))
    return FileResponse(path, media_type="application/pdf",
                        headers=_disposition(f"{volume.series.title} - {volume.label}.pdf"))

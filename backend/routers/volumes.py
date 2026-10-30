import asyncio
import zipfile
from io import BytesIO
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from natsort import natsorted
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.database import get_session
from db.models import (
    EngineMode,
    Job,
    JobStatus,
    Page,
    Series,
    Volume,
    VolumeStatus,
)
from models.api import CreateVolumeOut, PageOut, RegionOut, VolumeDetail
from services import quality
from services.worker import enqueue
from storage_backend import get_store

router = APIRouter(prefix="/api", tags=["volumes"])

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


@router.get("/series/{series_id}/volumes")
async def list_volumes(series_id: str, session: AsyncSession = Depends(get_session)):
    """The volumes of one series, in reading order.

    Only POST existed, so an API client asking for the list got the SPA's HTML
    with a 200 - a success status wrapping the wrong document.
    """
    series = (await session.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    rows = (await session.execute(
        select(Volume).where(Volume.series_id == series_id).order_by(Volume.order)
    )).scalars().all()
    return [
        {
            "id": v.id,
            "label": v.label,
            "number": v.order,
            "order": v.order,
            "kind": v.kind,
            "status": v.status,
            "page_count": v.page_count,
        }
        for v in rows
    ]


@router.post("/series/{series_id}/volumes", response_model=CreateVolumeOut, status_code=201)
async def create_volume(
    series_id: str,
    file: UploadFile,
    engine_mode: EngineMode = Query(EngineMode.VISION),
    batch_size: int = Query(5, ge=1, le=20),
    number: int | None = Query(None, ge=1, description="Volume number (1-based); defaults to next"),
    label: str | None = Query(None),
    session: AsyncSession = Depends(get_session),
):
    series = (await session.execute(select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted")

    content = await file.read()
    # Extract image members, flattened to basenames, natural-sorted.
    members: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(BytesIO(content)) as zf:
        for m in zf.infolist():
            if m.is_dir():
                continue
            name = Path(m.filename).name
            if not name or name.startswith(".") or Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            members.append((name, zf.read(m)))
    if not members:
        raise HTTPException(status_code=400, detail="No image files found in zip")
    members = natsorted(members, key=lambda t: t[0])

    # Volume number: explicit if given, else next after the highest existing.
    existing = (await session.execute(
        select(Volume).where(Volume.series_id == series_id)
    )).scalars().all()
    vol_number = number if number is not None else (max((v.order for v in existing), default=0) + 1)
    volume = Volume(
        series_id=series_id, label=label or f"Volume {vol_number}", order=vol_number,
        status=VolumeStatus.QUEUED.value, engine_mode=engine_mode.value,
        batch_size=batch_size, page_count=len(members),
    )
    session.add(volume)
    await session.flush()  # assign volume.id

    store = get_store()
    for index, (name, data) in enumerate(members):
        ext = Path(name).suffix.lower()
        key = f"volumes/{volume.id}/pages/{index:04d}{ext}"
        await asyncio.to_thread(store.put, key, data, "image/jpeg")
        try:
            with Image.open(BytesIO(data)) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            w = h = 0
        session.add(Page(volume_id=volume.id, index=index, image_key=key, width=w, height=h))

    session.add(Job(volume_id=volume.id, status=JobStatus.QUEUED.value, message="Queued"))
    await session.commit()

    await enqueue(volume.id)
    return CreateVolumeOut(volume_id=volume.id, series_id=series_id)


@router.get("/volumes/{volume_id}", response_model=VolumeDetail)
async def get_volume(volume_id: str, session: AsyncSession = Depends(get_session)):
    stmt = (
        select(Volume)
        .where(Volume.id == volume_id)
        .options(
            selectinload(Volume.job),
            selectinload(Volume.pages).selectinload(Page.regions),
        )
    )
    volume = (await session.execute(stmt)).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")

    store = get_store()
    pages = [
        PageOut(
            id=p.id, index=p.index, page_type=p.page_type, width=p.width, height=p.height,
            chapter_title=p.chapter_title or "",
            # Text pages (chapters) have no image; "" is the contract the UI
            # checks for, so never turn a missing key into a store URL.
            image_url=store.url(p.image_key) if p.image_key else "",
            regions=[RegionOut.model_validate(r) for r in p.regions],
        )
        for p in volume.pages
    ]
    detail = VolumeDetail.model_validate(volume)
    detail.pages = pages
    return detail


@router.delete("/volumes/{volume_id}")
async def delete_volume(volume_id: str, session: AsyncSession = Depends(get_session)):
    volume = (await session.execute(select(Volume).where(Volume.id == volume_id))).scalar_one_or_none()
    if volume is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    await asyncio.to_thread(get_store().delete_prefix, f"volumes/{volume_id}")
    await session.delete(volume)
    await session.commit()
    return {"deleted": volume_id}


@router.get("/volumes/{volume_id}/quality")
async def volume_quality(volume_id: str, session: AsyncSession = Depends(get_session)):
    """How consistently this volume used the series glossary, which terms drifted
    and on which pages, and which terms it introduced. No model call."""
    try:
        return await quality.glossary_report(session, volume_id)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="Volume not found") from e

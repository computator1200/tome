import asyncio
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.database import get_session
from db.models import JobStatus, Series, SeriesSource, Volume, VolumeStatus
from logging_config import get_logger
from models.api import (
    CreateSeriesIn,
    MetadataLinkIn,
    SeriesDetail,
    SeriesPatchIn,
    SeriesSummary,
    VolumeSummary,
)
from services import memory as memgraph, metadata
from services import novelupdates as nu
from storage_backend import get_store

router = APIRouter(prefix="/api/series", tags=["series"])
log = get_logger("series")


# --- serialization helpers --------------------------------------------------

def _poster_url(series: Series) -> str | None:
    return get_store().url(series.poster_key) if series.poster_key else None


def _aggregate_status(volumes: list[Volume]) -> str:
    statuses = {v.status for v in volumes}
    if not statuses:
        return VolumeStatus.UPLOADED.value
    if statuses & {VolumeStatus.PROCESSING.value, VolumeStatus.QUEUED.value}:
        return VolumeStatus.PROCESSING.value
    if VolumeStatus.PAUSED.value in statuses:
        return VolumeStatus.PAUSED.value
    if statuses == {VolumeStatus.DONE.value}:
        return VolumeStatus.DONE.value
    if VolumeStatus.ERROR.value in statuses:
        return VolumeStatus.ERROR.value
    if VolumeStatus.DONE.value in statuses:
        # Some volumes are translated and some are not. Calling that "not
        # processed" hides work the user has already paid for.
        return "partial"
    return VolumeStatus.UPLOADED.value


def _summary(series: Series) -> SeriesSummary:
    return SeriesSummary(
        id=series.id, title=series.title, source=series.source, nu_url=series.nu_url,
        description=series.description, author=series.author, genres=series.genres or [],
        nu_status=series.nu_status, year=series.year, poster_url=_poster_url(series),
        source_language=series.source_language,
        detected_language=getattr(series, "detected_language", "") or "",
        target_language=series.target_language,
        volume_count=len(series.volumes),
        page_count=sum(v.page_count for v in series.volumes),
        status=_aggregate_status(series.volumes),
    )


async def _store_poster(series_id: str, image_bytes: bytes, ext: str) -> str:
    key = f"series/{series_id}/poster{ext}"
    await asyncio.to_thread(get_store().put, key, image_bytes, "image/jpeg")
    return key


def _ext_from_url(url: str) -> str:
    suffix = PurePosixPath(url.split("?")[0]).suffix.lower()
    return suffix if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif") else ".jpg"


# --- routes -----------------------------------------------------------------

@router.get("", response_model=list[SeriesSummary])
async def list_series(session: AsyncSession = Depends(get_session)):
    stmt = select(Series).options(selectinload(Series.volumes)).order_by(Series.updated_at.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [_summary(s) for s in rows]


@router.post("", response_model=SeriesDetail, status_code=201)
async def create_series(body: CreateSeriesIn, session: AsyncSession = Depends(get_session)):
    if not body.nu_url and not body.manual:
        raise HTTPException(status_code=400, detail="Provide either nu_url or manual")

    if body.nu_url:
        try:
            detail = await asyncio.to_thread(nu.fetch_series, body.nu_url)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not fetch series from NovelUpdates: {e}")
        series = Series(
            title=detail.title, source=SeriesSource.NOVELUPDATES.value, nu_url=detail.nu_url,
            description=detail.description, author=detail.author, genres=detail.genres,
            associated_names=detail.associated_names, nu_status=detail.status, year=detail.year,
        )
        session.add(series)
        await session.flush()  # assign series.id
        if detail.image:
            try:
                img = await asyncio.to_thread(nu.fetch_image, detail.image)
                series.poster_key = await _store_poster(series.id, img, _ext_from_url(detail.image))
            except Exception as e:  # noqa: BLE001
                log.warning(f"poster download failed for {series.id}: {e}")
    else:
        series = Series(
            title=body.manual.title, source=SeriesSource.MANUAL.value,
            description=body.manual.description,
            source_language=body.manual.source_language,
            target_language=body.manual.target_language,
        )
        session.add(series)
        await session.flush()

    await session.commit()
    return await _detail(session, series.id)


@router.get("/{series_id}", response_model=SeriesDetail)
async def get_series(series_id: str, session: AsyncSession = Depends(get_session)):
    return await _detail(session, series_id)


@router.patch("/{series_id}", response_model=SeriesDetail)
async def patch_series(series_id: str, body: SeriesPatchIn, session: AsyncSession = Depends(get_session)):
    series = await _get_or_404(session, series_id)
    if body.title is not None:
        series.title = body.title
    if body.description is not None:
        series.description = body.description
    if body.source_language is not None:
        series.source_language = body.source_language
    if body.target_language is not None:
        series.target_language = body.target_language
    await session.commit()
    return await _detail(session, series_id)


@router.post("/{series_id}/metadata/link", response_model=SeriesDetail)
async def link_metadata(series_id: str, body: MetadataLinkIn,
                        session: AsyncSession = Depends(get_session)):
    """Match an existing series to a metadata source result — overwrites the
    metadata fields + poster (volumes and glossary are untouched). This is the
    recovery path when an import happened while NovelUpdates was unreachable."""
    series = await _get_or_404(session, series_id)
    try:
        detail = await asyncio.to_thread(metadata.fetch_detail, body.source, body.ref)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not fetch metadata: {e}")
    await metadata.apply_to_series(series, detail, get_store())
    await session.commit()
    return await _detail(session, series_id)


@router.post("/{series_id}/poster", response_model=SeriesDetail)
async def upload_poster(series_id: str, file: UploadFile, session: AsyncSession = Depends(get_session)):
    series = await _get_or_404(session, series_id)
    ext = _ext_from_url(file.filename or "poster.jpg")
    data = await file.read()
    series.poster_key = await _store_poster(series.id, data, ext)
    await session.commit()
    return await _detail(session, series_id)


@router.delete("/{series_id}")
async def delete_series(series_id: str, session: AsyncSession = Depends(get_session)):
    series = await _get_or_404(session, series_id)
    await asyncio.to_thread(get_store().delete_prefix, f"series/{series_id}")
    for vol in series.volumes:
        await asyncio.to_thread(get_store().delete_prefix, f"volumes/{vol.id}")
    await session.delete(series)
    await session.commit()
    return {"deleted": series_id}


# --- internal ---------------------------------------------------------------

async def _get_or_404(session: AsyncSession, series_id: str) -> Series:
    stmt = select(Series).where(Series.id == series_id).options(selectinload(Series.volumes))
    series = (await session.execute(stmt)).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    return series


async def _detail(session: AsyncSession, series_id: str) -> SeriesDetail:
    stmt = (
        select(Series)
        .where(Series.id == series_id)
        .options(selectinload(Series.volumes).selectinload(Volume.job))
    )
    series = (await session.execute(stmt)).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    base = _summary(series)
    return SeriesDetail(
        **base.model_dump(),
        associated_names=series.associated_names or [],
        volumes=[VolumeSummary.model_validate(v) for v in series.volumes],
    )


@router.get("/{series_id}/memory")
async def series_memory(series_id: str, volume: int | None = None,
                        session: AsyncSession = Depends(get_session)):
    """What the graph knows, and what a reader at `volume` is allowed to see.

    Without `volume` it reports everything the series has established so far,
    which is what the series page shows; with it, the answer is scoped the same
    way the prompt is, so you can see exactly what a translation run would see.
    """
    series = (await session.execute(
        select(Series).where(Series.id == series_id))).scalar_one_or_none()
    if series is None:
        raise HTTPException(status_code=404, detail="Series not found")
    latest = (await session.execute(
        select(func.max(Volume.order)).where(Volume.series_id == series_id))).scalar() or 1
    if volume is not None and volume > latest:
        # Answering an impossible volume with "everything" hid a client bug.
        raise HTTPException(
            status_code=400,
            detail=f"This series has {latest} volume(s); volume={volume} does not exist yet.")
    if volume is not None and volume < 1:
        # volume=0 used to fall through a falsy check to "everything", and a
        # negative number was read as volume 1.
        raise HTTPException(status_code=400, detail="volume must be 1 or greater")
    if await memgraph.ensure_positions(session, series_id):
        await session.commit()
    ctx = await memgraph.build_context(session, series_id,
                                       latest if volume is None else volume,
                                       max_entities=80, max_edges=60)
    stats = await memgraph.stats(session, series_id)
    # The view is capped (80 entities / 60 edges) and scoped; say whether it
    # left anything out, so a caller can tell a complete picture from part of it.
    return {
        "stats": stats,
        "scoped_to_volume": ctx["volume"],
        "entities": ctx["entities"],
        "edges": ctx["edges"],
        "truncated": {
            "entities": len(ctx["entities"]) < stats["entities"],
            "edges": len(ctx["edges"]) < stats["edges"],
        },
    }


# --- corrections ---------------------------------------------------------------
# Extraction is a model's reading of the page, so the graph can be wrong. These
# let a person fix it: merge two entities that are one, split off a form that was
# merged by mistake, and delete what should not be there.

class MemoryMergeIn(BaseModel):
    keep_id: str
    drop_id: str


async def _correct(session, series_id: str, action) -> dict:
    if (await session.execute(select(Series.id).where(Series.id == series_id))).scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Series not found")
    try:
        result = await action()
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"ok": True, **({"id": result} if isinstance(result, str) else {})}


@router.get("/{series_id}/memory/entities")
async def memory_entities(series_id: str, session: AsyncSession = Depends(get_session)):
    """Every entity with all of its names, unscoped: what the correction UI lists."""
    return await memgraph.entity_details(session, series_id)


@router.post("/{series_id}/memory/merge")
async def memory_merge(series_id: str, body: MemoryMergeIn,
                       session: AsyncSession = Depends(get_session)):
    return await _correct(session, series_id, lambda: memgraph.merge_entities(
        session, series_id, body.keep_id, body.drop_id))


@router.post("/{series_id}/memory/aliases/{alias_id}/split")
async def memory_split(series_id: str, alias_id: str,
                       session: AsyncSession = Depends(get_session)):
    return await _correct(session, series_id, lambda: memgraph.split_alias(
        session, series_id, alias_id))


@router.delete("/{series_id}/memory/entities/{entity_id}")
async def memory_delete_entity(series_id: str, entity_id: str,
                               session: AsyncSession = Depends(get_session)):
    return await _correct(session, series_id, lambda: memgraph.delete_entity(
        session, series_id, entity_id))


@router.delete("/{series_id}/memory/edges/{edge_id}")
async def memory_delete_edge(series_id: str, edge_id: str,
                             session: AsyncSession = Depends(get_session)):
    return await _correct(session, series_id, lambda: memgraph.delete_edge(
        session, series_id, edge_id))

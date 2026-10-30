import asyncio

from fastapi import APIRouter, HTTPException, Query

from logging_config import get_logger
from models.api import MetadataSearchOut, MetaResultOut, NUSearchResult
from services import metadata
from services import novelupdates as nu

router = APIRouter(prefix="/api", tags=["metadata"])
log = get_logger("nu_router")


@router.get("/metadata/search", response_model=MetadataSearchOut)
async def metadata_search(q: str = Query(..., min_length=1)):
    """Unified series-metadata search: NovelUpdates first, AniList fallback
    (NU is Cloudflare-gated and periodically unreachable server-side). Used by
    the add-series wizard and the edit-page "match metadata" picker."""
    results, notes = await asyncio.to_thread(metadata.search, q)
    return MetadataSearchOut(
        results=[MetaResultOut(source=r.source, ref=r.ref, title=r.title,
                               description=r.description, image=r.image,
                               genres=r.genres, year=r.year) for r in results],
        notes=notes,
    )


@router.get("/novelupdates/search", response_model=list[NUSearchResult])
async def search(q: str = Query(..., min_length=1)):
    """Proxy a NovelUpdates search for the add-series picker."""
    try:
        results = await asyncio.to_thread(nu.search, q)
    except Exception as e:  # noqa: BLE001
        # NU is behind Cloudflare; if it blocks us, tell the client so the UI
        # can fall back to manual entry rather than failing silently.
        log.warning(f"NU search failed: {e}")
        raise HTTPException(status_code=502, detail=f"NovelUpdates unreachable: {e}")
    return [
        NUSearchResult(
            title=r.title, nu_url=r.nu_url, image=r.image,
            description=r.description, genres=r.genres,
        )
        for r in results
    ]

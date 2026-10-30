import mimetypes

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import FileResponse

from storage_backend import get_store
from storage_backend.local import LocalObjectStore

router = APIRouter(tags=["files"])


@router.get("/files/{key:path}")
async def get_file(key: str):
    """Serve an object from the store. Local storage streams the file directly;
    any other backend (S3) is read through the store and sent as-is."""
    store = get_store()
    if not key or key.startswith("/") or ".." in key.split("/"):
        raise HTTPException(status_code=404, detail="Not found")
    if isinstance(store, LocalObjectStore):
        path = store.local_path(key)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Not found")
        if not path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        return FileResponse(path)

    if not store.exists(key):
        raise HTTPException(status_code=404, detail="Not found")
    data = store.get(key)
    media_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)

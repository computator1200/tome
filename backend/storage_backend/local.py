import shutil
from pathlib import Path
from urllib.parse import quote

from storage_backend.base import ObjectStore
from config import URL_PREFIX


class LocalObjectStore(ObjectStore):
    """Stores objects on the local filesystem under a root directory, treating
    keys as relative paths. Behaves like a bucket so it can be swapped for S3."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Normalise and guard against path traversal escaping the root.
        p = (self.root / key).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise ValueError(f"Illegal object key: {key!r}")
        return p

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        p = self._path(key)
        if p.exists():
            p.unlink()

    def delete_prefix(self, prefix: str) -> None:
        p = self._path(prefix)
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()

    def url(self, key: str) -> str:
        return f"{URL_PREFIX}/files/{quote(key)}"

    def local_path(self, key: str) -> Path:
        """Local-only helper so routers/files.py can stream via FileResponse."""
        return self._path(key)

from abc import ABC, abstractmethod


class ObjectStore(ABC):
    """Minimal object-store interface. The app only depends on this contract,
    so swapping LocalObjectStore for an S3 implementation later requires no
    changes to services/routers — only the factory."""

    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        ...

    @abstractmethod
    def get(self, key: str) -> bytes:
        ...

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None:
        """Delete every object whose key starts with `prefix` (e.g. a volume's pages)."""
        ...

    @abstractmethod
    def url(self, key: str) -> str:
        """Public-ish URL the frontend can load. For local storage this is a
        path served by routers/files.py; for S3 it could be a presigned URL."""
        ...

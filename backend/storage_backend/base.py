from abc import ABC, abstractmethod


class ObjectStore(ABC):
    """Minimal object-store interface. The app only depends on this contract:
    LocalObjectStore and S3ObjectStore are interchangeable, and the factory
    picks one from STORAGE_BACKEND."""

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
        """URL the frontend can load: a path served by routers/files.py, for
        both backends, so a bucket never has to be public."""
        ...

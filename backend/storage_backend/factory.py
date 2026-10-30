from functools import lru_cache

from config import LOCAL_STORAGE_DIR, STORAGE_BACKEND
from storage_backend.base import ObjectStore
from storage_backend.local import LocalObjectStore


@lru_cache(maxsize=1)
def get_store() -> ObjectStore:
    if STORAGE_BACKEND == "local":
        return LocalObjectStore(LOCAL_STORAGE_DIR)
    if STORAGE_BACKEND == "s3":
        # Future: return S3ObjectStore(bucket=..., prefix=...) backed by boto3.
        # The ObjectStore contract is all the app depends on, so wiring this up
        # is the only change needed to deploy against S3.
        raise NotImplementedError(
            "STORAGE_BACKEND=s3 is not implemented yet; add storage_backend/s3.py."
        )
    raise ValueError(f"Unknown STORAGE_BACKEND: {STORAGE_BACKEND!r}")

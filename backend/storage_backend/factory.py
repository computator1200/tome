from functools import lru_cache

import os

from config import LOCAL_STORAGE_DIR, STORAGE_BACKEND
from storage_backend.base import ObjectStore
from storage_backend.local import LocalObjectStore


@lru_cache(maxsize=1)
def get_store() -> ObjectStore:
    if STORAGE_BACKEND == "local":
        return LocalObjectStore(LOCAL_STORAGE_DIR)
    if STORAGE_BACKEND == "s3":
        from storage_backend.s3 import S3ObjectStore
        # Credentials fall back to boto3's own chain (env, profile, instance role)
        # when the S3_* variables are not set.
        return S3ObjectStore(
            bucket=os.getenv("S3_BUCKET", ""),
            prefix=os.getenv("S3_PREFIX", ""),
            endpoint_url=os.getenv("S3_ENDPOINT_URL"),
            region_name=os.getenv("S3_REGION"),
            aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY"),
        )
    raise ValueError(f"Unknown STORAGE_BACKEND: {STORAGE_BACKEND!r}")

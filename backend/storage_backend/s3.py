from urllib.parse import quote

from storage_backend.base import ObjectStore
from config import URL_PREFIX


class S3ObjectStore(ObjectStore):
    """Stores objects in an S3 bucket, or anything that speaks the S3 API (MinIO,
    Garage, Cloudflare R2) via `endpoint_url`. Keys are the same as the local
    store's, under an optional `prefix`.

    Pages are still served through routers/files.py, as with local storage, so
    the bucket never has to be public and the app's subpath rules apply unchanged.
    """

    def __init__(self, bucket: str, prefix: str = "", client=None, **client_kwargs):
        if not bucket:
            raise ValueError("STORAGE_BACKEND=s3 needs S3_BUCKET")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        if client is None:
            import boto3  # only needed when S3 is configured
            client = boto3.client("s3", **{k: v for k, v in client_kwargs.items() if v})
        self.client = client

    def _key(self, key: str) -> str:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError(f"Illegal object key: {key!r}")
        return self.prefix + key

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data,
                               ContentType=content_type)

    def get(self, key: str) -> bytes:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        except self.client.exceptions.NoSuchKey as e:
            # Callers treat a missing object the way the local store reports it.
            raise FileNotFoundError(key) from e
        return obj["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except self.client.exceptions.ClientError as e:
            if str(e.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))

    def delete_prefix(self, prefix: str) -> None:
        full = self._key(prefix.rstrip("/")) + "/"
        # Every listed key is deleted, so each pass lists from the start again
        # instead of following a continuation token past keys that are gone.
        while True:
            page = self.client.list_objects_v2(Bucket=self.bucket, Prefix=full)
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if not keys:
                return
            # delete_objects takes at most 1000 keys, which is also a page's size.
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})

    def url(self, key: str) -> str:
        return f"{URL_PREFIX}/files/{quote(key)}"

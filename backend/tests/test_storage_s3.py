"""The S3 object store, against a fake client that behaves like S3."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from storage_backend.s3 import S3ObjectStore  # noqa: E402


class _FakeS3:
    class exceptions:  # noqa: N801 - mirrors boto3's client.exceptions
        class NoSuchKey(Exception):
            pass

        class ClientError(Exception):
            def __init__(self, code):
                self.response = {"Error": {"Code": code}}

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()

        class Body:
            def __init__(self, data):
                self.data = data

            def read(self):
                return self.data
        return {"Body": Body(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.ClientError("404")

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]  # tiny pages, to exercise paging
        more = start + 2 < len(keys)
        return {"Contents": [{"Key": k} for k in page], "IsTruncated": more,
                "NextContinuationToken": str(start + 2) if more else None}

    def delete_objects(self, Bucket, Delete):
        for o in Delete["Objects"]:
            self.objects.pop(o["Key"], None)


def test_s3_store_round_trip_and_prefix_delete():
    fake = _FakeS3()
    store = S3ObjectStore("tome", prefix="lib", client=fake)
    for i in range(5):
        store.put(f"volumes/v1/pages/{i:04d}.jpg", b"img", "image/jpeg")
    store.put("volumes/v2/pages/0000.jpg", b"keep", "image/jpeg")
    assert store.get("volumes/v1/pages/0003.jpg") == b"img"
    assert store.exists("volumes/v2/pages/0000.jpg")
    assert set(fake.objects) >= {"lib/volumes/v1/pages/0000.jpg"}
    store.delete_prefix("volumes/v1")
    assert sorted(fake.objects) == ["lib/volumes/v2/pages/0000.jpg"]
    assert not store.exists("volumes/v1/pages/0000.jpg")
    with pytest.raises(FileNotFoundError):
        store.get("volumes/v1/pages/0000.jpg")
    assert store.url("volumes/v2/pages/0000.jpg").endswith("/files/volumes/v2/pages/0000.jpg")


@pytest.mark.parametrize("key", ["", "/etc/passwd", "volumes/../../secret"])
def test_s3_store_rejects_keys_that_escape(key):
    with pytest.raises(ValueError):
        S3ObjectStore("tome", client=_FakeS3()).get(key)

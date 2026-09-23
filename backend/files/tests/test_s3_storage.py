"""Tests for the S3/MinIO storage backend.

The boto3 client is faked so these stay offline: the same narrow contract the
``LocalStorage`` tests pin is exercised here, plus config resolution and the
``FileNotFoundError`` mapping.
"""

from __future__ import annotations

import io

import pytest

from files.services import S3Storage, get_storage


class FakeClientError(Exception):
    """Mimic ``botocore.exceptions.ClientError``'s ``response`` shape."""

    def __init__(self, code: str, status: int = 404) -> None:
        super().__init__(code)
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class FakeS3Client:
    """Minimal in-memory S3 client covering the contract's calls."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, *, Bucket, Key, Body):  # noqa: N803 - boto3 kwarg names
        self.objects[(Bucket, Key)] = bytes(Body)

    def head_object(self, *, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def get_object(self, *, Bucket, Key):  # noqa: N803
        if (Bucket, Key) not in self.objects:
            raise FakeClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def client():
    return FakeS3Client()


@pytest.fixture
def storage(client):
    return S3Storage(client=client, bucket="printforge")


def test_round_trip(storage, client):
    relative = "projects/1/v1/model.stl"
    payload = b"solid fake\nendsolid fake\n"

    assert storage.exists(relative) is False
    assert storage.size(relative) is None

    key = storage.write_bytes(relative, payload)

    assert key == relative
    assert client.objects[("printforge", relative)] == payload
    assert storage.exists(relative) is True
    assert storage.size(relative) == len(payload)
    assert storage.read_bytes(relative) == payload

    storage.delete(relative)
    assert storage.exists(relative) is False


def test_read_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError):
        storage.read_bytes("missing.bin")


def test_delete_missing_is_a_noop(storage):
    storage.delete("never/existed.stl")  # must not raise


def test_prefix_is_applied_to_keys(client):
    storage = S3Storage(client=client, bucket="printforge", prefix="artifacts/")

    storage.write_bytes("a/b.bin", b"x")

    assert ("printforge", "artifacts/a/b.bin") in client.objects
    assert storage.read_bytes("a/b.bin") == b"x"


def test_non_missing_error_propagates(storage, client):
    class Boom(FakeS3Client):
        def head_object(self, **_kwargs):
            raise RuntimeError("network down")

    broken = S3Storage(client=Boom(), bucket="printforge")

    with pytest.raises(RuntimeError, match="network down"):
        broken.exists("x")


def test_get_storage_returns_s3_backend(settings):
    settings.STORAGE_BACKEND = "s3"
    settings.AWS_STORAGE_BUCKET_NAME = "printforge"

    storage = get_storage()

    assert isinstance(storage, S3Storage)
    assert storage.bucket == "printforge"

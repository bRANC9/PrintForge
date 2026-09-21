"""Unit tests for the storage abstraction (terv.md 3. fejezet, Storage).

Pure filesystem logic: no database, no network. The ``LocalStorage`` class is
exercised in ``tmp_path`` so a MinIO/S3 backend can later implement the same
contract against the same tests.
"""

from __future__ import annotations

import pytest

from files.services import LocalStorage, get_storage


def test_write_read_exists_delete_round_trip(tmp_path):
    storage = LocalStorage(root=tmp_path)
    relative = "projects/1/v1/model.stl"
    payload = b"solid fake\nendsolid fake\n"

    assert storage.exists(relative) is False

    written = storage.write_bytes(relative, payload)

    assert written == tmp_path / relative
    assert written.is_file()
    assert storage.exists(relative) is True
    assert storage.read_bytes(relative) == payload


def test_write_creates_missing_parent_directories(tmp_path):
    storage = LocalStorage(root=tmp_path)
    relative = "a/b/c/d/deep.bin"

    storage.write_bytes(relative, b"x")

    assert (tmp_path / relative).parent.is_dir()
    assert storage.read_bytes(relative) == b"x"


def test_write_overwrites_existing_file(tmp_path):
    storage = LocalStorage(root=tmp_path)
    relative = "artifact.bin"

    storage.write_bytes(relative, b"first")
    storage.write_bytes(relative, b"second")

    assert storage.read_bytes(relative) == b"second"


def test_binary_payload_is_preserved_exactly(tmp_path):
    storage = LocalStorage(root=tmp_path)
    payload = bytes(range(256)) + b"\x00\xff\x10\x80"

    storage.write_bytes("blob", payload)

    assert storage.read_bytes("blob") == payload


def test_delete_removes_file(tmp_path):
    storage = LocalStorage(root=tmp_path)
    relative = "artifact.stl"
    storage.write_bytes(relative, b"data")

    storage.delete(relative)

    assert storage.exists(relative) is False
    assert not (tmp_path / relative).exists()


def test_delete_missing_file_is_a_noop(tmp_path):
    storage = LocalStorage(root=tmp_path)

    storage.delete("never/existed.stl")  # must not raise

    assert storage.exists("never/existed.stl") is False


def test_read_missing_file_raises_file_not_found(tmp_path):
    storage = LocalStorage(root=tmp_path)

    with pytest.raises(FileNotFoundError):
        storage.read_bytes("missing.bin")


def test_path_is_relative_to_the_configured_root(tmp_path):
    storage = LocalStorage(root=tmp_path)

    assert storage.path("x/y.z") == tmp_path / "x" / "y.z"


def test_get_storage_returns_local_backend(settings, tmp_path):
    settings.STORAGE_BACKEND = "local"
    settings.MEDIA_ROOT = str(tmp_path)

    storage = get_storage()

    assert isinstance(storage, LocalStorage)
    assert storage.root == tmp_path


def test_get_storage_rejects_unimplemented_backend(settings):
    settings.STORAGE_BACKEND = "s3"

    with pytest.raises(NotImplementedError, match="s3"):
        get_storage()

import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app
from dropshare_server.storage.base import CommitResult, new_blob_key
from dropshare_server.storage.local import LocalFilesystemBackend


def test_missing_root_is_never_created(tmp_path: Path) -> None:
    missing = tmp_path / "nas-mount"

    with pytest.raises(NotADirectoryError):
        LocalFilesystemBackend(missing)

    assert not missing.exists()


def test_server_refuses_to_start_without_storage_root(
    offline_settings: Settings, tmp_path: Path
) -> None:
    settings = offline_settings.model_copy(update={"storage_local_root": tmp_path / "missing"})

    with pytest.raises(NotADirectoryError), TestClient(create_app(settings)):
        pass


def test_health_check_leaves_no_files_behind(storage_root: Path) -> None:
    LocalFilesystemBackend(storage_root).health_check()

    assert list(storage_root.iterdir()) == []


def test_commit_assembles_chunks_in_order_and_measures_them(storage_root: Path) -> None:
    storage = LocalFilesystemBackend(storage_root)
    upload_id, key = uuid.uuid4(), new_blob_key()
    storage.write_chunk(upload_id, 1, b"world")
    storage.write_chunk(upload_id, 0, b"hello ")
    storage.write_chunk(upload_id, 0, b"hello ")  # a re-sent chunk replaces the first copy

    assert storage.chunk_indexes(upload_id) == {0, 1}
    stored = storage.commit(upload_id, 2, key)

    assert stored == CommitResult(size_bytes=11, sha256=hashlib.sha256(b"hello world").hexdigest())
    assert b"".join(storage.open_range(key, 0, 10)) == b"hello world"
    assert b"".join(storage.open_range(key, 6, 8)) == b"wor"
    # Chunks stay until the caller has recorded the blob, then it discards them.
    assert storage.chunk_indexes(upload_id) == {0, 1}
    assert list(storage.staged_upload_ids()) == [upload_id]
    storage.discard_upload(upload_id)
    assert list(storage.staged_upload_ids()) == []


def test_no_temporary_files_are_left_behind(storage_root: Path) -> None:
    storage = LocalFilesystemBackend(storage_root)
    upload_id, key = uuid.uuid4(), new_blob_key()
    storage.write_chunk(upload_id, 0, b"data")
    storage.commit(upload_id, 1, key)

    leftovers = [path for path in storage_root.rglob("*") if path.name.startswith(".part-")]

    assert leftovers == []


def test_discard_and_delete_tolerate_missing_data(storage_root: Path) -> None:
    storage = LocalFilesystemBackend(storage_root)

    storage.discard_upload(uuid.uuid4())
    storage.delete_blob(new_blob_key())


@pytest.mark.parametrize(
    "key", ["../outside", "blobs/ab/cd/../../x", "/etc/passwd", "blobs/AB/cd/x"]
)
def test_blob_keys_that_are_not_generated_are_refused(storage_root: Path, key: str) -> None:
    storage = LocalFilesystemBackend(storage_root)

    with pytest.raises(ValueError, match="Not a blob key"):
        storage.delete_blob(key)

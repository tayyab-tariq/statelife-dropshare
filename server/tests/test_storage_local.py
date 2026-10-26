from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dropshare_server.core.config import Settings
from dropshare_server.main import create_app
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

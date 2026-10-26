import os
from pathlib import Path

import pytest

from dropshare_client import platform
from dropshare_client.platform.windows.paths import WindowsPaths


def test_current_platform_paths_are_per_user() -> None:
    paths = platform.paths()

    assert paths.app_data_dir().is_relative_to(Path.home())
    assert paths.app_data_dir().parts[-2:] == ("StateLife", "DropShare")
    assert paths.default_sync_root() == Path.home() / "StateLife DropShare"


def test_windows_app_data_is_under_local_app_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    assert WindowsPaths().app_data_dir() == tmp_path / "StateLife" / "DropShare"


def test_windows_app_data_requires_local_app_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    with pytest.raises(RuntimeError):
        WindowsPaths().app_data_dir()


@pytest.mark.windows_only
def test_windows_paths_on_real_windows() -> None:
    paths = platform.paths()

    assert isinstance(paths, WindowsPaths)
    assert paths.app_data_dir() == Path(os.environ["LOCALAPPDATA"]) / "StateLife" / "DropShare"
    assert paths.default_sync_root() == Path(os.environ["USERPROFILE"]) / "StateLife DropShare"

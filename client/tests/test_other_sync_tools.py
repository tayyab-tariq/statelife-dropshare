"""Other sync tools' folders, wherever they are, can't hold or be inside the sync folder
(FR-SYNC-01)."""

import json
import sys
from pathlib import Path

import pytest

from dropshare_client.platform.windows.volumes import WindowsVolumes, dropbox_roots


def _info(folder: Path, content: str) -> None:
    (folder / "Dropbox").mkdir(parents=True)
    (folder / "Dropbox" / "info.json").write_text(content, encoding="utf-8")


def test_dropbox_folders_are_found_where_the_user_put_them(tmp_path: Path) -> None:
    roaming, local = tmp_path / "Roaming", tmp_path / "Local"
    _info(roaming, json.dumps({"personal": {"path": r"D:\My Dropbox", "host": 1}}))
    _info(
        local,
        json.dumps(
            {"business": {"path": r"E:\Dropbox (Work)"}, "personal": {"path": r"D:\My Dropbox"}}
        ),
    )

    roots = dropbox_roots({"APPDATA": str(roaming), "LOCALAPPDATA": str(local)})

    assert roots == [Path(r"D:\My Dropbox"), Path(r"E:\Dropbox (Work)")]


@pytest.mark.parametrize("content", ["{not json", "[]", '{"personal": "D:/x"}', '{"a": {}}'])
def test_unreadable_dropbox_settings_are_skipped(tmp_path: Path, content: str) -> None:
    _info(tmp_path, content)

    assert dropbox_roots({"APPDATA": str(tmp_path)}) == []


def test_without_dropbox_there_is_nothing_to_avoid(tmp_path: Path) -> None:
    assert dropbox_roots({"APPDATA": str(tmp_path)}) == []
    assert dropbox_roots({}) == []


@pytest.mark.windows_only
def test_other_sync_roots_include_dropbox_on_windows() -> None:
    assert sys.platform == "win32"
    roots = WindowsVolumes().other_sync_roots()
    assert all(isinstance(root, Path) for root in roots)

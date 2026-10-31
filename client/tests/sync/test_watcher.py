"""The watcher turns file-system events into paths to look at (algorithm.md §1)."""

import threading
import time
from pathlib import Path

import pytest

from dropshare_client.sync.watcher import Watcher

TIMEOUT = 5.0


class Seen:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self._changed = threading.Condition()

    def __call__(self, path: str) -> None:
        with self._changed:
            self.paths.append(path)
            self._changed.notify_all()

    def wait_for(self, path: str) -> bool:
        deadline = time.monotonic() + TIMEOUT
        with self._changed:
            while path not in self.paths:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._changed.wait(remaining)
            return True


def test_reports_changed_paths_relative_to_the_root(tmp_path: Path) -> None:
    root = tmp_path / "DropShare"
    (root / "Docs").mkdir(parents=True)
    seen = Seen()
    watcher = Watcher(root, seen)
    watcher.start()
    try:
        (root / ".dropshare-tmp").mkdir()
        (root / ".dropshare-tmp" / "x.part").write_bytes(b"partial")
        (root / "Docs" / "a.txt").write_bytes(b"a")

        assert seen.wait_for("Docs/a.txt")
    finally:
        watcher.stop()
    assert not any(path.startswith(".dropshare-tmp") for path in seen.paths)


def test_can_be_started_again_after_stopping(tmp_path: Path) -> None:
    """The engine restarts it when it isn't running (e.g. the sync folder's drive came back,
    audit S19); a stopped observer thread can't restart, so a new one must."""
    root = tmp_path / "DropShare"
    root.mkdir()
    seen = Seen()
    watcher = Watcher(root, seen)
    watcher.start()
    watcher.stop()
    assert not watcher.alive

    watcher.start()
    try:
        assert watcher.alive
        (root / "back.txt").write_bytes(b"x")
        assert seen.wait_for("back.txt")
    finally:
        watcher.stop()
    assert not watcher.alive


def test_a_sync_folder_reached_through_a_link_still_reports_changes(tmp_path: Path) -> None:
    """Events may name the folder by its resolved path (/var -> /private/var on macOS, a
    junction on Windows); they must not be dropped, or changes wait for the next full scan
    (NFR-PERF-02)."""
    real = tmp_path / "real" / "DropShare"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:  # Windows without Developer Mode: covered by the Windows check (junction)
        pytest.skip("this account can't create symbolic links")
    seen = Seen()
    watcher = Watcher(link / "DropShare", seen)
    watcher.start()
    try:
        (real / "a.txt").write_bytes(b"a")

        assert seen.wait_for("a.txt")
    finally:
        watcher.stop()

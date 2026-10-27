"""The watcher turns file-system events into paths to look at (algorithm.md §1)."""

import threading
import time
from pathlib import Path

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

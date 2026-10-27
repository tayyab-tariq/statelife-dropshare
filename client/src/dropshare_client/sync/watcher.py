"""File-system events as hints (algorithm.md §1): which paths to look at, never what changed.

Events can be dropped (ReadDirectoryChangesW overflows) or arrive out of order, so the engine
debounces them and the periodic full scan catches anything missed (windows-fs.md).
"""

import logging
import os
from collections.abc import Callable
from pathlib import Path

from watchdog.events import (
    EVENT_TYPE_CLOSED_NO_WRITE,
    EVENT_TYPE_MODIFIED,
    EVENT_TYPE_OPENED,
    FileSystemEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from dropshare_client.sync import paths

logger = logging.getLogger(__name__)

_JOIN_SECONDS = 5.0


class _Handler(FileSystemEventHandler):
    def __init__(self, root: Path, on_change: Callable[[str], None]) -> None:
        self._root = root
        self._on_change = on_change

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in (EVENT_TYPE_OPENED, EVENT_TYPE_CLOSED_NO_WRITE):
            return
        # A folder "modified" event only says something inside changed, and that has its
        # own event; following it would rescan the whole folder on every save.
        if event.is_directory and event.event_type == EVENT_TYPE_MODIFIED:
            return
        for raw in (event.src_path, event.dest_path):
            if raw:
                self._report(os.fsdecode(raw))

    def _report(self, absolute: str) -> None:
        try:
            relative = Path(absolute).relative_to(self._root).as_posix()
        except ValueError:
            return
        path = "" if relative == "." else relative
        if path.split("/", 1)[0] == paths.TEMP_DIR:
            return
        self._on_change(path)


class Watcher:
    def __init__(self, root: Path, on_change: Callable[[str], None]) -> None:
        self._root = root
        self._handler = _Handler(root, on_change)
        self._observer: BaseObserver | None = None

    @property
    def alive(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    def start(self) -> None:
        """OSError if the folder can't be watched (e.g. it doesn't exist). Can be called
        again after the watcher stopped (a thread can't be restarted, so a new one starts)."""
        self.stop()
        observer = Observer()
        observer.daemon = True
        observer.schedule(self._handler, str(self._root), recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None and observer.is_alive():
            observer.stop()
            observer.join(_JOIN_SECONDS)

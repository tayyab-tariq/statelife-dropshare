"""The Activity view (FR-CLIENT-03) and the Can't sync view (FR-SYNC-07): what the sync engine
did and what it can't do, read from the local index on the pool and kept up to date from the
engine's reports. Opening or showing an item checks it on the pool first."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.local_files import LocalFiles
from dropshare_client.store.records import Activity, Problem
from dropshare_client.sync import status

logger = logging.getLogger(__name__)

ACTIVITY_SHOWN = 200  # the newest events; the index keeps up to 1,000

# Events whose item is (or should still be) at its path; the others are gone from there.
_AT_PATH = frozenset(
    {
        "uploaded",
        "downloaded",
        "folder_added",
        "conflict",
        "moved_here",
        "moved_on_server",
        "cant_sync",
        "kept_shared_folder",
        "move_refused",
        "restored_view_only",
    }
)


@dataclass(frozen=True)
class ActivityRow:
    at: datetime
    what: str
    path: str  # in the sync folder, "/"-separated
    detail: str
    can_open: bool


@dataclass(frozen=True)
class CantSyncRow:
    key: str
    path: str
    reason: str  # what is wrong and how to fix it


def _activity_row(item: Activity) -> ActivityRow:
    what, detail = messages.activity_text(item.kind, item.detail)
    return ActivityRow(item.at, what, item.path, detail, item.kind in _AT_PATH)


class _LocalItems(QObject):
    """Open or show an item of the sync folder, checked on the pool."""

    open_file = Signal(object)  # Path: the view opens it with its program
    action_failed = Signal(str)

    def __init__(
        self,
        config: ConfigStore,
        local_files: Callable[[Path], LocalFiles],
        worker: Worker | None,
    ) -> None:
        super().__init__()
        self._config = config
        self._local_files = local_files
        self._worker = worker or Worker()

    def _files(self) -> LocalFiles | None:
        root = self._config.config.sync_root
        if root is None:
            self.action_failed.emit(messages.NO_SYNC_FOLDER_YET)
            return None
        return self._local_files(root)

    def open(self, path: str) -> None:
        files = self._files()
        if files is not None:
            self._worker.submit(lambda: files.locate(path), self.open_file.emit, self._failed)

    def show(self, path: str) -> None:
        files = self._files()
        if files is not None:
            self._worker.submit(lambda: files.show(path), lambda _: None, self._failed)

    def _failed(self, exc: Exception) -> None:
        logger.info("Couldn't open or show an item: %s", type(exc).__name__)
        self.action_failed.emit(messages.message_for(exc))


class ActivityController(_LocalItems):
    loading = Signal()
    loaded = Signal()  # read rows()
    failed = Signal(str)  # nothing to show: the view shows the error state
    reload_failed = Signal(str)  # the list is kept; the next reload that works clears it

    def __init__(
        self,
        config: ConfigStore,
        sync: SyncController,
        index_path: Path,
        local_files: Callable[[Path], LocalFiles],
        worker: Worker | None = None,
    ) -> None:
        super().__init__(config, local_files, worker)
        self._index_path = index_path
        self._rows: list[ActivityRow] = []
        self._loaded_once = False
        self._generation = 0  # changes at sign-out: late answers are dropped
        sync.activity_added.connect(self._added)

    def rows(self) -> list[ActivityRow]:
        return list(self._rows)

    def refresh(self) -> None:
        generation = self._generation
        index_path = self._index_path
        if not self._loaded_once:
            self.loading.emit()
        self._worker.submit(
            lambda: status.recent_activity(index_path, ACTIVITY_SHOWN),
            lambda items: self._loaded(generation, items),
            lambda exc: self._load_failed(generation, exc),
        )

    def clear(self) -> None:
        """Signed out: the next account's activity isn't this one's."""
        self._generation += 1
        self._rows = []
        self._loaded_once = False
        self.loaded.emit()

    def _loaded(self, generation: int, items: list[Activity]) -> None:
        if generation == self._generation:
            self._rows = [_activity_row(item) for item in items]
            self._loaded_once = True
            self.loaded.emit()

    def _load_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        logger.info("Couldn't read the activity: %s", type(exc).__name__)
        if self._loaded_once:
            self.reload_failed.emit(messages.message_for(exc))
        else:
            self.failed.emit(messages.message_for(exc))

    def _added(self, item: Activity) -> None:
        if self._loaded_once:  # otherwise the next load reads it from the index
            self._rows = [_activity_row(item), *self._rows][:ACTIVITY_SHOWN]
            self.loaded.emit()


class CantSyncController(_LocalItems):
    loading = Signal()
    loaded = Signal()  # read rows()
    failed = Signal(str)
    reload_failed = Signal(str)  # the list is kept; the next reload that works clears it
    retried = Signal(str)  # a short confirmation for the status line

    def __init__(
        self,
        config: ConfigStore,
        sync: SyncController,
        index_path: Path,
        local_files: Callable[[Path], LocalFiles],
        worker: Worker | None = None,
    ) -> None:
        super().__init__(config, local_files, worker)
        self._sync = sync
        self._index_path = index_path
        self._rows: list[CantSyncRow] = []
        self._loaded_once = False
        self._generation = 0
        sync.problems_changed.connect(self._changed)

    def rows(self) -> list[CantSyncRow]:
        return list(self._rows)

    def refresh(self) -> None:
        generation = self._generation
        index_path = self._index_path
        if not self._loaded_once:
            self.loading.emit()
        self._worker.submit(
            lambda: status.cant_sync(index_path),
            lambda problems: self._loaded(generation, problems),
            lambda exc: self._load_failed(generation, exc),
        )

    def clear(self) -> None:
        self._generation += 1
        self._rows = []
        self._loaded_once = False
        self.loaded.emit()

    def retry(self, rows: list[CantSyncRow]) -> None:
        """Look at these items again now, e.g. after renaming or freeing space; one that
        still can't be synced stays listed."""
        for row in rows:
            self._sync.retry(row.key)
        if rows:
            self.retried.emit(messages.RETRYING)

    def _loaded(self, generation: int, problems: list[Problem]) -> None:
        if generation == self._generation:
            self._set(problems)

    def _load_failed(self, generation: int, exc: Exception) -> None:
        if generation != self._generation:
            return
        logger.info("Couldn't read what can't be synced: %s", type(exc).__name__)
        if self._loaded_once:
            self.reload_failed.emit(messages.message_for(exc))
        else:
            self.failed.emit(messages.message_for(exc))

    def _changed(self, problems: list[Problem]) -> None:
        self._generation += 1  # newer than any read still running
        self._set(problems)

    def _set(self, problems: list[Problem]) -> None:
        self._rows = [CantSyncRow(p.key, p.path, p.reason) for p in problems]
        self._loaded_once = True
        self.loaded.emit()

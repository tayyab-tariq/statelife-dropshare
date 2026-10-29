"""The file browser's actions (FR-FILE-03/04/05): new folder, rename, move, delete, save a
copy and show in folder. Each acts on the local sync folder (OQ-23) on the worker pool, one at
a time; the sync engine is told at once so the change reaches the server without waiting
for the watcher.

Each item comes with the node the list showed there. Before acting, the worker checks in the
sync index that the path still belongs to that node: if another device renamed it and put
something else at its old name, the action is refused ("changed meanwhile") rather than done
to the wrong item (FR-FILE-04: stale data is a conflict, and the list refreshes).
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.errors import FileActionError, FileProblem, InvalidInputError
from dropshare_client.services.local_files import LocalFiles
from dropshare_client.sync.status import synced

logger = logging.getLogger(__name__)

# The sync folder -> the service for it (app.py wires the platform's Recycle Bin and reveal).
type LocalFilesFactory = Callable[[Path], LocalFiles]


@dataclass(frozen=True)
class Item:
    """An item as the list showed it: its local path and the server node it was."""

    path: str
    node_id: uuid.UUID


class FileActionsController(QObject):
    busy_changed = Signal(bool)
    succeeded = Signal(str)  # a short confirmation for the view's status line
    failed = Signal(str)  # what went wrong and what to do
    stale = Signal()  # an item wasn't what the list showed: refresh it

    def __init__(
        self,
        config: ConfigStore,
        local_files: LocalFilesFactory,
        sync: SyncController,
        index_path: Path,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._local_files = local_files
        self._sync = sync
        self._index_path = index_path
        self._worker = worker or Worker()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def sync_root(self) -> Path | None:
        return self._config.config.sync_root

    def new_folder(self, parent: Item | None, name: str) -> None:
        """In `parent`, or at the top of the sync folder for None."""
        checked = [parent] if parent is not None else []
        where = parent.path if parent is not None else ""
        self._run(checked, lambda files: [files.new_folder(where, name)], messages.FOLDER_CREATED)

    def rename(self, item: Item, name: str) -> None:
        self._run(
            [item], lambda files: [item.path, files.rename(item.path, name)], messages.RENAMED
        )

    def move(self, items: list[Item], folder: Item | None) -> None:
        """Into `folder`, or the top of the sync folder for None."""
        paths = [i.path for i in items]
        where = folder.path if folder is not None else ""
        checked = [*items, folder] if folder is not None else items
        self._run(
            checked, lambda files: [*paths, *files.move(paths, where)], messages.moved(len(items))
        )

    def move_to_disk_folder(self, items: list[Item], folder: Path) -> None:
        """ "Move to…" with a folder chosen on disk; it must be inside the sync folder."""
        paths = [i.path for i in items]
        self._run(
            items,
            lambda files: [*paths, *files.move(paths, files.relative(folder))],
            messages.moved(len(items)),
        )

    def delete(self, items: list[Item]) -> None:
        def recycle(files: LocalFiles) -> list[str]:
            files.delete([i.path for i in items])
            return [i.path for i in items]

        self._run(items, recycle, messages.recycled(len(items)))

    def save_copy(self, item: Item, destination: Path) -> None:
        def copy(files: LocalFiles) -> list[str]:
            files.save_copy(item.path, destination)
            return []

        self._run([item], copy, messages.COPY_SAVED)

    def show(self, item: Item) -> None:
        def reveal(files: LocalFiles) -> list[str]:
            files.show(item.path)
            return []

        self._run([item], reveal, "")

    def _run(
        self,
        items: list[Item],
        action: Callable[[LocalFiles], list[str]],
        confirmation: str,
    ) -> None:
        root = self.sync_root
        if self._busy:
            self.failed.emit(messages.ACTION_ALREADY_RUNNING)
            return
        if root is None:
            self.failed.emit(messages.NO_SYNC_FOLDER_YET)
            return
        files = self._local_files(root)

        def checked_then_done() -> list[str]:
            self._check_unchanged(items)
            return action(files)

        self._set_busy(True)
        self._worker.submit(
            checked_then_done,
            lambda changed: self._done(changed, confirmation),
            self._failed,
        )

    def _check_unchanged(self, items: list[Item]) -> None:
        """On the worker: each path still belongs to the node the list showed there."""
        entries = synced(self._index_path, (i.node_id for i in items))
        for item in items:
            entry = entries.get(item.node_id)
            if entry is None or entry.path != item.path:
                raise FileActionError(FileProblem.CHANGED)

    def _done(self, changed: list[str], confirmation: str) -> None:
        self._set_busy(False)
        if changed:
            self._sync.changed_here(changed)
        if confirmation:
            self.succeeded.emit(confirmation)

    def _failed(self, exc: Exception) -> None:
        self._set_busy(False)
        if not isinstance(exc, InvalidInputError):
            logger.error("A file action failed", exc_info=exc)
        self.failed.emit(messages.message_for(exc))
        if isinstance(exc, FileActionError) and exc.problem is FileProblem.CHANGED:
            self.stale.emit()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)

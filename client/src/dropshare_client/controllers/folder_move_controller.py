"""Moving the sync folder from Settings (FR-CLIENT-04): checks the new place, holds and stops
syncing, moves the files on the pool (sync/folder_move.py), and lets syncing start again from
wherever the folder is then. A move that needs attention (something stayed locked) keeps syncing
held: the engine would see the missing items as deleted. At start, an interrupted move is
finished or undone on the pool before syncing may start."""

import logging
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.worker import Worker
from dropshare_client.platform.base import Volumes
from dropshare_client.services import sync_folder
from dropshare_client.services.config import ConfigStore
from dropshare_client.sync.folder_move import (
    FolderMover,
    MoveAttentionError,
    MoveError,
    MoveOutcome,
)
from dropshare_shared.schemas.users import UserOut

logger = logging.getLogger(__name__)


class FolderMoveController(QObject):
    checked = Signal(str, str)  # path, problem ("" when it can be used)
    started = Signal()
    progressed = Signal(object, object)  # bytes copied, bytes in all (Signal(int) wraps)
    moved = Signal(str)  # what happened, for the user
    failed = Signal(str)  # why not; the folder is where it was
    attention = Signal(str)  # syncing stays paused until DropShare restarts (see module doc)
    state_changed = Signal()  # `busy` or `needs_attention` changed

    def __init__(
        self,
        mover: FolderMover,
        sync: SyncController,
        config: ConfigStore,
        volumes: Volumes,
        app_data: Path,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._mover = mover
        self._sync = sync
        self._config = config
        self._volumes = volumes
        self._app_data = app_data
        self._worker = worker or Worker()
        self._cancel = threading.Event()
        self._busy = False
        self._user: UserOut | None = None  # whose syncing starts again afterwards

    @property
    def busy(self) -> bool:
        return self._busy

    @property
    def needs_attention(self) -> bool:
        """A move is unfinished (the next start of DropShare tries again)."""
        return self._mover.pending

    @property
    def current_folder(self) -> str:
        root = self._config.config.sync_root
        return "" if root is None else str(root)

    def recover_at_start(self) -> None:
        """Before anything syncs: finish or undo a move a crash interrupted."""
        if not self._mover.pending:
            return
        self._sync.hold()
        self._busy = True
        self.state_changed.emit()
        self._worker.submit(self._mover.recover, self._recovered, self._recovery_failed)

    def check(self, path: str) -> None:
        self._worker.submit(
            lambda: self._problem(Path(path)) or "",
            lambda problem: self.checked.emit(path, problem),
            lambda exc: self.checked.emit(path, messages.message_for(exc)),
        )

    def move(self, path: str, user: UserOut) -> None:
        source = self._config.config.sync_root
        if self._busy or source is None:
            return
        self._busy = True
        self._user = user
        self._cancel.clear()
        self._sync.hold()  # from here until it's done, nothing starts syncing
        target = Path(path)
        self.started.emit()
        self.state_changed.emit()

        def checked(problem: str | None) -> None:
            if problem:
                self._finished(problem, failed=True)
            else:
                self._sync.stop(then=lambda: self._run(source, target))

        self._worker.submit(
            lambda: self._problem(target),
            checked,
            lambda exc: self._finished(messages.message_for(exc), failed=True),
        )

    def signed_out(self) -> None:
        """The session ended: a move still running finishes, but syncing doesn't restart."""
        self._user = None

    def cancel(self) -> None:
        """Stops a copy between files; everything is put back."""
        self._cancel.set()

    def _problem(self, target: Path) -> str | None:
        source = self._config.config.sync_root
        if source is not None and sync_folder.same_folder(source, target):
            return messages.MOVE_FOLDER_SAME
        if source is not None and sync_folder.nested(source, target):
            return messages.move_problem("nested")
        return sync_folder.problem_with(target, self._volumes, self._app_data)

    def _run(self, source: Path, target: Path) -> None:
        if not self._sync.released:  # the old engine is still at work in the folder
            self._finished(messages.move_problem("busy"), failed=True)
            return
        self._worker.submit(
            lambda: self._mover.move(
                source, target, self._cancel, lambda done, total: self.progressed.emit(done, total)
            ),
            self._moved,
            self._move_failed,
        )

    def _moved(self, outcome: MoveOutcome) -> None:
        kept = str(outcome.old_kept) if outcome.old_kept is not None else None
        self._finished(messages.folder_moved(str(outcome.root), kept), failed=False)

    def _move_failed(self, exc: Exception) -> None:
        if isinstance(exc, MoveError):
            self._finished(messages.move_problem(exc.problem.value, exc.needed), failed=True)
        elif isinstance(exc, MoveAttentionError) or self._mover.pending:
            self._needs_attention(exc)
        else:
            logger.error("Moving the sync folder failed", exc_info=exc)
            self._finished(messages.move_problem(""), failed=True)

    def _finished(self, message: str, *, failed: bool) -> None:
        """Done, one way or the other, with everything in one place: syncing may start."""
        self._busy = False
        user, self._user = self._user, None
        self._sync.release()
        if user is not None:
            self._sync.start(user)  # from wherever the folder is now
        self.state_changed.emit()
        (self.failed if failed else self.moved).emit(message)

    def _needs_attention(self, exc: Exception) -> None:
        """Syncing stays held: the next start of DropShare tries to put things right."""
        logger.error("A sync folder move needs attention", exc_info=exc)
        self._busy = False
        self._user = None
        self.state_changed.emit()
        if isinstance(exc, MoveAttentionError) and exc.unreadable:
            self.attention.emit(messages.move_record_unreadable(str(exc.folder)))
            return
        folder = exc.folder if isinstance(exc, MoveAttentionError) else self._app_data
        self.attention.emit(messages.move_attention(str(folder)))

    def _recovered(self, outcome: MoveOutcome | None) -> None:
        self._busy = False
        self._sync.release()  # a start asked for meanwhile goes ahead
        self.state_changed.emit()
        if outcome is None:
            return
        logger.warning("Finished an interrupted sync folder move (moved: %s)", outcome.moved)
        if outcome.moved:
            kept = str(outcome.old_kept) if outcome.old_kept is not None else None
            self.moved.emit(messages.folder_moved(str(outcome.root), kept))
        else:
            self.moved.emit(messages.move_undone(str(outcome.root)))

    def _recovery_failed(self, exc: Exception) -> None:
        self._needs_attention(exc)

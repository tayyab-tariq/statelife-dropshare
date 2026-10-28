"""The sync engine for the UI (architecture.md §3): starts and stops it, and turns its
callbacks, which arrive on the engine's threads, into signals on the GUI thread."""

import logging
import threading
import uuid
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, Qt, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.config import ConfigStore
from dropshare_client.sync.binding import ForeignFolderError
from dropshare_client.sync.engine import SyncListener, SyncProblem, SyncState
from dropshare_client.sync.transfers import TransferEvent
from dropshare_shared.schemas.users import UserOut

logger = logging.getLogger(__name__)

_QUIT_SECONDS = 10.0
_QUIT_WAIT_MS = 15_000
_PROBLEMS = {
    SyncProblem.ROOT_MISSING: messages.SYNC_FOLDER_MISSING,
    SyncProblem.NO_PERSONAL_FOLDER: messages.NO_PERSONAL_FOLDER,
    SyncProblem.UNEXPECTED: messages.SYNC_PROBLEM,
}


class SyncStatus(StrEnum):
    """What the UI shows; several engine states read the same to a user."""

    SYNCING = "syncing"
    UP_TO_DATE = "up_to_date"
    PAUSED = "paused"
    OFFLINE = "offline"
    NEEDS_CONFIRMATION = "needs_confirmation"
    ERROR = "error"
    UPGRADE_REQUIRED = "upgrade_required"
    STOPPED = "stopped"


_STATUS = {
    SyncState.STARTING: SyncStatus.SYNCING,
    SyncState.SCANNING: SyncStatus.SYNCING,
    SyncState.SYNCING: SyncStatus.SYNCING,
    SyncState.UP_TO_DATE: SyncStatus.UP_TO_DATE,
    SyncState.PAUSED: SyncStatus.PAUSED,
    SyncState.OFFLINE: SyncStatus.OFFLINE,
    SyncState.NEEDS_CONFIRMATION: SyncStatus.NEEDS_CONFIRMATION,
    SyncState.UPGRADE_REQUIRED: SyncStatus.UPGRADE_REQUIRED,
    SyncState.ERROR: SyncStatus.ERROR,
    SyncState.STOPPED: SyncStatus.STOPPED,
}


class Engine(Protocol):
    def start(self) -> None: ...

    def stop(self, timeout: float = ...) -> bool: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def confirm_deletes(self, approved: bool) -> None: ...

    def retry_now(self, key: str) -> None: ...


# (sync folder, user ID, display name, server URL, listener) -> an engine, not yet started.
# Runs on the pool; raises ForeignFolderError if the folder holds another account's files.
type EngineFactory = Callable[[Path, uuid.UUID, str, str, SyncListener], Engine]


class _Phase(StrEnum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class _Relay(QObject):
    """One engine's SyncListener. Called on engine threads; its signals are connected queued,
    so the controller handles them on the GUI thread, where this object lives."""

    state_reported = Signal(str, str)  # SyncState value, SyncProblem value or ""
    conflict_reported = Signal(str, str)
    deletes_reported = Signal(int, int)
    expiry_reported = Signal()
    progress_reported = Signal(int)
    transfer_reported = Signal(object)  # TransferEvent

    def state_changed(self, state: SyncState, problem: SyncProblem | None) -> None:
        self.state_reported.emit(state.value, problem.value if problem else "")

    def conflict(self, path: str, copy_path: str) -> None:
        self.conflict_reported.emit(path, copy_path)

    def confirm_deletes(self, here: int, on_server: int) -> None:
        self.deletes_reported.emit(here, on_server)

    def session_expired(self) -> None:
        self.expiry_reported.emit()

    def progress(self, pending: int) -> None:
        self.progress_reported.emit(pending)

    def transfer(self, event: TransferEvent) -> None:
        self.transfer_reported.emit(event)


class SyncController(QObject):
    status_changed = Signal(str, str)  # SyncStatus value, message ("" when all is well)
    conflict = Signal(str)  # a notification for the tray (OQ-15 until the Activity view)
    deletes_need_confirmation = Signal(str)  # what would be deleted (FR-SYNC-08)
    session_expired = Signal(str)
    folder_rejected = Signal(str)  # the folder holds another account's files
    start_failed = Signal(str)  # syncing couldn't start; offer to try again
    transfer_reported = Signal(object)  # TransferEvent, for the Transfers view (FR-CLIENT-08)

    def __init__(
        self,
        config: ConfigStore,
        make_engine: EngineFactory,
        forget_index: Callable[[], None],
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._make_engine = make_engine
        self._forget_index = forget_index
        self._worker = worker or Worker()
        self._phase = _Phase.IDLE
        self._engine: Engine | None = None
        self._generation = 0  # reports from older engines are dropped
        self._relay: _Relay | None = None
        self._paused = False
        self._last_status = (SyncStatus.STOPPED, "")
        self._stop_requested: list[Callable[[], None]] = []  # waiting for a start to finish
        self._start_requested: UserOut | None = None  # waiting for a stop to finish
        # The engine the pool started, until the GUI thread hears of it (for shutdown).
        self._opened: Engine | None = None
        self._opened_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self._phase is not _Phase.IDLE

    def start(self, user: UserOut) -> None:
        """Start syncing the configured folder for `user`, if it isn't running already."""
        root = self._config.config.sync_root
        if root is None or self._phase in (_Phase.STARTING, _Phase.RUNNING):
            return
        if self._phase is _Phase.STOPPING:
            self._start_requested = user  # after the old engine has let go of the index
            return
        self._phase = _Phase.STARTING
        self._generation += 1
        generation = self._generation
        relay = self._new_relay(generation)
        self.status_changed.emit(SyncStatus.SYNCING, "")
        server_url = self._config.config.server_url
        paused = self._paused
        self._worker.submit(
            lambda: self._open(root, user, server_url, relay, paused),
            lambda engine: self._started(engine, paused),
            lambda exc: self._start_failed(exc),
        )

    def _new_relay(self, generation: int) -> _Relay:
        relay = _Relay()
        queued = Qt.ConnectionType.QueuedConnection
        relay.state_reported.connect(
            lambda state, problem: self._state_reported(generation, state, problem), queued
        )
        relay.conflict_reported.connect(
            lambda path, copy: self._conflict_reported(generation, path, copy), queued
        )
        relay.deletes_reported.connect(
            lambda here, there: self._deletes_reported(generation, here, there), queued
        )
        relay.expiry_reported.connect(lambda: self._expired(generation), queued)
        relay.progress_reported.connect(
            lambda pending: self._progress_reported(generation, pending), queued
        )
        relay.transfer_reported.connect(
            lambda event: self._transfer_reported(generation, event), queued
        )
        self._relay = relay
        return relay

    def _open(
        self, root: Path, user: UserOut, server_url: str, relay: _Relay, paused: bool
    ) -> Engine:
        """On the pool: check the folder is this account's, open the index, start."""
        engine = self._make_engine(root, user.id, user.display_name, server_url, relay)
        if paused:
            engine.pause()
        engine.start()
        with self._opened_lock:
            self._opened = engine
        return engine

    def _started(self, engine: Engine, paused_at_start: bool) -> None:
        with self._opened_lock:
            self._opened = None
        self._phase = _Phase.RUNNING
        self._engine = engine
        if self._paused != paused_at_start:  # the user paused or resumed while it started
            engine.pause() if self._paused else engine.resume()
        pending, self._stop_requested = self._stop_requested, []
        for stop in pending:
            stop()

    def _start_failed(self, exc: Exception) -> None:
        self._phase = _Phase.IDLE
        pending, self._stop_requested = self._stop_requested, []
        if pending:  # signed out meanwhile: nothing to show
            for stop in pending:
                stop()
            return
        if isinstance(exc, ForeignFolderError):
            self.folder_rejected.emit(messages.OTHER_ACCOUNT_FOLDER)
            return
        logger.error("Couldn't start syncing", exc_info=exc)
        self.start_failed.emit(messages.message_for(exc))

    def pause(self) -> None:
        self._paused = True
        if self._engine is not None:
            self._engine.pause()
        else:
            self.status_changed.emit(SyncStatus.PAUSED, "")

    def resume(self) -> None:
        self._paused = False
        if self._engine is not None:
            self._engine.resume()

    def retry(self, key: str) -> None:
        """Try a transfer again now: the Transfers view's Retry (FR-CLIENT-08)."""
        if self._engine is not None:
            self._engine.retry_now(key)

    def answer_deletes(self, approved: bool) -> None:
        if not approved:
            self._paused = True
        if self._engine is not None:
            self._engine.confirm_deletes(approved)

    def stop(self, *, forget: bool = False, then: Callable[[], object] | None = None) -> None:
        """Stop syncing off the GUI thread, then call `then`. With `forget`, the index goes
        too (the files were removed at sign-out); it is kept if the engine didn't stop in
        time, since it is still using it."""
        if self._phase is _Phase.STARTING:
            self._stop_requested.append(lambda: self.stop(forget=forget, then=then))
            return
        if self._phase is _Phase.STOPPING:
            self._stop_requested.append(lambda: self.stop(forget=forget, then=then))
            return
        engine, self._engine = self._engine, None
        self._phase = _Phase.STOPPING
        self._generation += 1  # whatever it still reports is out of date

        def stop_engine() -> bool:
            stopped = engine is None or engine.stop()
            if not stopped:
                logger.warning("The sync engine didn't stop in time")
            elif forget:
                self._forget_index()
            return stopped

        def done(_: object) -> None:
            self._phase = _Phase.IDLE
            self._emit_status(SyncStatus.STOPPED, "")
            if then is not None:
                then()
            queued_stops, self._stop_requested = self._stop_requested, []
            for stop in queued_stops:
                stop()
            user, self._start_requested = self._start_requested, None
            if user is not None and self._phase is _Phase.IDLE:
                self.start(user)

        def failed(exc: Exception) -> None:
            logger.error("Stopping sync failed", exc_info=exc)
            done(None)

        self._worker.submit(stop_engine, done, failed)

    def shutdown(self) -> None:
        """At quit, after the event loop has ended: let started work finish, then stop
        whatever engine is running, here and now."""
        self._worker.wait(_QUIT_WAIT_MS)
        with self._opened_lock:
            engine = self._engine or self._opened
            self._opened = None
        self._engine = None
        self._phase = _Phase.IDLE
        if engine is not None and not engine.stop(_QUIT_SECONDS):
            logger.warning("Quit while the sync engine was still busy")

    # --- from the engine -----------------------------------------------------------------

    def _current(self, generation: int) -> bool:
        return generation == self._generation and self._phase is not _Phase.IDLE

    def _state_reported(self, generation: int, state: str, problem: str) -> None:
        if not self._current(generation):
            return
        status = _STATUS[SyncState(state)]
        if status is SyncStatus.ERROR:
            message = _PROBLEMS.get(SyncProblem(problem), messages.SYNC_PROBLEM) if problem else ""
        elif status is SyncStatus.UPGRADE_REQUIRED:
            message = messages.UPGRADE
        else:
            message = ""
        self._emit_status(status, message)

    def _progress_reported(self, generation: int, pending: int) -> None:
        if self._current(generation) and self._last_status[0] is SyncStatus.SYNCING:
            self._emit_status(SyncStatus.SYNCING, messages.items_left(pending))

    def _transfer_reported(self, generation: int, event: TransferEvent) -> None:
        if self._current(generation):
            self.transfer_reported.emit(event)

    def _emit_status(self, status: SyncStatus, message: str) -> None:
        self._last_status = (status, message)
        self.status_changed.emit(status, message)

    def _conflict_reported(self, generation: int, path: str, copy_path: str) -> None:
        if self._current(generation):
            self.conflict.emit(messages.conflict_message(path, copy_path))

    def _deletes_reported(self, generation: int, here: int, on_server: int) -> None:
        if self._current(generation):
            self.deletes_need_confirmation.emit(messages.mass_delete_message(here, on_server))

    def _expired(self, generation: int) -> None:
        if self._current(generation):
            self.stop(then=lambda: self.session_expired.emit(messages.SESSION_ENDED))

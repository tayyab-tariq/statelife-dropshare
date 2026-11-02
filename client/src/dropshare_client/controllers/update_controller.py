"""Updates in the app (FR-UPD-03, FR-UPD-04, FR-CLIENT-02, OQ-41).

Checks at sign-in and every 6 hours, at once when the server refuses this version, and when the
user asks. A newer release downloads in the background; once verified the user installs it now
or at the next start. Network, hashing and signature checks run on the pool. Installing now
quits the app first, so syncing stops at a safe point before the installer runs (`app.main`).

A ready update is checked again against what the server offers (before installing it now, and
at each later check), so a release withdrawn meanwhile isn't installed (S6). Everything resets
at sign-out: the next account may be on another channel (OQ-49), and results still arriving
for the previous one are dropped.
"""

import logging
import threading
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QObject, QTimer, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.errors import ApiError, OfflineError, TransferCancelledError
from dropshare_client.services.files import Progress
from dropshare_client.services.updater import (
    ReadyUpdate,
    UpdateDeferredError,
    UpdateVerificationError,
)
from dropshare_shared.schemas.releases import ReleaseOut

logger = logging.getLogger(__name__)

CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000  # FR-UPD-03


class UpdateState(StrEnum):
    IDLE = "idle"  # not checked yet
    CHECKING = "checking"
    UP_TO_DATE = "up_to_date"
    DOWNLOADING = "downloading"
    READY = "ready"  # downloaded and verified
    INSTALLING = "installing"  # checked once more, then the app quits
    FAILED = "failed"
    FROM_SOURCE = "from_source"  # a newer release exists; this copy can't install it


class Updates(Protocol):
    """services.updater.Updater, as the controller uses it."""

    @property
    def can_install(self) -> bool: ...

    def newer_release(self) -> ReleaseOut | None: ...

    def download(
        self, release: ReleaseOut, stop: threading.Event, progress: Progress
    ) -> ReadyUpdate: ...

    def verify(self, update: ReadyUpdate) -> None: ...

    def remember(self, update: ReadyUpdate) -> None: ...

    def forget(self) -> None: ...


class _NoLongerOfferedError(Exception):
    """The ready update isn't what the server offers any more (withdrawn, or superseded)."""

    def __init__(self, offered: ReleaseOut | None) -> None:
        super().__init__("The ready update is no longer offered")
        self.offered = offered


class _Relay(QObject):
    """Download progress from a pool thread, delivered on the GUI thread (queued)."""

    moved = Signal(object)  # bytes so far; Signal(int) wraps past 2 GB


class UpdateController(QObject):
    changed = Signal()  # read `state`, `status` and `error` again
    ready = Signal(str)  # version: downloaded and verified (the notification)
    prompt = Signal()  # show the update dialog: the server refused this version
    # While the server refuses this version: what is happening about it, for the main window.
    urgent_status = Signal(str)
    install_requested = Signal()  # quit; `app.main` then runs `installer_to_run`

    def __init__(
        self, updates: Updates, worker: Worker | None = None, interval_ms: int = CHECK_INTERVAL_MS
    ) -> None:
        super().__init__()
        self._updates = updates
        self._worker = worker or Worker()
        self._stop = threading.Event()
        self._generation = 0  # bumped at sign-out: older results are dropped
        self._state = UpdateState.IDLE
        self._release: ReleaseOut | None = None
        self._update: ReadyUpdate | None = None
        self._failure = ""
        self._done_bytes = 0
        self._urgent = False  # the server refused this version
        self._on_restart = False
        self._remembering = False
        self._rechecking = False
        self.installer_to_run: Path | None = None
        self._relay = _Relay(self)
        self._relay.moved.connect(self._moved)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.check)

    # --- what the views show -------------------------------------------------------------

    @property
    def state(self) -> UpdateState:
        return self._state

    @property
    def release(self) -> ReleaseOut | None:
        """The newer release, once one is found."""
        return self._release

    @property
    def urgent(self) -> bool:
        return self._urgent

    @property
    def on_restart(self) -> bool:
        """The ready update is installed at the next start."""
        return self._on_restart

    @property
    def error(self) -> str:
        """What went wrong with the last action, while it still applies ("" otherwise)."""
        return self._failure if self._state in (UpdateState.FAILED, UpdateState.READY) else ""

    @property
    def busy(self) -> bool:
        return self._remembering or self._state in (
            UpdateState.CHECKING,
            UpdateState.DOWNLOADING,
            UpdateState.INSTALLING,
        )

    @property
    def status(self) -> str:
        version = self._release.version if self._release else ""
        match self._state:
            case UpdateState.IDLE:
                return messages.UPDATE_NOT_CHECKED
            case UpdateState.CHECKING:
                return messages.UPDATE_CHECKING
            case UpdateState.UP_TO_DATE:
                return messages.UPDATE_UP_TO_DATE
            case UpdateState.DOWNLOADING:
                size = self._release.size_bytes if self._release else 0
                percent = self._done_bytes * 100 // size if size else 0
                return messages.update_downloading(version, percent)
            case UpdateState.READY:
                if self._on_restart:
                    return messages.update_on_restart(version)
                return messages.update_ready(version)
            case UpdateState.INSTALLING:
                return messages.UPDATE_PREPARING
            case UpdateState.FAILED:
                return self._failure  # each says what to do next
            case UpdateState.FROM_SOURCE:
                return messages.update_from_source(version)

    # --- actions ---------------------------------------------------------------------------

    def start(self) -> None:
        """Signed in: check now and every 6 hours."""
        self._timer.start()
        self.check()

    def stop(self) -> None:
        """Signed out: forget everything; the next account starts afresh. Downloaded files
        stay: a download for the same release reuses them once they verify again."""
        self._timer.stop()
        self._generation += 1
        self._state = UpdateState.IDLE
        self._release = self._update = None
        self._failure = ""
        self._done_bytes = 0
        self._urgent = self._on_restart = self._remembering = self._rechecking = False
        self.changed.emit()

    def shutdown(self) -> None:
        """Quitting: a download in progress stops (its partial file is kept to resume)."""
        self._timer.stop()
        self._stop.set()

    def check(self, *, urgent: bool = False) -> None:
        """Look for a newer release and download it. Nothing new starts while busy; with an
        update ready, it is checked that the server still offers it (an urgent check then
        shows the prompt)."""
        self._urgent = self._urgent or urgent
        if self.busy:
            return
        if self._state is UpdateState.READY:
            self._recheck()
            return
        self._failure = ""
        self._set(UpdateState.CHECKING)
        self._submit(self._updates.newer_release, self._checked, self._failed)

    def install_now(self) -> None:
        """Check that the server still offers it (unless offline), check the installer once
        more, remember it (should the launch fail, the next start installs it), then quit so
        it can run."""
        update = self._update
        if self._state is not UpdateState.READY or update is None or self._remembering:
            return

        def prepare() -> ReadyUpdate:
            self._still_offered(update)
            self._updates.verify(update)
            self._updates.remember(update)
            return update

        self._failure = ""
        self._set(UpdateState.INSTALLING)
        self._submit(prepare, self._install, self._failed)

    def install_on_restart(self) -> None:
        update = self._update
        if (
            self._state is not UpdateState.READY
            or update is None
            or self._on_restart
            or self._remembering
        ):
            return
        self._failure = ""
        self._remembering = True
        self.changed.emit()
        self._submit(lambda: self._updates.remember(update), self._remembered, self._failed)

    # --- on the pool -----------------------------------------------------------------------

    def _still_offered(self, update: ReadyUpdate) -> None:
        try:
            offered = self._updates.newer_release()
        except OfflineError:
            return  # can't ask: what was verified is installed
        if offered is None or offered.id != update.release.id:
            self._updates.forget()
            raise _NoLongerOfferedError(offered)

    # --- results, on the GUI thread ------------------------------------------------------

    def _recheck(self) -> None:
        update = self._update
        if update is None or self._rechecking:
            return
        self._rechecking = True
        self._submit(lambda: self._still_offered(update), self._rechecked, self._recheck_failed)

    def _rechecked(self, _: object) -> None:
        self._rechecking = False
        if self._urgent:
            self.prompt.emit()

    def _recheck_failed(self, exc: Exception) -> None:
        self._rechecking = False
        if isinstance(exc, _NoLongerOfferedError):
            self._drop(exc.offered)
            return
        logger.warning("Couldn't check the ready update is still offered: %s", _describe(exc))
        if self._urgent:
            self.prompt.emit()

    def _checked(self, release: ReleaseOut | None) -> None:
        if release is None:
            self._set(UpdateState.UP_TO_DATE)
            self._tell_urgent(messages.UPDATE_NONE_AVAILABLE)
            return
        self._release = release
        if not self._updates.can_install:
            self._set(UpdateState.FROM_SOURCE)
            self._tell_urgent(self.status)
            return
        self._done_bytes = 0
        self._set(UpdateState.DOWNLOADING)
        self._tell_urgent(messages.UPDATE_REQUIRED_DOWNLOADING)
        relay = self._relay
        self._submit(
            lambda: self._updates.download(release, self._stop, relay.moved.emit),
            self._downloaded,
            self._failed,
        )

    def _moved(self, done: object) -> None:
        if isinstance(done, int) and self._state is UpdateState.DOWNLOADING:
            self._done_bytes = done
            self.changed.emit()

    def _downloaded(self, update: ReadyUpdate) -> None:
        self._update = update
        self._set(UpdateState.READY)
        self.ready.emit(update.release.version)
        if self._urgent:
            self.prompt.emit()

    def _remembered(self, _: object) -> None:
        self._remembering = False
        self._on_restart = True
        self.changed.emit()

    def _install(self, update: ReadyUpdate) -> None:
        self.installer_to_run = update.installer
        logger.info("Installing update %s", update.release.version)
        self.install_requested.emit()

    def _failed(self, exc: Exception) -> None:
        self._remembering = False
        if isinstance(exc, TransferCancelledError):  # quitting
            return
        if isinstance(exc, _NoLongerOfferedError):
            self._drop(exc.offered)
            return
        if isinstance(exc, UpdateVerificationError):
            logger.warning("Update not verified: %s", exc)
            self._update = None
            self._on_restart = False
            message = messages.UPDATE_NOT_VERIFIED
        elif isinstance(exc, UpdateDeferredError):
            logger.warning("Update can't be checked now: %s", exc)
            message = messages.UPDATE_CANT_CHECK_NOW
        elif isinstance(exc, OfflineError):
            message = messages.UPDATE_CHECK_OFFLINE
        elif isinstance(exc, OSError):
            logger.warning("Couldn't prepare the update", exc_info=exc)
            message = messages.UPDATE_NOT_SAVED
        else:
            logger.warning("Update check failed: %s", _describe(exc))
            message = messages.message_for(exc)
        self._failure = message
        if self._update is not None:
            self._set(UpdateState.READY)  # still downloaded and verified: try again
        else:
            self._set(UpdateState.FAILED)
            self._tell_urgent(message)

    def _drop(self, offered: ReleaseOut | None) -> None:
        """The ready update was withdrawn or superseded: offer what the server offers now."""
        logger.info("The ready update is no longer offered; dropped")
        self._update = None
        self._on_restart = False
        self._checked(offered)

    def _tell_urgent(self, message: str) -> None:
        if self._urgent:
            self.urgent_status.emit(f"{messages.UPDATE_REQUIRED} {message}")

    def _submit[T](
        self,
        fn: Callable[[], T],
        on_success: Callable[[T], None],
        on_failure: Callable[[Exception], None],
    ) -> None:
        """Like Worker.submit, but a result arriving after sign-out is dropped."""
        generation = self._generation

        def succeeded(result: T) -> None:
            if generation == self._generation:
                on_success(result)

        def failed(exc: Exception) -> None:
            if generation == self._generation:
                on_failure(exc)

        self._worker.submit(fn, succeeded, failed)

    def _set(self, state: UpdateState) -> None:
        self._state = state
        self.changed.emit()


def _describe(exc: Exception) -> str:
    """For the log: the type, and the server's request ID when there is one."""
    if isinstance(exc, ApiError) and exc.request_id:
        return f"{type(exc).__name__} (request {exc.request_id})"
    return type(exc).__name__

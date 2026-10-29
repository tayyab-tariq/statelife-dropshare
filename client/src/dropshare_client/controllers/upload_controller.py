"""Upload from the app (FR-FILE-02): plan the copy, ask about names already used, check the
space, copy into the sync folder with progress, and let the user cancel at any point (OQ-28).

Planning and copying run on the worker pool; progress comes back through a queued signal,
at most about four times a second. Byte counts travel as Python ints (Signal(object)): a Qt
int signal is 32-bit and would wrap past 2 GB.
"""

import logging
import threading
import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.sync_controller import SyncController
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.config import ConfigStore
from dropshare_client.services.errors import (
    ApiError,
    FileActionError,
    FileProblem,
    InvalidInputError,
    OfflineError,
    SessionExpiredError,
    TransferCancelledError,
    UpgradeRequiredError,
)
from dropshare_client.services.importer import (
    Importer,
    ImportPlan,
    ImportResult,
    ImportTakenBackError,
    NotEnoughSpaceError,
    Resolution,
)
from dropshare_shared.schemas.users import UserOut

# The UI chooses with this; it is the importer's own, re-exported so ui/ needn't import it.
__all__ = ["Resolution", "UploadController"]

logger = logging.getLogger(__name__)

_PROGRESS_SECONDS = 0.25  # FR-CLIENT-08's pace, for the copy's progress too

# The sync folder -> the importer for it (app.py wires the platform's Recycle Bin).
type ImporterFactory = Callable[[Path], Importer]
# The signed-in user as the server has them now (GET /me), for a current quota figure.
type CurrentUser = Callable[[], UserOut]


class _Phase(StrEnum):
    IDLE = "idle"
    PLANNING = "planning"
    CHOOSING = "choosing"  # waiting for the user's answers about names already used
    COPYING = "copying"


class _Relay(QObject):
    """Progress from the copying thread to the GUI thread."""

    copied = Signal(object)  # int bytes


class UploadController(QObject):
    busy_changed = Signal(bool)
    planning = Signal()  # looking through what was chosen; cancel() stops it
    clashes_found = Signal(list)  # names already used in the target folder: answer choose()
    started = Signal(object)  # total bytes to copy (int)
    progressed = Signal(object)  # bytes copied so far (int)
    succeeded = Signal(str)
    cancelled = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        config: ConfigStore,
        importer: ImporterFactory,
        sync: SyncController,
        current_user: CurrentUser,
        worker: Worker | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._importer = importer
        self._sync = sync
        self._current_user = current_user
        self._worker = worker or Worker()
        self._phase = _Phase.IDLE
        self._plan: ImportPlan | None = None
        self._current: Importer | None = None
        self._stop = threading.Event()
        self._signed_in: UserOut | None = None
        self._relay = _Relay()
        self._relay.copied.connect(self.progressed, Qt.ConnectionType.QueuedConnection)

    @property
    def busy(self) -> bool:
        return self._phase is not _Phase.IDLE

    def set_user(self, user: UserOut | None) -> None:
        """The user at sign-in; their quota is read again from the server for each upload,
        and this is only used when that can't be done. None once signed out."""
        self._signed_in = user

    def upload(self, sources: list[Path], target: str) -> None:
        root = self._config.config.sync_root
        if not sources:
            return
        if self.busy:
            self.failed.emit(messages.UPLOAD_ALREADY_RUNNING)
            return
        if root is None:
            self.failed.emit(messages.NO_SYNC_FOLDER_YET)
            return
        network = next((p for p in sources if _is_network(p)), None)
        if network is not None:  # not touched at all: that alone could reach the server
            self.failed.emit(
                messages.file_problem(FileActionError(FileProblem.NETWORK_SOURCE, network.name))
            )
            return
        importer = self._importer(root)
        self._current = importer
        self._stop = threading.Event()
        stop = self._stop
        self._set_phase(_Phase.PLANNING)
        self.planning.emit()

        def plan() -> ImportPlan:
            planned = importer.plan(sources, target, stop)
            importer.check_space(planned, self._quota_left())
            return planned

        self._worker.submit(plan, self._planned, self._failed)

    def choose(self, resolutions: dict[str, Resolution]) -> None:
        """The user's answers about the names that were already used."""
        if self._phase is _Phase.CHOOSING and self._plan is not None:
            self._copy(self._plan, resolutions)

    def cancel(self) -> None:
        """Before copying: nothing happens. While copying: everything is taken back."""
        if self._phase in (_Phase.PLANNING, _Phase.COPYING):
            self._stop.set()
        elif self._phase is _Phase.CHOOSING:
            self._plan = None
            self._set_phase(_Phase.IDLE)
            self.cancelled.emit(messages.UPLOAD_CANCELLED)

    def _quota_left(self) -> int | None:
        """On the worker: the quota left now, never below zero; the sign-in figure if the
        server can't be asked. The server checks again for every upload."""
        user: UserOut | None
        try:
            user = self._current_user()
        except (ApiError, OfflineError, SessionExpiredError, UpgradeRequiredError):
            logger.info("Couldn't read the current quota; using the one from sign-in")
            user = self._signed_in
        if user is None:
            return None
        return max(0, user.quota_bytes - user.used_bytes)

    def _planned(self, plan: ImportPlan) -> None:
        if self._stop.is_set():
            self._failed(TransferCancelledError())
        elif plan.clashes:
            self._plan = plan
            self._set_phase(_Phase.CHOOSING)
            self.clashes_found.emit(list(plan.clashes))
        else:
            self._copy(plan, {})

    def _copy(self, plan: ImportPlan, resolutions: dict[str, Resolution]) -> None:
        importer = self._current
        if importer is None:
            return
        self._plan = None
        self._stop = threading.Event()
        stop = self._stop
        self._set_phase(_Phase.COPYING)
        self.started.emit(plan.total)
        last = [0.0]

        def progress(done: int) -> None:  # on the copying thread
            now = time.monotonic()
            if now - last[0] >= _PROGRESS_SECONDS or done >= plan.total:
                last[0] = now
                self._relay.copied.emit(done)

        self._worker.submit(
            lambda: importer.run(plan, resolutions, stop, progress),
            lambda result: self._copied(result, len(plan.sources)),
            self._failed,
        )

    def _copied(self, result: ImportResult, chosen: int) -> None:
        self._set_phase(_Phase.IDLE)
        if result.changed:
            self._sync.changed_here(result.changed)
        text = messages.uploaded(len(result.changed), chosen - len(result.changed))
        if result.kept:
            text = f"{text} {messages.kept_aside(len(result.kept))}"
        self.succeeded.emit(text)

    def _failed(self, exc: Exception) -> None:
        self._set_phase(_Phase.IDLE)
        if isinstance(exc, ImportTakenBackError):
            cause = exc.__cause__
            if not exc.cancelled and isinstance(cause, Exception):
                logger.error("Upload failed and was taken back", exc_info=cause)
                self.failed.emit(
                    f"{messages.message_for(cause)} "
                    f"{messages.upload_taken_back(cancelled=False, kept=len(exc.kept))}"
                )
            else:
                self.cancelled.emit(messages.upload_taken_back(cancelled=True, kept=len(exc.kept)))
        elif isinstance(exc, TransferCancelledError):
            self.cancelled.emit(messages.UPLOAD_CANCELLED)
        elif isinstance(exc, NotEnoughSpaceError):
            self.failed.emit(
                messages.not_enough_space(exc.needed, exc.available, in_dropshare=exc.in_dropshare)
            )
        else:
            if not isinstance(exc, InvalidInputError):
                logger.error("Upload failed", exc_info=exc)
            self.failed.emit(messages.message_for(exc))

    def _set_phase(self, phase: _Phase) -> None:
        was_busy = self.busy
        self._phase = phase
        if self.busy != was_busy:
            self.busy_changed.emit(self.busy)


def _is_network(path: Path) -> bool:
    """A UNC path (\\\\server\\share, or //server/share from a file://server/ URL): opening it
    would connect to that server, so it is refused before anything touches it."""
    return str(path).replace("\\", "/").startswith("//")

"""Shared with me (FR-FILE-01, FR-SHARE-02/03): what other people share with the user, adding a
shared folder to their DropShare or leaving it, and for single files saving a copy or, as
editor, uploading a new version (OQ-32). Looks again every minute while signed in, and says when
something new was shared (FR-CLIENT-02, new shares only for now)."""

import logging
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.errors import TransferCancelledError
from dropshare_client.services.links import link_to
from dropshare_client.services.sharing import Sharing
from dropshare_shared.schemas.nodes import NodeKind
from dropshare_shared.schemas.sharing import SharedItemOut, ShareRole

logger = logging.getLogger(__name__)

REFRESH_MS = 60_000

# Re-exported so ui/ needn't import the shared contract (architecture §1).
__all__ = ["NodeKind", "ShareRole", "SharedController", "SharedItemOut"]


class SharedController(QObject):
    loading = Signal(bool)  # True: keep what is shown while it reloads
    loaded = Signal()  # read items()
    failed = Signal(str)
    busy_changed = Signal(bool)
    succeeded = Signal(str)
    action_failed = Signal(str)
    new_shares = Signal(list)  # names shared since the last look (not on the first one)
    # A transfer's progress (FR-CLIENT-08): what it is, bytes done and in all. Object, not
    # int: byte counts pass 2 GB (CLAUDE.md).
    transfer_started = Signal(str)
    transfer_progressed = Signal(object, object)

    def __init__(self, sharing: Sharing, worker: Worker | None = None) -> None:
        super().__init__()
        self._sharing = sharing
        self._worker = worker or Worker()
        self._items: list[SharedItemOut] = []
        self._seen: set[uuid.UUID] | None = None  # None until the first answer of a session
        self._generation = 0
        self._busy = False
        self._stop = threading.Event()
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(lambda: self.refresh(quiet=True))

    @property
    def busy(self) -> bool:
        return self._busy

    def items(self) -> list[SharedItemOut]:
        return list(self._items)

    def link(self, item: SharedItemOut) -> str:
        return link_to(item.node_id)

    def start(self) -> None:
        """Signed in: load now and keep looking."""
        self._stop.clear()
        self._timer.start()
        self.refresh()

    def clear(self) -> None:
        """Signed out: forget everything and stop."""
        self._generation += 1
        self._timer.stop()
        self._stop.set()
        self._items = []
        self._seen = None
        self.loaded.emit()

    def refresh(self, *, quiet: bool = False) -> None:
        generation = self._generation
        self.loading.emit(quiet or bool(self._items))
        self._worker.submit(
            self._sharing.shared_with_me,
            lambda items: self._loaded(generation, items),
            lambda exc: self._failed(generation, exc, quiet),
        )

    def _loaded(self, generation: int, items: list[SharedItemOut]) -> None:
        if generation != self._generation:
            return
        ids = {item.node_id for item in items}
        if self._seen is not None:
            new = [item.name for item in items if item.node_id not in self._seen]
            if new:
                self.new_shares.emit(new)
        self._seen = ids
        self._items = items
        self.loaded.emit()

    def _failed(self, generation: int, exc: Exception, quiet: bool) -> None:
        if generation != self._generation:
            return
        logger.info("Couldn't load Shared with me: %s", exc)
        if not quiet:
            self.failed.emit(messages.message_for(exc))

    # --- actions -------------------------------------------------------------------------

    def add_to_dropshare(self, item: SharedItemOut) -> None:
        if item.namespace_id is not None:
            namespace_id = item.namespace_id
            self._act(
                lambda: self._sharing.add_to_dropshare(namespace_id), messages.ADDED_TO_DROPSHARE
            )

    def leave(self, item: SharedItemOut) -> None:
        if item.namespace_id is not None:
            namespace_id = item.namespace_id
            self._act(lambda: self._sharing.leave(namespace_id), messages.LEFT_SHARED_FOLDER)

    def save_copy(self, item: SharedItemOut, target: Path) -> None:
        self._transfer(
            messages.saving_copy(item.name),
            lambda stop: self._sharing.save_copy(item.node_id, target, stop, self._progress),
            messages.COPY_SAVED,
        )

    def upload_version(self, item: SharedItemOut, source: Path) -> None:
        self._transfer(
            messages.uploading_version(item.name),
            lambda stop: self._sharing.upload_version(item.node_id, source, stop, self._progress),
            messages.NEW_VERSION_UPLOADED,
        )

    def cancel(self) -> None:
        """Stop the transfer running now; what it wrote so far is removed."""
        self._stop.set()

    def _progress(self, done: int, total: int) -> None:
        """Called on the pool thread: a queued signal hands it to the GUI thread."""
        self.transfer_progressed.emit(done, total)

    def _transfer(self, what: str, work: Callable[[threading.Event], object], done: str) -> None:
        if self._busy:
            self.action_failed.emit(messages.ACTION_ALREADY_RUNNING)
            return
        stop = self._stop = threading.Event()
        self.transfer_started.emit(what)
        self._act(lambda: work(stop), done)

    def _act(self, work: Callable[[], object], done: str) -> None:
        if self._busy:
            self.action_failed.emit(messages.ACTION_ALREADY_RUNNING)
            return
        generation = self._generation
        self._set_busy(True)

        def succeeded(_: object) -> None:
            self._set_busy(False)
            if generation == self._generation:
                self.succeeded.emit(done)
                self.refresh()

        def failed(exc: Exception) -> None:
            self._set_busy(False)
            if generation == self._generation:
                logger.info("Shared with me action failed: %s", exc)
                if isinstance(exc, FileExistsError):
                    self.action_failed.emit(messages.COPY_EXISTS_THERE)
                elif isinstance(exc, TransferCancelledError):
                    self.succeeded.emit(messages.TRANSFER_CANCELLED)
                else:
                    self.action_failed.emit(messages.message_for(exc))

        self._worker.submit(work, succeeded, failed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)

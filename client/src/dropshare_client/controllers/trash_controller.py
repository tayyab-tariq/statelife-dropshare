"""The trash (FR-TRASH-01 … 03): what the user (or, in their shared folders, others) deleted,
restoring it, and deleting it for good. Restored items come back to the sync folder through
ordinary sync; nothing here touches the disk."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.worker import Worker
from dropshare_client.services.errors import (
    ApiError,
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_client.services.trash import Trash
from dropshare_shared.errors import ErrorCode
from dropshare_shared.schemas.nodes import NodeKind
from dropshare_shared.schemas.trash import TrashItemOut

logger = logging.getLogger(__name__)

# What one item's request can fail with; anything else is a bug and ends the whole action.
_ITEM_ERRORS = (ApiError, OfflineError, SessionExpiredError, UpgradeRequiredError)

# Re-exported so ui/ needn't import the shared contract (architecture §1).
__all__ = ["NodeKind", "TrashController", "TrashItemOut"]


@dataclass(frozen=True)
class _Outcome:
    done: int
    failures: list[Exception]
    stopped: bool
    note: str | None = None  # what to say instead of the usual message (one renamed item)


class TrashController(QObject):
    loading = Signal(bool)  # True: keep what is shown while it reloads
    loaded = Signal()  # read items()
    failed = Signal(str)  # nothing to show: the view shows the error state
    busy_changed = Signal(bool)
    succeeded = Signal(str)
    action_failed = Signal(str)  # also a reload that failed while the list is shown
    progressed = Signal(str)  # "Restoring 3 of 40…", from the pool thread (queued)
    cleared = Signal()  # signed out or another account: forget messages and selection

    def __init__(self, trash: Trash, worker: Worker | None = None) -> None:
        super().__init__()
        self._trash = trash
        self._worker = worker or Worker()
        self._items: list[TrashItemOut] = []
        self._generation = 0  # changes when the account does: late answers are dropped
        self._reload = 0  # the latest reload: an older one finishing later is dropped
        self._loaded_once = False
        self._busy = False
        self._stop = threading.Event()

    @property
    def busy(self) -> bool:
        return self._busy

    def items(self) -> list[TrashItemOut]:
        return list(self._items)

    def clear(self) -> None:
        """Signed out, or the session ended: forget everything and stop what is running."""
        self._generation += 1
        self._stop.set()
        self._items = []
        self._loaded_once = False
        if self._busy:
            self._set_busy(False)
        self.loaded.emit()
        self.cleared.emit()

    def cancel(self) -> None:
        """Stop a running action after the item in progress; what was done stays done."""
        self._stop.set()

    def refresh(self) -> None:
        generation = self._generation
        self._reload += 1
        reload = self._reload
        self.loading.emit(self._loaded_once)
        self._worker.submit(
            self._trash.items,
            lambda items: self._loaded(generation, reload, items),
            lambda exc: self._failed(generation, reload, exc),
        )

    def _current(self, generation: int, reload: int) -> bool:
        return generation == self._generation and reload == self._reload

    def _loaded(self, generation: int, reload: int, items: list[TrashItemOut]) -> None:
        if self._current(generation, reload):
            self._items = items
            self._loaded_once = True
            self.loaded.emit()

    def _failed(self, generation: int, reload: int, exc: Exception) -> None:
        if not self._current(generation, reload):
            return
        logger.info("Couldn't load the trash: %s", _logged(exc))
        if self._loaded_once:  # keep the list; say so above it
            self.action_failed.emit(messages.message_for(exc))
        else:
            self.failed.emit(messages.message_for(exc))

    # --- actions -------------------------------------------------------------------------

    def restore(self, items: list[TrashItemOut]) -> None:
        def act(item: TrashItemOut) -> str | None:
            node = self._trash.restore(item.id)
            return messages.restored_as(node.name) if node.name != item.name else None

        self._each(items, act, messages.restoring, messages.restored)

    def delete_forever(self, items: list[TrashItemOut]) -> None:
        deletable = [item for item in items if item.can_delete]

        def act(item: TrashItemOut) -> None:
            self._trash.delete_forever(item.id)

        self._each(deletable, act, messages.deleting_forever, messages.deleted_forever)

    def empty(self) -> None:
        """Calls the server until nothing is left: each call empties part of a large trash."""

        def work(stop: threading.Event) -> _Outcome:
            removed = freed = 0
            while not stop.is_set():
                result = self._trash.empty()
                removed += result.removed
                freed += result.freed_bytes
                if not result.more:
                    return _Outcome(removed, [], False, messages.trash_emptied(removed, freed))
                self.progressed.emit(messages.emptying(removed))
            return _Outcome(removed, [], True)

        self._run(work, messages.deleted_forever)

    def _each[T](
        self,
        items: list[TrashItemOut],
        act: Callable[[TrashItemOut], T],
        doing: Callable[[int, int], str],
        done_text: Callable[[int], str],
    ) -> None:
        """Run `act` for each item in turn on the pool; one failing doesn't stop the rest."""
        if not items:
            return

        def work(stop: threading.Event) -> _Outcome:
            done = 0
            failures: list[Exception] = []
            note: str | None = None
            for position, item in enumerate(items, start=1):
                if stop.is_set():
                    return _Outcome(done, failures, True)
                if len(items) > 1:
                    self.progressed.emit(doing(position, len(items)))
                try:
                    said = act(item)
                    done += 1
                    note = said if isinstance(said, str) and len(items) == 1 else None
                except _ITEM_ERRORS as exc:  # reported below; the rest still go
                    failures.append(exc)
            return _Outcome(done, failures, False, note)

        self._run(work, done_text, total=len(items))

    def _run(
        self,
        work: Callable[[threading.Event], _Outcome],
        done_text: Callable[[int], str],
        *,
        total: int = 0,
    ) -> None:
        if self._busy:
            self.action_failed.emit(messages.ACTION_ALREADY_RUNNING)
            return
        generation = self._generation
        stop = self._stop = threading.Event()
        self._set_busy(True)

        def finished(outcome: _Outcome) -> None:
            if generation != self._generation:
                return
            self._set_busy(False)
            if outcome.done or outcome.note:
                self.succeeded.emit(outcome.note or done_text(outcome.done))
            if outcome.stopped:
                self.succeeded.emit(messages.trash_action_stopped(outcome.done))
            if outcome.failures:
                first = outcome.failures[0]
                logger.info(
                    "Trash action failed for %d item(s): %s", len(outcome.failures), _logged(first)
                )
                reason = _reason(first)
                self.action_failed.emit(
                    reason if total == 1 else messages.some_failed(len(outcome.failures), reason)
                )
            self.refresh()

        def crashed(exc: Exception) -> None:
            if generation != self._generation:
                return
            self._set_busy(False)
            logger.info("Trash action failed: %s", _logged(exc))
            self.action_failed.emit(messages.message_for(exc))
            self.refresh()

        self._worker.submit(lambda: work(stop), finished, crashed)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.busy_changed.emit(busy)


def _reason(exc: Exception) -> str:
    if isinstance(exc, ApiError) and exc.code == ErrorCode.NOT_FOUND:
        return messages.NOT_IN_TRASH
    return messages.message_for(exc)


def _logged(exc: Exception) -> str:
    """The error for the log, with the server's request ID to find it there."""
    if isinstance(exc, ApiError) and exc.request_id:
        return f"{exc} (request {exc.request_id})"
    return str(exc)

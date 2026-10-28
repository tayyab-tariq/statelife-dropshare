"""What the Transfers view and the tray show about uploads and downloads (FR-CLIENT-08).

Events arrive on the GUI thread from the sync engine (through SyncController), already
throttled per file. They are gathered and handed on at most every 250 ms, so a cycle that
queues 1,000 files is one update for the view, not 1,000.
"""

from collections import deque

from PySide6.QtCore import QObject, QTimer, Signal

from dropshare_client.controllers import messages
from dropshare_client.controllers.sync_controller import SyncController, SyncStatus
from dropshare_client.sync.transfers import TransferEvent, TransferStatus

FINISHED_KEPT = 50  # FR-CLIENT-08: the 50 most recent completed or failed transfers
_FLUSH_MS = 250
_ORDER = {TransferStatus.ACTIVE: 0, TransferStatus.WAITING: 1, TransferStatus.QUEUED: 2}


class TransferBook:
    """Transfers still to finish, in the order they were first seen, and the most recent
    finished ones. Plain data, no Qt."""

    def __init__(self) -> None:
        self._open: dict[str, TransferEvent] = {}
        self._finished: deque[TransferEvent] = deque(maxlen=FINISHED_KEPT)

    def apply(self, event: TransferEvent) -> None:
        if event.status in (TransferStatus.DONE, TransferStatus.FAILED):
            self._open.pop(event.key, None)
            for old in [f for f in self._finished if f.key == event.key]:
                self._finished.remove(old)
            self._finished.appendleft(event)
        else:
            self._open[event.key] = event

    def settle(self) -> None:
        """Syncing is up to date: nothing is still waiting or queued."""
        self._open.clear()

    def clear(self) -> None:
        self._open.clear()
        self._finished.clear()

    def rows(self) -> list[TransferEvent]:
        """Active first, then waiting, then queued; then finished, newest first."""
        still = sorted(self._open.values(), key=lambda e: _ORDER[e.status])
        return [*still, *self._finished]

    def summary(self) -> str:
        """ "12 items · 45%" while anything is open; empty otherwise."""
        if not self._open:
            return ""
        total = sum(e.total for e in self._open.values())
        done = sum(e.done for e in self._open.values())
        percent = 100 if total <= 0 else done * 100 // total
        return messages.transfers_summary(len(self._open), percent)


class TransfersController(QObject):
    rows_changed = Signal()  # read rows()
    summary_changed = Signal(str)

    def __init__(self, sync: SyncController) -> None:
        super().__init__()
        self._sync = sync
        self._book = TransferBook()
        self._summary = ""
        self._flush = QTimer(self)
        self._flush.setSingleShot(True)
        self._flush.setInterval(_FLUSH_MS)
        self._flush.timeout.connect(self._publish)
        sync.transfer_reported.connect(self._reported)
        sync.status_changed.connect(self._status_changed)

    @property
    def summary(self) -> str:
        return self._summary

    def rows(self) -> list[TransferEvent]:
        return self._book.rows()

    def retry(self, key: str) -> None:
        self._sync.retry(key)

    def _reported(self, event: TransferEvent) -> None:
        self._book.apply(event)
        self._schedule()

    def _status_changed(self, status: str, _: str) -> None:
        match SyncStatus(status):
            case SyncStatus.UP_TO_DATE:
                self._book.settle()
            case SyncStatus.STOPPED:  # signed out: nothing of that account stays
                self._book.clear()
            case _:
                return
        self._schedule()

    def _schedule(self) -> None:
        if not self._flush.isActive():
            self._flush.start()

    def _publish(self) -> None:
        self.rows_changed.emit()
        summary = self._book.summary()
        if summary != self._summary:
            self._summary = summary
            self.summary_changed.emit(summary)

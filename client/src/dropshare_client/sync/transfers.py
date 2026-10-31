"""Per-file transfer progress for the Transfers view and the tray (FR-CLIENT-08).

The engine reports each upload and download as it is queued, starts, moves bytes, and ends.
Byte updates arrive from transfer threads and are throttled here, so the UI gets at most about
four per second per file whatever the chunk or read size (transfers.md "Scheduling").
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum

# FR-CLIENT-08: at most ~4 progress updates per second per item.
MIN_INTERVAL_SECONDS = 0.25


class Direction(StrEnum):
    UPLOAD = "upload"
    DOWNLOAD = "download"


class TransferStatus(StrEnum):
    QUEUED = "queued"  # planned in this cycle, not started yet
    ACTIVE = "active"
    WAITING = "waiting"  # will be tried again on its own (locked, offline, busy server)
    DONE = "done"
    FAILED = "failed"  # can't sync until something changes; `reason` says why


@dataclass(frozen=True)
class TransferEvent:
    key: str  # the item's key in the sync folder; one row per key in the Transfers view
    path: str  # its on-disk spelling, for display
    direction: Direction
    status: TransferStatus
    done: int  # bytes transferred
    total: int  # bytes in the file
    reason: str = ""  # for WAITING and FAILED

    @property
    def percent(self) -> int:
        return 100 if self.total <= 0 else min(100, self.done * 100 // self.total)


class TransferReporter:
    """Turns the engine's transfer steps into events, throttling byte progress. Byte progress
    arrives from transfer threads (FR-XFER-03), so every step takes the same lock; events for
    one file therefore go out in the order they happened."""

    def __init__(
        self,
        emit: Callable[[TransferEvent], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        interval: float = MIN_INTERVAL_SECONDS,
    ) -> None:
        self._emit = emit
        self._clock = clock
        self._interval = interval
        self._current: dict[str, TransferEvent] = {}
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def queued(self, key: str, path: str, direction: Direction, total: int) -> None:
        self._send(TransferEvent(key, path, direction, TransferStatus.QUEUED, 0, total))

    def started(self, key: str, path: str, direction: Direction, total: int) -> None:
        self._send(TransferEvent(key, path, direction, TransferStatus.ACTIVE, 0, total))

    def progress(self, key: str, done: int) -> None:
        """Bytes so far; sent only if the last update for `key` is old enough."""
        with self._lock:
            event = self._current.get(key)
            if event is None or event.status is not TransferStatus.ACTIVE or done == event.done:
                return
            updated = replace(event, done=max(event.done, min(done, event.total)))
            self._current[key] = updated
            if self._clock() - self._last_sent.get(key, float("-inf")) >= self._interval:
                self._send_locked(updated)

    def finished(self, key: str) -> None:
        with self._lock:
            event = self._current.get(key)
            if event is not None:
                self._send_locked(replace(event, status=TransferStatus.DONE, done=event.total))

    def waiting(self, key: str, reason: str) -> None:
        self._end(key, TransferStatus.WAITING, reason)

    def failed(self, key: str, reason: str) -> None:
        self._end(key, TransferStatus.FAILED, reason)

    def _end(self, key: str, status: TransferStatus, reason: str) -> None:
        with self._lock:
            event = self._current.get(key)
            if event is not None:
                self._send_locked(replace(event, status=status, reason=reason))

    def _send(self, event: TransferEvent) -> None:
        with self._lock:
            self._send_locked(event)

    def _send_locked(self, event: TransferEvent) -> None:
        if event.status in (TransferStatus.QUEUED, TransferStatus.ACTIVE):
            self._current[event.key] = event
        else:
            self._current.pop(event.key, None)
        self._last_sent[event.key] = self._clock()
        if event.status is not TransferStatus.ACTIVE:
            self._last_sent.pop(event.key, None)
        self._emit(event)

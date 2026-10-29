"""The running sync engine: its thread, the long-poll thread and the file watcher
(architecture.md §2, transfers.md "Remote change notifications").

Qt-free. The UI hears about it only through SyncListener, whose methods may be called from
any of the engine's threads.
"""

import contextlib
import logging
import random
import threading
import time
import uuid
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from dropshare_client.platform.base import FileAttributes, FileIdentity, RecycleBin
from dropshare_client.services.errors import (
    OfflineError,
    SessionExpiredError,
    UpgradeRequiredError,
)
from dropshare_client.store.index import Index
from dropshare_client.sync import paths
from dropshare_client.sync.core import NoPersonalFolderError, SyncCore
from dropshare_client.sync.errors import SyncRootMissingError, SyncStoppedError
from dropshare_client.sync.scanner import STABLE_AFTER_NS
from dropshare_client.sync.server import FileServer
from dropshare_client.sync.transfers import TransferEvent
from dropshare_client.sync.watcher import Watcher

logger = logging.getLogger(__name__)

PERIODIC_SCAN_SECONDS = 30 * 60  # requirements §10.1
SETTLE_SECONDS = 1.0  # quiet time after the last local event before syncing (debounce)
_MAX_DELAY = 60.0
_ROOT_MISSING_DELAY = 10.0
_REPLANS_IN_A_ROW = 5  # after this many, pause a little between replans
_REPLAN_DELAY = 5.0
_STOP_SECONDS = 15.0
_POLL_SETTLE_SECONDS = 30.0
_POLLER_JOIN_SECONDS = 2.0
_STALE_PARTIAL_SECONDS = 24 * 60 * 60


class SyncState(StrEnum):
    """algorithm.md §11 (signed out is the shell's business, not the engine's)."""

    STARTING = "starting"
    SCANNING = "scanning"
    SYNCING = "syncing"
    UP_TO_DATE = "up_to_date"
    PAUSED = "paused"
    OFFLINE = "offline"
    NEEDS_CONFIRMATION = "needs_confirmation"
    UPGRADE_REQUIRED = "upgrade_required"
    ERROR = "error"
    STOPPED = "stopped"


class SyncProblem(StrEnum):
    ROOT_MISSING = "root_missing"
    NO_PERSONAL_FOLDER = "no_personal_folder"
    UNEXPECTED = "unexpected"


class SyncListener(Protocol):
    def state_changed(self, state: SyncState, problem: SyncProblem | None) -> None: ...

    def conflict(self, path: str, copy_path: str) -> None: ...

    def confirm_deletes(self, here: int, on_server: int) -> None: ...

    def session_expired(self) -> None: ...

    def progress(self, pending: int) -> None:
        """Items still to do in this cycle (FR-CLIENT-01 "Syncing (n items)"); 0 when done."""
        ...

    def transfer(self, event: TransferEvent) -> None:
        """An upload or download was queued, moved bytes (at most ~4 times a second), or
        ended (FR-CLIENT-08). Called on the engine's thread."""
        ...


def _jitter(delay: float) -> float:
    return delay + random.uniform(0, delay / 4)  # noqa: S311 - spreading retries, not security


class SyncEngine:
    def __init__(
        self,
        root: Path,
        index: Index,
        server: FileServer,
        recycle_bin: RecycleBin,
        file_identity: FileIdentity,
        file_attributes: FileAttributes,
        listener: SyncListener,
        *,
        display_name: str,
        watch: bool = True,
        stable_after_ns: int = STABLE_AFTER_NS,
        settle_seconds: float = SETTLE_SECONDS,
        periodic_scan_seconds: float = PERIODIC_SCAN_SECONDS,
        max_path: int | None = None,
    ) -> None:
        """The engine owns `index` from here on and closes it once stopped."""
        self._root = root
        self._index = index
        self._server = server
        self._listener = listener
        self._settle = settle_seconds
        self._periodic = periodic_scan_seconds
        self._stopping = threading.Event()
        self._interrupt = threading.Event()  # stop or pause: abandon the step in progress
        self._core = SyncCore(
            root,
            index,
            server,
            recycle_bin,
            file_identity,
            file_attributes,
            self,
            display_name=display_name,
            stop=self._interrupt,
            stable_after_ns=stable_after_ns,
            max_path=max_path,
            progress=listener.progress,
            transfer=listener.transfer,
        )
        self._watcher = Watcher(root, self.local_changed) if watch else None
        self._watcher_lock = threading.Lock()  # started by the engine thread, stopped by stop()
        self._lock = threading.Condition()
        self._local: set[str] = set()
        self._local_due: float | None = None
        self._remote = False
        self._full = True
        self._retry: dict[str, float] = {}  # key -> monotonic time it is due
        self._not_before = 0.0
        self._next_full = time.monotonic() + periodic_scan_seconds
        self._paused = False
        self._confirming = False
        self._state: tuple[SyncState, SyncProblem | None] | None = None
        self._cursors: dict[uuid.UUID, int] = {}
        self._thread = threading.Thread(target=self._run, name="sync-engine", daemon=True)
        self._poller = threading.Thread(target=self._poll, name="sync-long-poll", daemon=True)

    # --- control (any thread) ------------------------------------------------------------

    def start(self) -> None:
        self._set_state(SyncState.STARTING)
        self._thread.start()
        self._poller.start()
        self._ensure_watcher()

    def _ensure_watcher(self) -> None:
        """(Re)start watching; a missing folder or a lost drive stops the watcher."""
        with self._watcher_lock:
            if self._watcher is None or self._watcher.alive or self._stopping.is_set():
                return
            try:
                self._watcher.start()
            except OSError:
                logger.warning("Can't watch the sync folder yet; relying on scans", exc_info=True)

    def stop(self, timeout: float = _STOP_SECONDS) -> bool:
        """Stop at a safe point. True if the engine thread finished within `timeout`."""
        with self._lock:
            self._stopping.set()
            self._interrupt.set()
            self._lock.notify_all()
        with self._watcher_lock:
            if self._watcher is not None:
                self._watcher.stop()
        deadline = time.monotonic() + timeout
        if self._thread.is_alive():
            self._thread.join(timeout)
        if self._poller.is_alive():
            # It may be inside a wait the server holds for most of a minute: give it a moment
            # to come back, but don't hold up quitting for it (it is a daemon thread).
            self._poller.join(max(0.0, min(deadline - time.monotonic(), _POLLER_JOIN_SECONDS)))
        if self._thread.is_alive():
            return False  # still mid-step: the index stays open for it
        self._index.close()
        return True

    def pause(self) -> None:
        """FR-SYNC-05: stop after the current step; nothing is lost, resume picks it up."""
        with self._lock:
            self._paused = True
            self._interrupt.set()
            self._lock.notify_all()
        self._set_state(SyncState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            self._paused = False
            self._confirming = False
            self._interrupt.clear()
            self._full = True  # whatever happened while paused
            self._not_before = 0.0
            self._lock.notify_all()
        self._set_state(SyncState.SYNCING)

    def confirm_deletes(self, approved: bool) -> None:
        """The answer to `SyncListener.confirm_deletes`. Declining pauses syncing, so nothing
        is deleted until the user resumes and is asked again (FR-SYNC-08)."""
        if approved:
            self._core.approve_deletes()
        with self._lock:
            self._confirming = False
            self._full = True
            self._lock.notify_all()
        if not approved:
            self.pause()

    def retry_now(self, key: str) -> None:
        """Look at `key` again in the next cycle, without waiting for its backoff: the
        Transfers view's Retry (FR-CLIENT-08). An item listed as can't sync is planned
        again too."""
        with self._lock:
            self._retry[key] = time.monotonic()
            self._lock.notify_all()

    def local_changed(self, path: str) -> None:
        """A watcher hint. Synced after SETTLE_SECONDS without further events."""
        name = paths.name_of(path)
        if name and paths.is_ignored(name):
            return
        key = paths.key_of(path)
        with self._lock:
            self._local.add(key)
            self._local_due = time.monotonic() + self._settle
            self._lock.notify_all()

    # --- ActivityLog for the executor ----------------------------------------------------

    def activity(self, kind: str, path: str, detail: str = "") -> None:
        self._index.add_activity(kind, path, detail)

    def conflict(self, path: str, copy_path: str) -> None:
        self._listener.conflict(path, copy_path)

    # --- the engine thread ---------------------------------------------------------------

    def _run(self) -> None:
        delay = 1.0
        replans = 0
        while True:
            work = self._next_work()
            if work is None:
                break
            dirty, full = work
            try:
                again = self._sync(dirty, full)
            except SyncStoppedError:
                with self._lock:
                    self._full = True  # finish the interrupted work after resume
                continue
            except OfflineError:
                self._set_state(SyncState.OFFLINE)
                self._back_off(delay, full_scan=True)
                delay = min(delay * 2, _MAX_DELAY)
                continue
            except SessionExpiredError:
                self._set_state(SyncState.STOPPED)
                self._listener.session_expired()
                break
            except UpgradeRequiredError:
                self._set_state(SyncState.UPGRADE_REQUIRED)
                break
            except SyncRootMissingError:
                logger.error("The sync folder is missing; not syncing until it is back")
                self._set_state(SyncState.ERROR, SyncProblem.ROOT_MISSING)
                self._back_off(_ROOT_MISSING_DELAY, full_scan=True)
                continue
            except NoPersonalFolderError:
                self._set_state(SyncState.ERROR, SyncProblem.NO_PERSONAL_FOLDER)
                self._back_off(_MAX_DELAY, full_scan=True)
                continue
            except Exception:  # engine boundary: log, show an error, retry later
                logger.exception("Sync cycle failed")
                self._set_state(SyncState.ERROR, SyncProblem.UNEXPECTED)
                self._back_off(delay, full_scan=True)
                delay = min(delay * 2, _MAX_DELAY)
                continue
            delay = 1.0
            replans = replans + 1 if again else 0
            if replans > _REPLANS_IN_A_ROW:
                logger.warning("Still replanning after %d cycles; slowing down", replans)
                self._back_off(_REPLAN_DELAY, full_scan=False)
        with self._lock:
            final = self._state
        if final is not None and final[0] not in (SyncState.UPGRADE_REQUIRED, SyncState.STOPPED):
            self._set_state(SyncState.STOPPED)

    def _next_work(self) -> tuple[set[str], bool] | None:
        """Wait until there is something to do; None once stopping."""
        with self._lock:
            while not self._stopping.is_set():
                now = time.monotonic()
                if self._paused or self._confirming:
                    self._lock.wait()
                    continue
                if now < self._not_before:
                    self._lock.wait(self._not_before - now)
                    continue
                local_ready = self._local_due is not None and self._local_due <= now
                due = {key for key, at in self._retry.items() if at <= now}
                periodic = now >= self._next_full
                if self._full or self._remote or local_ready or due or periodic:
                    full = self._full or periodic
                    dirty = set(due)
                    if local_ready:
                        dirty |= self._local
                        self._local.clear()
                        self._local_due = None
                    for key in due:
                        del self._retry[key]
                    self._full = self._remote = False
                    if full:
                        self._next_full = now + self._periodic
                    return dirty, full
                waits = [*self._retry.values(), self._next_full]
                if self._local_due is not None:
                    waits.append(self._local_due)
                self._lock.wait(max(min(waits) - now, 0.05))
        return None

    def _sync(self, dirty: set[str], full: bool) -> bool:
        """One cycle. True if it asked for another one right away."""
        if not self._core.connected:
            self._core.connect()
            self._remove_stale_partials()
            full = True
        dirty |= self._core.fetch()  # also settles interrupted operations (SyncCore.recover)
        self._publish_cursors()
        if full:
            dirty = {""}
        if dirty:
            self._set_state(SyncState.SCANNING if "" in dirty else SyncState.SYNCING)
            result = self._core.cycle(dirty)
        else:
            result = None
        now = time.monotonic()
        with self._lock:
            if result is not None:
                for key, seconds in result.retry.items():
                    self._retry[key] = min(self._retry.get(key, now + seconds), now + seconds)
                if result.again:
                    for key in paths.outermost(dirty):
                        self._retry.setdefault(key, now)
                    self._remote = True
                if result.confirm_deletes is not None:
                    self._confirming = True
            pending = bool(self._retry) or self._remote
        if result is not None and result.confirm_deletes is not None:
            self._set_state(SyncState.NEEDS_CONFIRMATION)
            self._listener.confirm_deletes(*result.confirm_deletes)
        else:
            self._set_state(SyncState.SYNCING if pending else SyncState.UP_TO_DATE)
        self._ensure_watcher()
        return result is not None and result.again

    def _remove_stale_partials(self) -> None:
        """Partial downloads nobody resumed for a day are DropShare's own leftovers."""
        cutoff = time.time() - _STALE_PARTIAL_SECONDS
        for part in (self._root / paths.TEMP_DIR).glob("*.part"):
            with contextlib.suppress(OSError):
                if part.stat().st_mtime < cutoff:
                    part.unlink()

    def _back_off(self, seconds: float, *, full_scan: bool) -> None:
        with self._lock:
            self._not_before = time.monotonic() + _jitter(seconds)
            self._full = self._full or full_scan

    def _publish_cursors(self) -> None:
        with self._lock:
            self._cursors = self._core.cursors()
            self._lock.notify_all()

    def _set_state(self, state: SyncState, problem: SyncProblem | None = None) -> None:
        with self._lock:
            if self._paused and state not in (SyncState.PAUSED, SyncState.STOPPED):
                return  # stays "Paused" until resumed
            if self._state == (state, problem):
                return
            self._state = (state, problem)
        logger.info("Sync state: %s%s", state.value, f" ({problem.value})" if problem else "")
        self._listener.state_changed(state, problem)

    # --- the long-poll thread ------------------------------------------------------------

    def _poll(self) -> None:
        delay = 1.0
        failed = False
        while not self._stopping.is_set():
            cursors = self._wait_for_cursors()
            if cursors is None:
                return
            try:
                changed = self._server.wait(cursors)
            except OfflineError:
                if not failed:
                    self._wake(remote=True)  # let the engine notice and show "Offline"
                failed = True
                self._stopping.wait(_jitter(delay))
                delay = min(delay * 2, _MAX_DELAY)
                continue
            except (SessionExpiredError, UpgradeRequiredError):
                self._wake(remote=True)  # the engine meets the same error and stops
                return
            except Exception:  # keep listening; the engine reports real problems
                logger.warning("Waiting for changes failed", exc_info=True)
                self._stopping.wait(_jitter(delay))
                delay = min(delay * 2, _MAX_DELAY)
                continue
            delay = 1.0
            if failed:  # reconnected: look at everything (FR-SYNC-04)
                failed = False
                self._wake(full=True)
            if changed:
                self._wake(remote=True)
                self._wait_for_cycle(cursors)

    def _wait_for_cursors(self) -> dict[uuid.UUID, int] | None:
        with self._lock:
            while not self._cursors and not self._stopping.is_set():
                self._lock.wait()
            return None if self._stopping.is_set() else dict(self._cursors)

    def _wait_for_cycle(self, sent: dict[uuid.UUID, int]) -> None:
        """Don't ask again until the engine has fetched what the last answer announced."""
        deadline = time.monotonic() + _POLL_SETTLE_SECONDS
        with self._lock:
            while self._cursors == sent and not self._stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._lock.wait(remaining)

    def _wake(self, *, remote: bool = False, full: bool = False) -> None:
        with self._lock:
            self._remote = self._remote or remote
            self._full = self._full or full
            if full:
                self._not_before = 0.0
            self._lock.notify_all()

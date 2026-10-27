"""Run blocking work on the thread pool and deliver the result on the GUI thread."""

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal

logger = logging.getLogger(__name__)


class _Signals(QObject):
    succeeded = Signal(object)
    failed = Signal(object)  # the exception


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], object], signals: _Signals) -> None:
        super().__init__()
        self._fn = fn
        self._signals = signals

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - task boundary: the controller logs and maps it
            self._signals.failed.emit(exc)
        else:
            self._signals.succeeded.emit(result)


class Worker:
    """One per controller. Callbacks run on the GUI thread, after `fn` finishes on the pool."""

    def __init__(self, pool: QThreadPool | None = None) -> None:
        self._pool = pool or QThreadPool.globalInstance()
        self._running: set[_Signals] = set()

    def submit[T](
        self,
        fn: Callable[[], T],
        on_success: Callable[[T], None],
        on_failure: Callable[[Exception], None],
    ) -> None:
        signals = _Signals()  # created here, so it lives on the GUI thread
        self._running.add(signals)

        def succeeded(result: object) -> None:
            self._finish(signals)
            on_success(result)  # type: ignore[arg-type]  # fn's result, passed through Qt untyped

        def failed(exc: object) -> None:
            self._finish(signals)
            if not isinstance(exc, Exception):
                raise TypeError(f"Worker reported a non-exception: {exc!r}")
            on_failure(exc)

        # Queued: the callbacks run on the GUI thread, never on the pool thread that emitted.
        signals.succeeded.connect(succeeded, Qt.ConnectionType.QueuedConnection)
        signals.failed.connect(failed, Qt.ConnectionType.QueuedConnection)
        self._pool.start(_Task(fn, signals))

    def wait(self, msecs: int) -> bool:
        """Block until the pool's work is done (only at quit, after the event loop ended)."""
        return self._pool.waitForDone(msecs)

    def _finish(self, signals: _Signals) -> None:
        """Release a finished task's signal object on the GUI thread that owns it; dropping it
        anywhere else could destroy a GUI-thread QObject from a pool thread."""
        self._running.discard(signals)
        signals.deleteLater()

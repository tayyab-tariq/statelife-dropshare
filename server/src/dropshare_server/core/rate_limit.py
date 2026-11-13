"""A per-key sliding-window rate limit kept in memory (NFR-SEC-13).

Per server process: it resets when the server restarts and isn't shared between workers, which
is accepted for admin actions (OQ-60). The reverse proxy's limits still apply in front of it.
"""

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable


class SlidingWindowLimiter:
    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 1 or window_seconds <= 0:
            raise ValueError("The limit and the window must be positive")
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[Hashable, deque[float]] = {}
        self._lock = threading.Lock()  # handlers run in FastAPI's thread pool

    def acquire(self, key: Hashable) -> int | None:
        """Count one action for `key`. None when allowed; otherwise the whole seconds to wait
        (at least 1), and the action isn't counted."""
        now = self._clock()
        with self._lock:
            self._forget_before(now - self._window)
            hits = self._hits.setdefault(key, deque())
            if len(hits) >= self._limit:
                return max(1, math.ceil(hits[0] + self._window - now))
            hits.append(now)
            return None

    def _forget_before(self, cutoff: float) -> None:
        """Drop hits older than the window, and keys left with none, so memory stays small."""
        for key in list(self._hits):
            hits = self._hits[key]
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if not hits:
                del self._hits[key]

"""In-memory sliding-window rate limiter (per process; resets on restart)."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable, Hashable


class RateLimiter:
    def __init__(
        self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._hits: defaultdict[Hashable, deque[float]] = defaultdict(deque)

    def allow(self, key: Hashable) -> bool:
        """Record a hit for ``key`` and return False if it exceeds the limit."""
        if self.limit <= 0:
            return True
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

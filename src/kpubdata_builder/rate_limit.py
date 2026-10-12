"""A per-key limit of N events in any 60 seconds, held in process memory (#1245).

Two limits use it: how often one user may run queries (``service.query_rate``) and how
often Builder may call one provider, whoever the call is for (``egress``). A sliding
window, rather than a token bucket, so that the setting means what it says: no 60
seconds hold more than N admitted events. The window keeps one timestamp per admitted
event, so it holds at most N per key, plus the slots reserved by callers that wait.

A caller either asks to be admitted now (:meth:`WindowLimiter.admit`, refused with how
long to wait) or reserves the next free slot and sleeps until it comes
(:meth:`WindowLimiter.wait`). A reserved slot is counted from the moment it is
reserved, so callers that wait are served in the order they arrived. A restart forgets
every window, which errs on the side of allowing.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable

#: The window every limit is counted over, in seconds.
WINDOW_SECONDS = 60.0


def per_minute_from_env(name: str) -> int:
    """The limit an environment variable sets; ``0`` (off) when unset or unusable.

    An unusable value — not an integer, or negative — is off, and the start-up check
    warns about it (``service.startup_settings``).
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(value, 0)


class RateLimited(Exception):
    """Not admitted; ``retry_after_seconds`` is how long to wait, at least one second."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class WindowLimiter:
    """At most ``per_minute()`` events per key in any 60 seconds. Thread-safe.

    ``per_minute`` is read on every call, so a test or an operator's environment change
    takes effect at once; ``0`` or less admits everything and records nothing.
    """

    def __init__(
        self,
        per_minute: Callable[[], int],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._windows: dict[str, deque[float]] = {}

    def _next_slot(self, key: str, limit: int, now: float) -> float:
        """When the next event of ``key`` may happen; ``now`` when it may at once."""
        # Windows that hold only expired events decide nothing any more.
        for name in [n for n, w in self._windows.items() if not w or w[-1] <= now - WINDOW_SECONDS]:
            del self._windows[name]
        window = self._windows.get(key)
        if window is None:
            return now
        while window and window[0] <= now - WINDOW_SECONDS:
            window.popleft()
        if len(window) < limit:
            return now
        # The slot frees when the event ``limit`` places back leaves the window.
        return max(now, window[-limit] + WINDOW_SECONDS)

    def _record(self, key: str, at: float) -> None:
        self._windows.setdefault(key, deque()).append(at)

    def admit(self, key: str) -> None:
        """Count one event of ``key`` now.

        Raises:
            RateLimited: The window is full. Nothing was counted.
        """
        limit = self._per_minute()
        if limit <= 0:
            return
        with self._lock:
            now = self._clock()
            slot = self._next_slot(key, limit, now)
            if slot > now:
                raise RateLimited(max(1, math.ceil(slot - now)))
            self._record(key, now)

    def wait(self, key: str) -> float:
        """Reserve the next slot of ``key`` and sleep until it comes; the seconds slept."""
        limit = self._per_minute()
        if limit <= 0:
            return 0.0
        with self._lock:
            now = self._clock()
            slot = self._next_slot(key, limit, now)
            self._record(key, slot)
        delay = slot - now
        if delay > 0:
            self._sleep(delay)
        return delay


__all__ = ["WINDOW_SECONDS", "RateLimited", "WindowLimiter", "per_minute_from_env"]

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

A wait that ends without its call — cancelled, or interrupted by an exception — gives
its slot back (#1328), so no later caller waits behind a call that never happens. A
caller that can be cancelled looks at its slot again every half second: it notices the
cancellation that soon, and moves up when a slot before its own is given back. A caller
that cannot be cancelled sleeps once to its slot, which therefore never moves.
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

#: How long a cancellable wait sleeps before it looks at its slot and signal again.
POLL_SECONDS = 0.5


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


class WaitCancelled(Exception):
    """A wait ended because its caller was cancelled; its slot was given back."""

    def __init__(self) -> None:
        super().__init__("wait cancelled")


class _Slot:
    """One counted event: when it happens, and whether its waiter can move it earlier."""

    __slots__ = ("at", "movable")

    def __init__(self, at: float, *, movable: bool) -> None:
        self.at = at
        self.movable = movable


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
        self._windows: dict[str, deque[_Slot]] = {}

    def _next_slot(self, key: str, limit: int, now: float) -> float:
        """When the next event of ``key`` may happen; ``now`` when it may at once."""
        # Windows that hold only expired events decide nothing any more.
        expired = now - WINDOW_SECONDS
        for name in [n for n, w in self._windows.items() if not w or w[-1].at <= expired]:
            del self._windows[name]
        window = self._windows.get(key)
        if window is None:
            return now
        while window and window[0].at <= expired:
            window.popleft()
        if len(window) < limit:
            return now
        # The slot frees when the event ``limit`` places back leaves the window.
        return max(now, window[-limit].at + WINDOW_SECONDS)

    def _record(self, key: str, at: float, *, movable: bool = False) -> _Slot:
        slot = _Slot(at, movable=movable)
        self._windows.setdefault(key, deque()).append(slot)
        return slot

    def _release(self, key: str, slot: _Slot) -> None:
        """Give back ``slot``, whose call will not happen, and move up who waits for it.

        Waiters behind it that can move take the earliest slot the window allows, never
        before the waiter ahead of them, so they are still served in arrival order.
        """
        with self._lock:
            window = self._windows.get(key)
            if window is None:
                return
            index = next((i for i, held in enumerate(window) if held is slot), None)
            if index is None:
                return
            del window[index]
            limit = self._per_minute()
            now = self._clock()
            for position in range(index, len(window)):
                later = window[position]
                if not later.movable or later.at <= now:
                    continue
                earliest = max(now, window[position - 1].at) if position else now
                if limit > 0 and position >= limit:
                    earliest = max(earliest, window[position - limit].at + WINDOW_SECONDS)
                later.at = min(later.at, earliest)

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

    def wait(self, key: str, *, cancelled: Callable[[], bool] | None = None) -> float:
        """Reserve the next slot of ``key`` and sleep until it comes; the seconds slept.

        ``cancelled`` is asked before the wait and every :data:`POLL_SECONDS` during it.

        Raises:
            WaitCancelled: ``cancelled`` answered True. The slot was given back, as it
                is when anything else interrupts the wait.
        """
        limit = self._per_minute()
        if limit <= 0:
            return 0.0
        with self._lock:
            start = self._clock()
            slot = self._record(
                key, self._next_slot(key, limit, start), movable=cancelled is not None
            )
        try:
            if cancelled is None:
                delay = slot.at - start
                if delay > 0:
                    self._sleep(delay)
                return delay
            while True:
                if cancelled():
                    raise WaitCancelled
                with self._lock:
                    now = self._clock()
                    remaining = slot.at - now
                if remaining <= 0:
                    return now - start
                self._sleep(min(remaining, POLL_SECONDS))
        except BaseException:
            self._release(key, slot)
            raise


__all__ = [
    "POLL_SECONDS",
    "WINDOW_SECONDS",
    "RateLimited",
    "WaitCancelled",
    "WindowLimiter",
    "per_minute_from_env",
]

"""How many builds run at once, on whichever path they started (#1028, #1040).

A build takes a slot for as long as its pipeline runs. Two callers take one:

- an async worker, **before** it marks its job running — a job that is waiting for a
  slot has started nothing and stays ``queued`` (#1040);
- the synchronous ``POST /build``, on its request thread, waiting no longer than the
  service allows — past that it answers ``build_queue_full`` instead of holding the
  thread (#1040).

``BuildRunsApiService.build`` is where the pipeline is called from both, so it is the
one place that requires a slot. A worker that already took one for the job it is running
is recognised by thread, and takes no second.
"""

from __future__ import annotations

import threading


class BuildSlots:
    """A counted limit that knows whether the current thread already holds a slot."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("max_concurrent_builds must be >= 1")
        self.limit = limit
        self._semaphore = threading.BoundedSemaphore(limit)
        self._local = threading.local()

    def held_by_current_thread(self) -> bool:
        return bool(getattr(self._local, "held", False))

    def acquire(self, timeout: float | None = None) -> bool:
        """Take a slot; False when ``timeout`` seconds pass without one. ``None`` waits."""
        acquired = self._semaphore.acquire(timeout=timeout)
        if acquired:
            self._local.held = True
        return acquired

    def release(self) -> None:
        self._local.held = False
        self._semaphore.release()


__all__ = ["BuildSlots"]

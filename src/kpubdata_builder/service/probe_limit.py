"""How often one user may probe a provider key (#1059).

A probe makes the server call the provider once per dataset, from the server's address,
with whatever string the request carries as a key. Unbounded, one user could repeat it
until the provider blocks that address — for every user of the deployment.

Two bounds, both per user and both in memory:

- **One probe at a time.** A second one while the first runs is refused.
- **An interval per provider.** After a probe of a provider ends, the same user's next
  probe of it is refused until ``KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS`` has passed
  (default 60; ``0`` turns the interval off and leaves the one-at-a-time bound).

A refused probe calls nothing. Another user is not affected, and neither is the same user's
probe of another provider once the first has ended. A restart forgets the state, which
errs on the side of allowing. The bounds hold in every deployment mode: a single-user
deployment has one user, and its provider can block its address just the same.
"""

from __future__ import annotations

import math
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

PROBE_INTERVAL_ENV = "KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS"
DEFAULT_PROBE_INTERVAL_SECONDS = 60.0


def probe_interval_seconds() -> float:
    """The configured interval; the default when unset, unreadable or negative."""
    raw = os.environ.get(PROBE_INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_PROBE_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_PROBE_INTERVAL_SECONDS
    return value if value >= 0 and math.isfinite(value) else DEFAULT_PROBE_INTERVAL_SECONDS


class ProbeRefused(Exception):
    """A probe was refused; ``retry_after_seconds`` is how long to wait, at least."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("probe refused")
        self.retry_after_seconds = retry_after_seconds


class ProbeLimiter:
    """Per-user bounds on provider probes. Thread-safe; holds no key."""

    def __init__(
        self,
        *,
        interval_seconds: Callable[[], float] = probe_interval_seconds,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._running: set[str] = set()
        self._ended: dict[tuple[str, str], float] = {}

    @contextmanager
    def probing(self, owner: str, provider: str) -> Iterator[None]:
        """Hold the user's one probe for the block.

        Raises:
            ProbeRefused: The user has a probe running, or probed this provider within
                the interval. Nothing was started.
        """
        interval = self._interval_seconds()
        with self._lock:
            now = self._clock()
            # Entries older than the interval decide nothing any more.
            for key in [key for key, ended in self._ended.items() if now - ended >= interval]:
                del self._ended[key]
            if owner in self._running:
                # When the running probe ends is not known; one second is the least wait.
                raise ProbeRefused(max(1, math.ceil(interval)))
            ended = self._ended.get((owner, provider))
            if ended is not None:
                raise ProbeRefused(max(1, math.ceil(interval - (now - ended))))
            self._running.add(owner)
        try:
            yield
        finally:
            with self._lock:
                self._running.discard(owner)
                if interval > 0:
                    self._ended[(owner, provider)] = self._clock()

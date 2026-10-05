"""Authentication failure throttle — push the cost of repeated failed auth attempts onto the client.

The auth gate (``dispatch``) performs API key comparison or bearer token RS256 signature
verification on every request. If we accept unlimited failed attempts, two things become free:

1. Guessing attempts against the single static ``X-API-Key`` per instance (ADR 0006).
2. Throwing invalid tokens to consume JWKS signature verification CPU — verification itself
   is offline so no external calls, but repeated on public endpoints means raw CPU burn.

When the same client exceeds the limit within a window, we cut it with 429 **before
attempting authentication**. Successful authentication immediately clears that client's
failure record — a normal user experiencing token expiry with a few 401s is not counted
toward the limit.

Default is generous, unreachable by normal clients (60 failures within 60 seconds). To
adjust or disable, use ``KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT`` (≤0 disables).

**Client identification is TCP peer address**, unless the deployment names its reverse
proxy. ``X-Forwarded-For`` is forgeable, so it is not read by default: behind a proxy
every user then arrives from the proxy's address and shares one bucket (#1031).
``KPUBDATA_BUILDER_TRUSTED_PROXIES`` (comma-separated addresses or CIDR blocks) says
which peers are proxies. Only when the TCP peer is one of them is the header read, and
then from the right: the rightmost address that is not itself a trusted proxy is the
client — the part of the header the trusted proxy wrote, not the part the client sent.

**An expired token is not a failure.** Its signature verified; it is only old, and the
client's next step is to refresh it. Counting it let ordinary expiry across the users
behind one proxy lock everyone out (#1031). The caller leaves it out.
"""

from __future__ import annotations

import ipaddress
import logging
import math
import os
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence

_logger = logging.getLogger(__name__)

_LIMIT_ENV = "KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT"
_WINDOW_ENV = "KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS"
TRUSTED_PROXIES_ENV = "KPUBDATA_BUILDER_TRUSTED_PROXIES"

_Network = ipaddress.IPv4Network | ipaddress.IPv6Network
_Address = ipaddress.IPv4Address | ipaddress.IPv6Address

_DEFAULT_LIMIT = 60
_DEFAULT_WINDOW_SECONDS = 60.0
# Upper bound on tracked client count — prevent the throttle itself from becoming a
# memory amplification vector. An attacker spreading failures across arbitrary IPs
# cannot create an unbounded dict.
_DEFAULT_MAX_CLIENTS = 4096


def _positive_int_env(name: str, default: int) -> int:
    """Read env as int; use default if empty or malformed (don't fail startup)."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _address(text: str | None) -> _Address | None:
    """``text`` as an address, an IPv4-mapped IPv6 one as its IPv4; None if it is not one."""
    if not text:
        return None
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def parse_trusted_proxies(raw: str) -> tuple[_Network, ...]:
    """The networks ``raw`` names. An entry that is not an address or CIDR block is
    dropped with a warning: startup does not fail, and what is left trusts less, not
    more."""
    networks: list[_Network] = []
    for position, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            # The position, not the text: a variable set by mistake to something else
            # must not end up in the log.
            _logger.warning(
                "%s: ignoring entry %d — not an address or CIDR block",
                TRUSTED_PROXIES_ENV,
                position,
            )
    return tuple(networks)


def client_identity(
    peer: str | None, forwarded_for: Sequence[str], trusted: Sequence[_Network]
) -> str | None:
    """Who to count a request against: the TCP peer, or the client a trusted proxy names.

    ``forwarded_for`` is every ``X-Forwarded-For`` header value, in order. It is read only
    when ``peer`` is in ``trusted``, and from the right, skipping trusted proxies: each
    proxy appends the address it saw, so what a client put in the header itself sits to
    the left of what the trusted proxy wrote and is never reached. If the proxy wrote
    nothing usable, the peer is the identity — as without the setting.
    """
    peer_address = _address(peer)
    if peer_address is None or not any(peer_address in network for network in trusted):
        return peer
    hops = [hop for value in forwarded_for for hop in value.split(",")]
    for hop in reversed(hops):
        address = _address(hop)
        if address is None:
            return peer
        if not any(address in network for network in trusted):
            return str(address)
    return peer


class AuthFailureThrottle:
    """In-process throttle counting authentication failures per-client in sliding window.

    One instance per ``BuilderService`` to keep test state separate (same reason as
    ``LatencyRecorder``). Process-local, so multi-instance deployments count per
    instance — not exact global limit, but designed for abuse mitigation.
    """

    def __init__(
        self,
        *,
        limit: int | None = None,
        window_seconds: float | None = None,
        max_clients: int = _DEFAULT_MAX_CLIENTS,
        time_source: Callable[[], float] = time.monotonic,
        trusted_proxies: Sequence[_Network] | None = None,
    ) -> None:
        self._limit = limit if limit is not None else _positive_int_env(_LIMIT_ENV, _DEFAULT_LIMIT)
        self._window = (
            window_seconds
            if window_seconds is not None
            else _positive_float_env(_WINDOW_ENV, _DEFAULT_WINDOW_SECONDS)
        )
        self._max_clients = max_clients
        self._now = time_source
        self._trusted_proxies = (
            tuple(trusted_proxies)
            if trusted_proxies is not None
            else parse_trusted_proxies(os.environ.get(TRUSTED_PROXIES_ENV, ""))
        )
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        """If limit ≤0, disabled — all calls become no-op."""
        return self._limit > 0

    def client_id(self, peer: str | None, forwarded_for: Sequence[str] = ()) -> str | None:
        """The identity to count this request against (see :func:`client_identity`)."""
        return client_identity(peer, forwarded_for, self._trusted_proxies)

    def retry_after(self, client_id: str | None) -> int | None:
        """If client should be blocked now, return remaining wait time (seconds, ceiling).

        Return None if no blocking needed. If client cannot be identified (``client_id``
        is ``None``), do not throttle — don't block legitimate requests due to
        identification failure.
        """
        if not self.enabled or client_id is None:
            return None
        with self._lock:
            timestamps = self._failures.get(client_id)
            if timestamps is None:
                return None
            now = self._now()
            self._prune(timestamps, now)
            if not timestamps:
                del self._failures[client_id]
                return None
            if len(timestamps) < self._limit:
                return None
            # The oldest failure must exit the window before capacity frees up.
            remaining = timestamps[0] + self._window - now
            return max(1, math.ceil(remaining))

    def record_failure(self, client_id: str | None) -> None:
        """Record one auth failure (401 class only — 403/503 excluded by caller)."""
        if not self.enabled or client_id is None:
            return
        with self._lock:
            now = self._now()
            timestamps = self._failures.get(client_id)
            if timestamps is None:
                self._evict_if_needed(now)
                timestamps = deque()
                self._failures[client_id] = timestamps
            self._prune(timestamps, now)
            timestamps.append(now)

    def record_success(self, client_id: str | None) -> None:
        """Authentication success — clear failure record for this client."""
        if not self.enabled or client_id is None:
            return
        with self._lock:
            self._failures.pop(client_id, None)

    def _prune(self, timestamps: deque[float], now: float) -> None:
        cutoff = now - self._window
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    def _evict_if_needed(self, now: float) -> None:
        """Evict expired entries first when limit hit; if still over, evict oldest."""
        if len(self._failures) < self._max_clients:
            return
        cutoff = now - self._window
        expired = [
            key for key, stamps in self._failures.items() if not stamps or stamps[-1] <= cutoff
        ]
        for key in expired:
            del self._failures[key]
        while len(self._failures) >= self._max_clients:
            oldest = min(self._failures, key=lambda key: self._failures[key][-1])
            del self._failures[oldest]


__all__ = [
    "TRUSTED_PROXIES_ENV",
    "AuthFailureThrottle",
    "client_identity",
    "parse_trusted_proxies",
]

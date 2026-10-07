"""Provider keys that live only as long as a request or a job (#683).

ADR 0012's 2026-09-30 amendment (D1): in a multi-user deployment a provider key exists
only while the request or job that needs it runs. It arrives in a request header — never a
URL query — and is held in memory:

- **For a request**, in a context variable set by ``dispatch`` and reset when the request
  ends (``request_scope``).
- **For an async job**, in ``JobCredentials``, bound to the run id when the job is
  submitted and removed when the worker takes it, when the job ends whichever way, or when
  its time-to-live passes. Only the keys the job's spec uses are bound. Nothing here is
  written to disk, to the job registry's snapshot or to an event, so a dump of any of them
  holds no key, and a restart forgets every key — an interrupted job then fails as
  ``credentials_required``.

A job's keys are kept on two clocks (#1070). While it **waits**, they are held for at
most ``KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS``, and a timer removes them when that
passes — not the next time someone asks — so a job nobody runs does not keep a key. Once
a worker **takes** them they live as long as the build does and are dropped when it ends;
how long a build may run is not decided here.

A single-user deployment does not use any of this; its stored and environment
credentials work as before (ADR 0012's main text).
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

#: The request header that carries provider keys: ``<provider>=<key>``, one per header
#: or comma-separated. A key never travels in a URL, where proxies and logs keep it.
PROVIDER_KEY_HEADER = "X-Provider-Key"
#: How long a submitted job's keys wait for a worker before they are dropped. Counted
#: from submission and ended by a timer: the queue has no time limit of its own.
CREDENTIAL_TTL_ENV = "KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS"
_DEFAULT_TTL_SECONDS = 3600.0

_request_keys: ContextVar[Mapping[str, str] | None] = ContextVar(
    "kpubdata_request_provider_keys", default=None
)


def parse_provider_key_headers(values: Iterable[str]) -> dict[str, str]:
    """``X-Provider-Key`` header values → ``{provider: key}``.

    A key cannot contain a comma: the comma separates entries, so the part after it is
    read as another ``<provider>=<key>`` and refused. Studio refuses such a key when it
    is typed (``providerKeyProblem``), by the same rule.

    Raises:
        ValueError: A value is not ``<provider>=<key>``, or names a provider twice with
            different keys. The message never contains a key.
    """
    keys: dict[str, str] = {}
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            provider, separator, key = item.partition("=")
            provider, key = provider.strip().lower(), key.strip()
            if not separator or not provider or not key:
                raise ValueError(f"{PROVIDER_KEY_HEADER} must be '<provider>=<key>'")
            if keys.get(provider, key) != key:
                raise ValueError(f"{PROVIDER_KEY_HEADER} gives {provider!r} two different keys")
            keys[provider] = key
    return keys


def route_reads_provider_keys(method: str, path: str) -> bool:
    """Whether the route uses the request's provider keys — the operations the contract
    declares ``X-Provider-Key`` on (a test holds the two together).

    A malformed header is an error only there (#1073). Elsewhere the request carries no
    keys, exactly as if the header were absent.
    """
    if method == "POST" and path in ("/preview", "/build", "/builds"):
        return True
    if path == "/providers":
        return method == "GET"
    if path.startswith("/providers/"):
        operation = path.rsplit("/", 1)[-1]
        return (method, operation) in (("GET", "status"), ("POST", "test"), ("POST", "probe"))
    return False


@contextmanager
def request_scope(keys: Mapping[str, str] | None) -> Iterator[None]:
    """Make ``keys`` the current request's provider keys until the block ends."""
    token = _request_keys.set(dict(keys) if keys else None)
    try:
        yield
    finally:
        _request_keys.reset(token)


def current_key(provider: str) -> str | None:
    """The current request's key for ``provider``, or None."""
    keys = _request_keys.get()
    return None if keys is None else keys.get(provider.lower())


def current_keys() -> dict[str, str]:
    """A copy of every key the current request carries."""
    keys = _request_keys.get()
    return dict(keys) if keys else {}


def _ttl_seconds() -> float:
    raw = os.environ.get(CREDENTIAL_TTL_ENV, "").strip()
    try:
        value = float(raw) if raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    return value if value > 0 else _DEFAULT_TTL_SECONDS


@dataclass
class _Binding:
    owner_id: str | None
    keys: dict[str, str] = field(repr=False)
    expires_at: float = 0.0
    timer: threading.Timer | None = field(default=None, repr=False)


class JobCredentials:
    """In-memory provider keys for submitted async jobs, by run id.

    ``repr`` never shows a key. ``take`` hands the keys to the worker exactly once and
    forgets them; ``discard`` forgets them whatever happened. A binding that has waited
    its time-to-live is removed by its own timer (#1070): checking the clock only in
    ``take`` left the keys of a job that never reached a worker in memory for as long as
    the queue took.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._bindings: dict[str, _Binding] = {}

    def __repr__(self) -> str:
        with self._lock:
            return f"JobCredentials(runs={sorted(self._bindings)})"

    def bind(self, run_id: str, owner_id: str | None, keys: Mapping[str, str]) -> None:
        if not keys:
            return
        ttl = _ttl_seconds()
        binding = _Binding(owner_id=owner_id, keys=dict(keys), expires_at=time.monotonic() + ttl)
        timer = threading.Timer(ttl, self._expire, args=(run_id, binding))
        timer.daemon = True
        binding.timer = timer
        with self._lock:
            replaced = self._bindings.pop(run_id, None)
            self._bindings[run_id] = binding
        if replaced is not None:
            _forget(replaced)
        timer.start()

    def take(self, run_id: str, owner_id: str | None) -> dict[str, str] | None:
        """The job's keys, once; None when absent, expired or bound to another owner."""
        with self._lock:
            binding = self._bindings.pop(run_id, None)
        if binding is None:
            return None
        if binding.timer is not None:
            binding.timer.cancel()
        if binding.owner_id != owner_id or time.monotonic() > binding.expires_at:
            binding.keys.clear()
            return None
        return binding.keys

    def discard(self, run_id: str) -> None:
        with self._lock:
            binding = self._bindings.pop(run_id, None)
        if binding is not None:
            _forget(binding)

    def _expire(self, run_id: str, binding: _Binding) -> None:
        """The timer's end: drop ``binding`` if it is still the one held for ``run_id``."""
        with self._lock:
            if self._bindings.get(run_id) is not binding:
                return
            del self._bindings[run_id]
        binding.keys.clear()

    def holds(self, run_id: str) -> bool:
        """Whether keys are still held for ``run_id`` — for tests of the cleanup paths."""
        with self._lock:
            return run_id in self._bindings


def _forget(binding: _Binding) -> None:
    """Stop the binding's timer and empty it, so no reference to it still holds a key."""
    if binding.timer is not None:
        binding.timer.cancel()
    binding.keys.clear()


__all__ = [
    "CREDENTIAL_TTL_ENV",
    "PROVIDER_KEY_HEADER",
    "JobCredentials",
    "current_key",
    "current_keys",
    "parse_provider_key_headers",
    "request_scope",
]

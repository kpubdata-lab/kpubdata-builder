"""Keep provider keys out of log records (#686).

The canary gate found the leak this closes: ``httpx`` logs every request at INFO as
``HTTP Request: GET <url>``, and a data.go.kr URL carries the user's key as the
``serviceKey`` query parameter. kpubdata masks the URLs it writes itself, but this line
is the HTTP library's, so any deployment logging at INFO wrote users' keys to its logs
(docs/CREDENTIAL_SURFACE.md, item 9: there was no redaction filter at all).

Two ways a key can sit in a message, two rules:

- **By name** — a query parameter whose name kpubdata treats as a credential
  (``kpubdata.transport._sensitive.SENSITIVE_PARAM_KEYS``, the one canonical list;
  builder keeps no copy of it). Covers keys sent as ``?serviceKey=...``.
- **By value** — the keys of clients that are open right now. Some providers put the
  key in a path segment (kpubdata #354), where no parameter name marks it. Builder
  registers a client's keys when it creates the client and releases them when it
  closes it, so the set holds only keys in use and nothing outlives the request.

Records are rewritten, never dropped.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from collections.abc import Iterable
from urllib.parse import quote, quote_plus

from kpubdata.transport._sensitive import SENSITIVE_PARAM_KEYS

REDACTED = "[REDACTED]"

_QUERY_PARAM = re.compile(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>[^&\s\"'<>#]+)")

_lock = threading.Lock()
_active: Counter[str] = Counter()
_by_owner: dict[int, tuple[str, ...]] = {}


def _forms(secret: str) -> set[str]:
    """The secret and the encodings a URL or a JSON dump would give it."""
    return {secret, quote(secret, safe=""), quote_plus(secret)}


def redact(text: str) -> str:
    """``text`` with credential query values and active keys replaced."""

    def by_name(match: re.Match[str]) -> str:
        if match.group("name").casefold() in SENSITIVE_PARAM_KEYS:
            return f"{match.group('name')}={REDACTED}"
        return match.group(0)

    redacted = _QUERY_PARAM.sub(by_name, text)
    with _lock:
        values = sorted(
            {form for secret in _active for form in _forms(secret)}, key=len, reverse=True
        )
    for value in values:
        if value:
            redacted = redacted.replace(value, REDACTED)
    return redacted


def register(owner: object, secrets: Iterable[str]) -> None:
    """Treat ``secrets`` as active while ``owner`` (a client) is open."""
    values = tuple(s for s in secrets if s)
    if not values:
        return
    with _lock:
        release_locked(owner)
        _by_owner[id(owner)] = values
        _active.update(values)


def release(owner: object) -> None:
    """Forget the secrets registered for ``owner``; nothing happens if there were none."""
    with _lock:
        release_locked(owner)


def release_locked(owner: object) -> None:
    values = _by_owner.pop(id(owner), ())
    _active.subtract(values)
    for value in values:
        if _active[value] <= 0:
            del _active[value]


def active_count() -> int:
    """How many distinct keys are registered — for tests that check nothing lingers."""
    with _lock:
        return len(_active)


def scrub(record: logging.LogRecord) -> logging.LogRecord:
    """Rewrite ``record`` in place so its message and traceback carry no provider key."""
    try:
        message = record.getMessage()
    except Exception:
        # A record whose arguments do not fit its format string is the handler's
        # problem to report; leave it exactly as it came.
        return record
    cleaned = redact(message)
    if cleaned != message:
        record.msg = cleaned
        record.args = None
    if record.exc_info and not record.exc_text:
        record.exc_text = logging.Formatter().formatException(record.exc_info)
    if record.exc_text:
        record.exc_text = redact(record.exc_text)
    return record


_install_lock = threading.Lock()
_installed = False


def install() -> None:
    """Scrub every log record the process creates, from any logger. Idempotent.

    A filter on a logger sees only records created on that logger, and a filter on a
    handler only handlers that existed when it was added — both miss records in the
    usual deployment, where the HTTP stack logs under ``httpx`` and handlers are
    configured later. The record factory is the one place every record passes, so the
    rewrite happens there, before any handler formats it.
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        previous = logging.getLogRecordFactory()

        def factory(*args: object, **kwargs: object) -> logging.LogRecord:
            return scrub(previous(*args, **kwargs))

        logging.setLogRecordFactory(factory)
        _installed = True


__all__ = [
    "REDACTED",
    "SENSITIVE_PARAM_KEYS",
    "active_count",
    "install",
    "redact",
    "register",
    "release",
    "scrub",
]

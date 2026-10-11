"""Two lines per build: when it started, and how and when it ended (#1100).

A request line (``request_log.py``) says that a build was submitted and how that request
was answered. An async build then runs on a worker long after the answer, and nothing
said when it started, how long it ran or how it ended: the two lines here do, joined by
the run.

**What a line holds is decided by what this module is given, as in the request log.** It
is given no spec, no response body, no error message and no exception — only the run's
id, the owner's id (written as the request log's opaque ``owner``), a status that is one
of three words, a ``code`` that is kept only when it is one of Builder's
(``request_log.KNOWN_CODES``), and the *class name* of an unhandled exception. An
exception's text can quote a provider's URL, key included, so it never comes here.

A run id is the client's to choose. One that has the form Builder generates is written
as ``run_id``; any other is text a client wrote, so the line carries ``run_ref`` instead
— the first 16 hex digits of the id's SHA-256. Whoever knows the id can compute the
reference and find the lines; the lines do not hold what the client wrote.

The lines leave through standard logging under ``kpubdata_builder.build``, and to stderr
when the deployment attached no handler of its own, as the request lines do.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Literal

from . import request_log

__all__ = ["BuildMode", "BuildOutcome", "ended", "run_reference", "started"]

BuildMode = Literal["async", "sync"]
BuildOutcome = Literal["succeeded", "failed", "cancelled"]

_build_logger = logging.getLogger("kpubdata_builder.build")
# The service configures no logging, so the root's WARNING threshold would drop every
# line: the logger sets its own, as the request logger does.
_build_logger.setLevel(logging.INFO)

#: The two forms Builder gives a run id it makes itself: ``jobs.generate_run_id`` and
#: ``BuildContext.create``. Digits, and twelve hex digits: nothing a client could say.
_GENERATED_RUN_ID = re.compile(r"\d{8}T\d{12}Z(?:-[0-9a-f]{12})?")

_OUTCOMES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})
_MODES: frozenset[str] = frozenset({"async", "sync"})
#: An exception's class name, as Python allows one to be written.
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,79}")

_fallback_handler: logging.Handler | None = None
_output_lock = threading.Lock()


def run_reference(run_id: str) -> str:
    """What a line says of a run id a client chose: a digest, not the id."""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:16]


def _run_fields(run_id: str) -> dict[str, object]:
    if _GENERATED_RUN_ID.fullmatch(run_id):
        return {"run_id": run_id}
    return {"run_ref": run_reference(run_id)}


def _ensure_output() -> None:
    """Attach a stderr handler when no handler is anywhere in the chain.

    Decided at the first line, not at import, for the reason the request log gives: a
    handler attached before the deployment configured logging would write every line
    twice from then on.
    """
    global _fallback_handler
    if _fallback_handler is not None:
        return
    with _output_lock:
        if _fallback_handler is not None:
            return
        logger: logging.Logger | None = _build_logger
        while logger is not None:
            if logger.handlers:
                return
            if not logger.propagate:
                break
            logger = logger.parent
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        # The line is JSON and carries its own time: nothing is put before it.
        handler.setFormatter(logging.Formatter("%(message)s"))
        _build_logger.addHandler(handler)
        _fallback_handler = handler


def _write(event: str, run_id: str, mode: str, owner_id: str | None, **fields: object) -> None:
    try:
        line: dict[str, object] = {
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "event": event,
            **_run_fields(run_id),
            "mode": mode if mode in _MODES else None,
            "owner": request_log.opaque_owner(owner_id) if owner_id else None,
            **fields,
        }
        _ensure_output()
        _build_logger.info(json.dumps(line, ensure_ascii=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - a line that cannot be written must not fail the build
        return


def started(run_id: str, *, mode: BuildMode, owner_id: str | None = None) -> float:
    """Write the line of a build that is starting now.

    Returns:
        The clock reading to hand :func:`ended`, which measures the duration from it.
    """
    _write("build_started", run_id, mode, owner_id)
    return time.monotonic()


def ended(
    run_id: str,
    *,
    mode: BuildMode,
    status: BuildOutcome,
    started_at: float,
    owner_id: str | None = None,
    code: object = None,
    error_type: str | None = None,
    time_limit: bool = False,
) -> None:
    """Write the line of a build that has ended.

    Args:
        run_id: The run. Written as it is only when Builder made it.
        mode: Whether a worker ran it (``async``) or a request thread (``sync``).
        status: How it ended. A word that is not one of the three is written as null.
        started_at: What :func:`started` returned.
        owner_id: The owner's id; the line holds the request log's opaque ``owner``.
        code: The ``code`` of the build's answer, when it has one. One of
            ``request_log.KNOWN_CODES`` is written as it is, any other text as ``other``.
        error_type: The class name of an exception nothing handled. Never its text.
        time_limit: The build was cancelled because it ran past the time limit.
    """
    fields: dict[str, object] = {
        "status": status if status in _OUTCOMES else None,
        "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
    }
    kept = request_log.code_of(code)
    if kept is not None:
        fields["code"] = kept
    if error_type is not None:
        fields["error_type"] = error_type if _CLASS_NAME.fullmatch(error_type) else "other"
    if time_limit:
        fields["reason"] = "time_limit"
    _write("build_ended", run_id, mode, owner_id, **fields)

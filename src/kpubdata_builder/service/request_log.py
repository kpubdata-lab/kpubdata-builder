"""One line per request: when, which route, how it ended, how long it took (#1100).

Builder wrote nothing for a request that succeeded, so after an outage or an incident
nobody could say which requests had come in or how they had ended.

**What a line holds is decided by what this module is given, and it is given no header,
no query, no body and no path.** A path carries run ids, table names and file names; a
query, a body and a header can carry a key. The line names the *route* — the contract's
template, ``/builds/{run_id}`` — and a path no route matches is ``unmatched``, never the
path itself. The only text of the answer it keeps is ``code``, which is Builder's own
fixed vocabulary (``auth_throttled``, ``provider_credential_required``, …).

``owner`` says that two lines are the same user without saying who: a keyed hash of the
principal's ``owner_id``, under a key made when the process starts and kept nowhere. It
cannot be turned back into the id, and it cannot be matched across a restart — a hash
without a key, or under a key anyone can read, would only be the id written another way.

Where the lines go and how long they are kept is the deployment's: they leave through
standard logging under ``kpubdata_builder.request``, and to stderr when the deployment
attached no handler of its own, as the admin audit does (``admin_audit.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import threading
from contextvars import ContextVar
from datetime import datetime, timezone

from ._contract_operations import OPERATIONS
from .auth import Principal

__all__ = ["UNMATCHED_ROUTE", "begin", "note_principal", "record", "route_of"]

_request_logger = logging.getLogger("kpubdata_builder.request")
# The service configures no logging, so the root's WARNING threshold would drop every
# line: the logger sets its own, as the admin audit logger does.
_request_logger.setLevel(logging.INFO)

#: What a line says of a path no route of the contract matches.
UNMATCHED_ROUTE = "unmatched"

#: The key of ``owner``. Made here, once per process, and written nowhere.
_OWNER_KEY = secrets.token_bytes(32)

#: The requester of the request this thread is answering: (kind, owner).
_requester: ContextVar[tuple[str, str | None] | None] = ContextVar(
    "kpubdata_builder_request_log_requester", default=None
)

_fallback_handler: logging.Handler | None = None
_output_lock = threading.Lock()

#: ``code`` is copied from an answer only when it looks like one of Builder's codes.
_CODE_MAX_LENGTH = 64

_ROUTES: dict[str, list[tuple[tuple[str, ...], str]]] = {}
for _method, _template, *_rest in OPERATIONS:
    _ROUTES.setdefault(_method, []).append((tuple(_template.strip("/").split("/")), _template))


#: The parameters that stand for the rest of a path, slashes and all.
_REST_PARAMETERS = frozenset({"{file_path}"})


def _is_parameter(segment: str) -> bool:
    return segment.startswith("{") and segment.endswith("}")


def route_of(method: str, path: str) -> str:
    """The contract's template for ``path``, or :data:`UNMATCHED_ROUTE`.

    A literal segment wins over a parameter (``/warehouse/tables/{name}/profile`` over a
    table called ``profile``). An artifact's file path has slashes in it, so that one
    parameter takes the rest of the path.
    """
    segments = tuple(path.strip("/").split("/"))
    best: tuple[int, str] | None = None
    for template, name in _ROUTES.get(method.upper(), ()):
        if len(template) == len(segments):
            exact = True
        elif len(template) < len(segments) and template[-1] in _REST_PARAMETERS:
            exact = False
        else:
            continue
        head = template if exact else template[:-1]
        if any(not _is_parameter(t) and t != s for t, s in zip(head, segments, strict=False)):
            continue
        literals = sum(1 for t in template if not _is_parameter(t))
        score = literals * 2 + (1 if exact else 0)
        if best is None or score > best[0]:
            best = (score, name)
    return best[1] if best is not None else UNMATCHED_ROUTE


def _opaque(owner_id: str) -> str:
    return hmac.new(_OWNER_KEY, owner_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def begin() -> None:
    """Forget the requester of the request this thread answered before."""
    _requester.set(None)


def note_principal(principal: Principal) -> None:
    """Remember who the request being answered is from, for its line."""
    owner = principal.owner_id
    _requester.set((principal.kind, _opaque(owner) if owner else None))


def _ensure_output() -> None:
    """Attach a stderr handler when no handler is anywhere in the chain.

    Decided at the first line, not at import: the deployment may configure logging
    after this module is loaded, and a handler attached early would write every line
    twice from then on.
    """
    global _fallback_handler
    if _fallback_handler is not None:
        return
    with _output_lock:
        if _fallback_handler is not None:
            return
        logger: logging.Logger | None = _request_logger
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
        _request_logger.addHandler(handler)
        _fallback_handler = handler


def _code_of(code: object) -> str | None:
    if not isinstance(code, str) or not code or len(code) > _CODE_MAX_LENGTH:
        return None
    return code if code.replace("_", "").isalnum() and code.isascii() else None


def record(
    *,
    request_id: str,
    method: str,
    route: str,
    status: int,
    duration_ms: float,
    code: object = None,
) -> None:
    """Write the line of a request that has been answered.

    Args:
        request_id: The id the answer carries in ``X-Request-ID``.
        method: The HTTP method.
        route: :func:`route_of`'s answer — a template, not a path.
        status: The HTTP status of the answer.
        duration_ms: From the request line to the end of the answer.
        code: The answer's ``code``, when it has one. Anything that does not look like
            one of Builder's codes is left out.
    """
    requester = _requester.get()
    line: dict[str, object] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": "request",
        "request_id": request_id,
        "method": method,
        "route": route,
        "status": status,
        "duration_ms": round(duration_ms, 1),
        "principal": requester[0] if requester else None,
        "owner": requester[1] if requester else None,
    }
    kept = _code_of(code)
    if kept is not None:
        line["code"] = kept
    try:
        _ensure_output()
        _request_logger.info(json.dumps(line, ensure_ascii=True, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - a line that cannot be written must not fail the request
        return

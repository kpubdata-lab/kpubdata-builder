"""Administrator-only route adapter (#679).

**Only metadata is exposed here.** Run output bytes, user credentials, and
response bodies are never returned by any endpoint.

This product is BYOK — data received by users with their own keys is theirs. It is
not yet decided whether administrators should be able to access it (#679's
(a)/(b)/(c)). Until that is decided, we use the narrowest option (a) — expanding
later is possible, but narrowing after expanding cannot undo what was seen.

Administrators can:
    - View **status** of all runs (who, when, success)
    - View currently applied **policy configuration**

Cannot do:
    - View artifacts, view credentials, manipulate others' runs
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs

from ...spec import JsonValue
from .. import ownership as ownership_module
from .. import publish_credentials
from ..admin_audit import record_admin_action
from ..auth import Principal
from ..responses import ServiceResponse
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

#: Max runs returned by /admin/runs per call.
_MAX_LIMIT = 200

#: Terminal job statuses. Same set as _TERMINAL_STATUSES in jobs.py — that is
#: private, so we restate it here.
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _forbidden(principal: Principal, action: str) -> ServiceResponse:
    """Non-administrator request. **Denial is also recorded** — who knocked on
    the admin path is as important as who was allowed in."""
    record_admin_action(principal, action, outcome="denied")
    return ServiceResponse(403, {"error": "forbidden: administrator role required"})


def _parse_limit(query: str) -> int:
    raw = parse_qs(query).get("limit", ["50"])[-1]
    try:
        limit = int(raw)
    except ValueError:
        return 50
    return max(1, min(limit, _MAX_LIMIT))


def _admin_runs(service: BuilderService, principal: Principal, query: str) -> ServiceResponse:
    """Return status only for all owners' runs.

    Reads ``BuildIndex`` directly. ``service.list_builds`` strips ``owner_id``
    before response (#505), but for admins, **which user's run it is, is the
    essence of that information**. ``owner_id`` is a hash, irreversible; it
    distinguishes users without revealing identity itself — exactly the right
    level for admin purposes.
    """
    limit = _parse_limit(query)
    try:
        entries = service._build_index.list_builds(limit=limit)
    except Exception:
        # Index read failed; do not fall back to filesystem. Fallback has
        # less accurate owner info, and if admin UI shows it as fact, they
        # will make decisions on wrong grounds. Empty list is better.
        record_admin_action(principal, "admin.runs.list", outcome="index_unavailable")
        return ServiceResponse(503, {"error": "build index unavailable"})

    # run_id -> (sort key, response row). Registry jobs have no actual start time,
    # so use submission time for sort only, not in response. Completed index rows
    # are sorted by finish time per BuildIndex contract.
    rows: dict[str, tuple[str, dict[str, JsonValue]]] = {}
    # Add in-progress runs first. BuildIndex is filled only after manifest exists,
    # so queued/running jobs are not in the index at all — but stuck runs are what
    # operators look for. If there's an index entry for the same run_id, it wins
    # (terminal status is newer).
    for job in service._async_builds.list_all():
        # BuildJobSnapshot has created_at/updated_at. finished_at is meaningful
        # only for terminal jobs; leave it empty while running — using updated_at
        # as-is would read as "this running run just finished".
        finished = job.updated_at if job.status in _TERMINAL_JOB_STATUSES else None
        rows[job.run_id] = (
            finished or job.created_at,
            {
                "run_id": job.run_id,
                "status": job.status,
                "started_at": None,
                "finished_at": finished,
                "owner_id": job.owner_id,
            },
        )
    for entry in entries:
        rows[entry.run_id] = (
            entry.finished_at or "",
            {
                "run_id": entry.run_id,
                "status": entry.status,
                "started_at": entry.started_at,
                "finished_at": entry.finished_at,
                "owner_id": entry.owner_id,
            },
        )

    ordered = sorted(
        rows.items(),
        key=lambda item: (item[1][0], item[0]),
        reverse=True,
    )[:limit]
    runs: list[JsonValue] = [cast(JsonValue, row) for _, (_, row) in ordered]
    record_admin_action(principal, "admin.runs.list", target=f"limit={limit}")
    return ServiceResponse(200, {"runs": runs, "count": len(runs)})


def _admin_config(principal: Principal) -> ServiceResponse:
    """Currently applied policy configuration.

    Returns **status only**, not values — token, key, issuer URL are omitted.
    What operators need to know is "is ownership enforcement on?", not what value
    it is configured with.
    """
    record_admin_action(principal, "admin.config.read")
    return ServiceResponse(
        200,
        {
            "enforce_ownership": ownership_module.enforce_ownership(),
            "publish_server_credential_fallback": publish_credentials.server_fallback_allowed(),
        },
    )


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del body
    if not path.startswith("/admin/"):
        return None
    if method != "GET":
        return None

    if path == "/admin/runs":
        if not principal.is_admin:
            return _forbidden(principal, "admin.runs.list")
        return _admin_runs(service, principal, query)

    if path == "/admin/config":
        if not principal.is_admin:
            return _forbidden(principal, "admin.config.read")
        return _admin_config(principal)

    return None

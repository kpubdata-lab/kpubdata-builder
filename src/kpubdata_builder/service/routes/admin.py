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
from typing import TYPE_CHECKING, cast, get_args
from urllib.parse import parse_qs

from ... import logging_redaction
from ...manifest.endings import RUN_ENDING_CODES, RUN_ENDING_STAGES
from ...manifest.models import RunFailureStage
from ...pipeline.failures import SOURCE_FAILURE_REASONS, reason_sentence
from ...spec import JsonValue
from ...stages.gold.compose import COMPOSITION_FAILURE_SUMMARIES
from .. import ownership as ownership_module
from .. import publish_credentials
from ..admin_audit import record_admin_action
from ..auth import Principal
from ..build_runs_api import INTERRUPTED_CODE, SHUTDOWN_QUEUED_ERROR
from ..jobs import BuildJobSnapshot
from ..responses import ServiceResponse
from ..user_ledger import SignupStatus
from ._parsing import decode_segment
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


_USERS = "/admin/users"
_DECISIONS: dict[str, SignupStatus] = {"approve": "approved", "reject": "rejected"}


def _admin_users(service: BuilderService, principal: Principal, query: str) -> ServiceResponse:
    """The sign-up ledger (#785): hashed id, display name, status and when — nothing else.

    ``?status=pending|approved|rejected`` narrows it. No credential or token is kept in
    the ledger, so none can be returned.
    """
    raw = parse_qs(query).get("status", [None])[-1]
    if raw is not None and raw not in ("pending", "approved", "rejected"):
        return ServiceResponse(400, {"error": "status must be pending, approved or rejected"})
    entries = service._user_ledger().list(status=cast("SignupStatus | None", raw))
    record_admin_action(principal, "admin.users.list", target=f"status={raw or 'all'}")
    return ServiceResponse(
        200, {"users": [cast(JsonValue, e.body()) for e in entries], "count": len(entries)}
    )


def _decide_user(
    service: BuilderService, principal: Principal, path: str
) -> ServiceResponse | None:
    """``POST /admin/users/{user_id}/approve|reject`` (#785), recorded in the audit log."""
    raw_id, _, action = path[len(_USERS) + 1 :].partition("/")
    user_id = decode_segment(raw_id)
    if not user_id or action not in _DECISIONS:
        return None
    audit_action = f"admin.users.{action}"
    if not principal.is_admin:
        return _forbidden(principal, audit_action)
    entry = service._user_ledger().decide(
        user_id, _DECISIONS[action], by=principal.owner_id or principal.label
    )
    if entry is None:
        record_admin_action(principal, audit_action, target=user_id, outcome="not_found")
        return ServiceResponse(404, {"error": f"no such user: {user_id}", "code": "user_not_found"})
    record_admin_action(principal, audit_action, target=user_id)
    return ServiceResponse(200, entry.body())


def _parse_limit(query: str) -> int:
    raw = parse_qs(query).get("limit", ["50"])[-1]
    try:
        limit = int(raw)
    except ValueError:
        return 50
    return max(1, min(limit, _MAX_LIMIT))


def _reason(error: str | None) -> str | None:
    """A run's failure reason for an administrator, with anything key-shaped masked.

    The stored summary was already redacted when the run ended; masking again costs
    nothing and keeps a key out of the admin view if it ever was not.
    """
    return logging_redaction.redact(error) if error else None


_STAGES = ("bronze", "silver", "gold")
_COMMIT_REASONS = frozenset({"conflict", "empty_result", "table_exists", "commit_failed"})

#: Every code a failure is recorded with (#1120): a provider's refusal, any other source
#: failure, a composition's rule, a refused table commit, and why a run was cancelled or
#: ended without a manifest. ``error_code`` is one of these or null — a value the index
#: holds that is not one of them is not served.
FAILURE_CODES: frozenset[str] = frozenset(
    {
        *SOURCE_FAILURE_REASONS,
        "pipeline_failed",
        *COMPOSITION_FAILURE_SUMMARIES,
        *_COMMIT_REASONS,
        *RUN_ENDING_CODES,
    }
)
#: Every stage a failure is recorded at: a manifest's ``failures[].stage`` and how far a
#: run that left no manifest had got. ``error_stage`` is one of these or null.
FAILURE_STAGES: frozenset[str] = frozenset(get_args(RunFailureStage)) | RUN_ENDING_STAGES


def _allowed(value: str | None, allowed: frozenset[str]) -> str | None:
    """``value`` when it is one of ``allowed``; None otherwise."""
    return value if value in allowed else None


#: A job row's failure: the line, its code and its stage.
_JobFailure = tuple[str | None, str | None, str | None]


def _job_failure(job: BuildJobSnapshot) -> _JobFailure:
    """A job row's failure line for an administrator, made only of Builder's words (#1221).

    The job's ``error`` is written for the run's owner: a failed build's is the first
    failed source's or composition's message, which can name the data's columns or a
    join key's value. An administrator sees every owner's runs, so the line is built
    from what the build response carries in Builder's own vocabulary — a source key, a
    refusal reason, the stages completed, a commit's reason — as the index line is
    (#1219). A failure without a build response gets a fixed line by kind. The owner's
    ``GET /builds/{run_id}`` keeps the full message.

    The code and the stage are those the run's manifest records for the same failure
    (#1120), where the job row can tell them; None where it cannot.
    """
    if job.error is None and job.status != "failed":
        return None, None, None
    raw = getattr(job, "response", None)
    response = raw if isinstance(raw, dict) else {}
    outcomes = response.get("outcomes")
    if isinstance(outcomes, list):
        for outcome in outcomes:
            if not isinstance(outcome, dict) or outcome.get("status") != "failed":
                continue
            key = str(outcome.get("source_key") or "source")
            completed = outcome.get("stages_completed")
            done = completed if isinstance(completed, list) else []
            stage = next((s for s in _STAGES if s not in done), "export")
            reason = outcome.get("reason")
            if isinstance(reason, str) and (sentence := reason_sentence(reason)) is not None:
                return f"{key}: {sentence}", reason, stage
            return f"{key}: the source failed at the {stage} stage", "pipeline_failed", stage
    composition = response.get("composition")
    if isinstance(composition, dict) and composition.get("status") == "failed":
        name = str(composition.get("name") or "composition")
        line = f"{name}: {COMPOSITION_FAILURE_SUMMARIES['composition_failed']}"
        return line, "composition_failed", "composition"
    warehouse = response.get("warehouse_failures")
    if isinstance(warehouse, dict):
        for key, failure in warehouse.items():
            reason = failure.get("reason") if isinstance(failure, dict) else None
            known = reason if isinstance(reason, str) and reason in _COMMIT_REASONS else None
            suffix = f" ({known})" if known else ""
            return f"{key}: the table was not committed{suffix}", known, "warehouse"
    error = job.error or ""
    if error.startswith(f"{INTERRUPTED_CODE}:"):
        return (
            "the run lost its provider keys before it finished (credentials_required)",
            "credentials_required",
            "queued",
        )
    if error == SHUTDOWN_QUEUED_ERROR:
        return "the server shut down before this job started", "server_shutdown", "queued"
    if error.startswith("internal error"):
        return "the build failed with an internal error", None, None
    return "the build failed", None, None


def _job_failure_line(job: BuildJobSnapshot) -> str | None:
    """The line of ``_job_failure`` alone."""
    return _job_failure(job)[0]


def _admin_runs(service: BuilderService, principal: Principal, query: str) -> ServiceResponse:
    """Return metadata only for all owners' runs: status, times, failure reason, owner.

    ADR 0012's 2026-09-30 amendment (#679, option a): an administrator sees what a run
    is and how it ended, never its bytes. Every route that serves bytes — artifacts,
    stage samples, manifests, queries, warehouse tables — answers an administrator
    like any other user who does not own the run.

    Reads ``BuildIndex`` directly. ``service.list_builds`` strips ``owner_id``
    before response (#505), but for admins, **which user's run it is, is the
    essence of that information**. ``owner_id`` is a hash, irreversible; it
    distinguishes users without revealing identity itself — exactly the right
    level for admin purposes.
    """
    limit = _parse_limit(query)
    jobs = service._async_builds.list_all()
    try:
        entries = service._build_index.list_builds(limit=limit)
        # `total` (#948): every run this listing draws from, before `limit` — the index
        # counted, plus the registry's jobs it does not hold yet. Counting, not
        # listing, keeps it one COUNT and a key lookup however many runs there are.
        listed = {entry.run_id for entry in entries}
        total = service._build_index.count_builds(
            also=[job.run_id for job in jobs if job.run_id not in listed]
        )
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
    for job in jobs:
        # BuildJobSnapshot has created_at/updated_at. finished_at is meaningful
        # only for terminal jobs; leave it empty while running — using updated_at
        # as-is would read as "this running run just finished".
        finished = job.updated_at if job.status in _TERMINAL_JOB_STATUSES else None
        line, code, stage = _job_failure(job)
        rows[job.run_id] = (
            finished or job.created_at,
            {
                "run_id": job.run_id,
                "status": job.status,
                "started_at": None,
                "finished_at": finished,
                "owner_id": job.owner_id,
                # Never the job's own message, which is the owner's (#1221).
                "error": line,
                "error_code": _allowed(code, FAILURE_CODES),
                "error_stage": _allowed(stage, FAILURE_STAGES),
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
                "error": _reason(entry.error),
                # Stable vocabulary only (#1120): a client tells the cause and what to
                # do from these without parsing the line.
                "error_code": _allowed(entry.error_code, FAILURE_CODES),
                "error_stage": _allowed(entry.error_stage, FAILURE_STAGES),
            },
        )

    ordered = sorted(
        rows.items(),
        key=lambda item: (item[1][0], item[0]),
        reverse=True,
    )[:limit]
    runs: list[JsonValue] = [cast(JsonValue, row) for _, (_, row) in ordered]
    record_admin_action(principal, "admin.runs.list", target=f"limit={limit}")
    # A build that finishes between the list and the count can leave the count one
    # short; never report fewer runs than this response already merged.
    total = max(total, len(rows))
    return ServiceResponse(200, {"runs": runs, "count": len(runs), "total": total})


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
    if method == "POST" and path.startswith(_USERS + "/"):
        return _decide_user(service, principal, path)
    if method != "GET":
        return None

    if path == _USERS:
        if not principal.is_admin:
            return _forbidden(principal, "admin.users.list")
        return _admin_users(service, principal, query)

    if path == "/admin/runs":
        if not principal.is_admin:
            return _forbidden(principal, "admin.runs.list")
        return _admin_runs(service, principal, query)

    if path == "/admin/config":
        if not principal.is_admin:
            return _forbidden(principal, "admin.config.read")
        return _admin_config(principal)

    return None

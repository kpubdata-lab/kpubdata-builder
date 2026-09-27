"""Run event timeline route adapter (#496)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from .. import events as events_service
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_active_run_access
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_PREFIX = "/builds/"
_SUFFIX = "/events"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del body
    # Same order as "/stages" route (service/routes/stages.py): extract run_id from
    # segments[0] and validate first regardless of remaining path shape — unsafe run_id
    # like "../escape/events" must be rejected as 400 regardless of actual path shape
    # matched by this adapter (#317 conformance and same path-traversal defense convention).
    if method != "GET" or not path.startswith(_PREFIX) or _SUFFIX not in path:
        return None
    segments = path[len(_PREFIX) :].split("/")
    run_id = segments[0]
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    if len(segments) != 2 or segments[1] != "events":
        return None

    limit_or_error = _parse_limit(query)
    if isinstance(limit_or_error, ServiceResponse):
        return limit_or_error
    tail_or_error = _parse_tail(query)
    if isinstance(tail_or_error, ServiceResponse):
        return tail_or_error

    # Order same as other /builds/{run_id}/* routes (#488 convention, #496 follows):
    # run_id validation -> existence/ownership check -> actual query. Cross-owner access
    # must be blocked as 403 before reaching events query logic, so run existence
    # does not leak through this endpoint.
    #
    # Existence/ownership determination handled by check_active_run_access
    # — must recognize not just persisted runs (manifest-based) but also active async jobs
    # (queued/running) without run directory/manifest yet, so events polling is not blocked
    # as 404/403 in that interval. Other /builds/{run_id}/* routes (manifest, stages etc.)
    # still only handle persisted runs, so do not use this helper.
    error = check_active_run_access(service, run_id, principal)
    return error or service.get_build_events(run_id, limit=limit_or_error, tail=tail_or_error)


def _parse_limit(query: str) -> int | ServiceResponse:
    query_params = parse_qs(query)
    if "limit" not in query_params:
        return events_service.DEFAULT_EVENTS_LIMIT
    raw_limit = query_params["limit"][-1]
    try:
        limit = int(raw_limit)
    except ValueError:
        return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
    if limit < 1 or limit > events_service.MAX_EVENTS_LIMIT:
        return ServiceResponse(
            400,
            {
                "error": (
                    f"'limit' must be a positive integer up to {events_service.MAX_EVENTS_LIMIT}"
                )
            },
        )
    return limit


def _parse_tail(query: str) -> bool | ServiceResponse:
    query_params = parse_qs(query)
    if "tail" not in query_params:
        return False
    raw_tail = query_params["tail"][-1]
    if raw_tail == "true":
        return True
    if raw_tail == "false":
        return False
    return ServiceResponse(400, {"error": "'tail' must be 'true' or 'false'"})

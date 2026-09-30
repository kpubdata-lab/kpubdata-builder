"""Build quality route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..auth import Principal
from ..quality_api import ISSUE_STATUSES
from ..responses import ServiceResponse
from ._guards import check_ownership, check_run_exists
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del body
    # Recent 24h cross-run quality aggregate (#486 follow-up, API 1.22.0). Treated
    # as same quality family as per-run /builds/{id}/quality so handled in same adapter —
    # fixed path not run_id path, and ownership determined by service per principal.
    if method == "GET" and path == "/quality/summary":
        window = parse_qs(query).get("window", ["24h"])[-1]
        return service.quality_summary(window=window, principal=principal)
    if method == "GET" and path == "/quality/issues":
        return _issues(service, query, principal)
    if method != "GET" or not path.startswith("/builds/") or not path.endswith("/quality"):
        return None
    run_id = path[len("/builds/") : -len("/quality")]
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    existence_error = check_run_exists(service, run_id)
    if existence_error is not None:
        return existence_error
    ownership_error = check_ownership(service, run_id, principal)
    return ownership_error or service.get_build_quality(run_id)


def _issues(service: BuilderService, query: str, principal: Principal) -> RouteResponse:
    """``GET /quality/issues`` (#843): parse filters; an empty or unknown value is 400."""
    params = parse_qs(query, keep_blank_values=True)
    statuses: set[str] = set()
    for raw in params.get("status", []):
        for value in raw.split(","):
            if value not in ISSUE_STATUSES:
                return ServiceResponse(
                    400, {"error": f"status must be among {', '.join(ISSUE_STATUSES)}"}
                )
            statuses.add(value)
    single: dict[str, str | None] = {}
    for name in ("dataset_id", "category", "cursor"):
        values = params.get(name)
        if values is not None and not values[-1]:
            return ServiceResponse(400, {"error": f"'{name}' must not be empty"})
        single[name] = values[-1] if values else None
    limit = 100
    if "limit" in params:
        try:
            limit = int(params["limit"][-1])
        except ValueError:
            limit = 0
        if not 1 <= limit <= 500:
            return ServiceResponse(400, {"error": "'limit' must be an integer from 1 to 500"})
    return service.list_quality_issues(
        principal=principal,
        statuses=frozenset(statuses) if statuses else None,
        dataset_id=single["dataset_id"],
        category=single["category"],
        limit=limit,
        cursor=single["cursor"],
    )

"""Build quality route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..auth import Principal
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

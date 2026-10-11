"""Build stage route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from .. import stages as stages_service
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_ownership, check_run_exists
from ._parsing import decode_segment
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
    if method != "GET" or not path.startswith("/builds/") or "/stages" not in path:
        return None
    segments = path[len("/builds/") :].split("/")
    run_id = decode_segment(segments[0])
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    if len(segments) == 2 and segments[1] == "stages":
        error = check_run_exists(service, run_id)
        if error is not None:
            return error
        error = check_ownership(service, run_id, principal)
        return error or service.list_run_stages(run_id)
    if len(segments) != 3 or segments[1] != "stages" or not segments[2]:
        return None

    stage = decode_segment(segments[2])
    if stage not in stages_service.STAGE_NAMES:
        return ServiceResponse(
            400, {"error": f"invalid stage: {stage!r}; must be one of bronze/silver/gold"}
        )
    query_params = parse_qs(query)
    source_values = query_params.get("source")
    if not source_values or not source_values[-1]:
        return ServiceResponse(400, {"error": "'source' query parameter is required"})
    source_key = source_values[-1]
    try:
        validate_path_segment(source_key, field_name="source")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    limit = stages_service.DEFAULT_STAGE_PREVIEW_LIMIT
    if "limit" in query_params:
        raw_limit = query_params["limit"][-1]
        try:
            limit = int(raw_limit)
        except ValueError:
            return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
        if limit < 1 or limit > stages_service.MAX_STAGE_PREVIEW_LIMIT:
            return ServiceResponse(
                400,
                {
                    "error": (
                        "'limit' must be a positive integer up to "
                        f"{stages_service.MAX_STAGE_PREVIEW_LIMIT}"
                    )
                },
            )
    error = check_run_exists(service, run_id)
    if error is not None:
        return error
    error = check_ownership(service, run_id, principal)
    return error or service.get_run_stage_detail(run_id, stage, source_key, limit=limit)

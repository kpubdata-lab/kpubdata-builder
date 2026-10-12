"""Build artifact route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_ownership
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
    del body, query
    if method != "GET" or not path.startswith("/artifacts/"):
        return None
    parts = path[len("/artifacts/") :].split("/", 1)
    run_id = decode_segment(parts[0])
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    ownership_error = check_ownership(service, run_id, principal)
    if ownership_error is not None:
        return ownership_error
    if len(parts) == 2 and parts[1]:
        return service.serve_artifact_file(run_id, parts[1])
    return service.artifacts(run_id)

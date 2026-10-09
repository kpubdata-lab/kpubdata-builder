"""Route input parsing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..responses import ServiceResponse


def spec_from_body(body: Mapping[str, JsonValue] | None) -> str | ServiceResponse:
    if not body or "spec" not in body:
        return ServiceResponse(400, {"error": "missing 'spec' in request body"})
    spec_value = body["spec"]
    if not isinstance(spec_value, str):
        return ServiceResponse(400, {"error": "'spec' must be a YAML string"})
    return spec_value


def positive_limit_query(query: str, *, default: int = 50) -> int | ServiceResponse:
    query_params = parse_qs(query)
    if "limit" not in query_params:
        return default
    raw_limit = query_params["limit"][-1]
    try:
        value = int(raw_limit)
    except ValueError:
        return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
    if value < 1:
        return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
    return value


def optional_run_id(body: Mapping[str, JsonValue] | None) -> str | None | ServiceResponse:
    if body is None or "run_id" not in body:
        return None
    run_id = body["run_id"]
    if not isinstance(run_id, str) or not run_id.strip():
        return ServiceResponse(400, {"error": "'run_id' must be a non-empty string"})
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    return run_id


def optional_if_absent(body: Mapping[str, JsonValue] | None) -> bool | ServiceResponse:
    """Whether the build's tables must be new (#1223); absent or null is False."""
    if body is None or body.get("if_absent") is None:
        return False
    value = body["if_absent"]
    if not isinstance(value, bool):
        return ServiceResponse(400, {"error": "'if_absent' must be true or false"})
    return value


def optional_retry_of(body: Mapping[str, JsonValue] | None) -> str | None | ServiceResponse:
    """The earlier run a build says it retries (#1042), when the body names one."""
    if body is None or "retry_of" not in body or body["retry_of"] is None:
        return None
    retry_of = body["retry_of"]
    if not isinstance(retry_of, str) or not retry_of.strip():
        return ServiceResponse(400, {"error": "'retry_of' must be a non-empty string"})
    try:
        validate_path_segment(retry_of, field_name="retry_of")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    return retry_of

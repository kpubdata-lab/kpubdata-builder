"""Build publish readiness/execution route adapter (#491)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_active_run_access
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_READINESS_SUFFIX = "/publish/readiness"
_PUBLISH_SUFFIX = "/publish"
_RECEIPT_SUFFIX = "/publish/receipt"
_RECONCILE_SUFFIX = "/publish/reconcile"
_AUDIT_SUFFIX = "/publish/audit"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    if not path.startswith("/builds/"):
        return None
    rest = path[len("/builds/") :]

    if method == "GET" and rest.endswith(_READINESS_SUFFIX):
        run_id = rest[: -len(_READINESS_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        query_params = parse_qs(query)
        target_values = query_params.get("target")
        if not target_values or not target_values[-1]:
            return ServiceResponse(400, {"error": "'target' query parameter is required"})
        target = target_values[-1]
        # destination is optional (#550) — destination-dependent checks like kaggle metadata
        # matching only appear in readiness if provided, and POST does final re-validation.
        destination_values = query_params.get("destination")
        destination = destination_values[-1] if destination_values else None
        # Same as #496 follow-up: determine existence/ownership regardless of manifest presence
        # (queued/running too) — publish readiness should report running/queued runs as
        # "not yet complete" blocker, not 404 (#491).
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        # To determine credential blocker per-requestor requires owner_id. Without it,
        # readiness sees only server environment variables, but POST blocks per-requestor
        # — two answers diverge.
        return service.publish_readiness(
            run_id, target, destination=destination, owner_id=principal.owner_id
        )

    if method == "GET" and rest.endswith(_RECEIPT_SUFFIX):
        run_id = rest[: -len(_RECEIPT_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        query_params = parse_qs(query)
        target = (query_params.get("target") or [""])[-1]
        destination = (query_params.get("destination") or [""])[-1]
        if not target or not destination:
            return ServiceResponse(
                400, {"error": "'target' and 'destination' query parameters are required"}
            )
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.get_publish_receipt(run_id, target, destination, principal=principal)

    if method == "GET" and rest.endswith(_AUDIT_SUFFIX):
        run_id = rest[: -len(_AUDIT_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.publish_audit_log(run_id, principal=principal)

    if method == "POST" and rest.endswith(_RECONCILE_SUFFIX):
        run_id = rest[: -len(_RECONCILE_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.reconcile_publish(run_id, body, principal=principal)

    if method == "DELETE" and rest.endswith(_RECEIPT_SUFFIX):
        run_id = rest[: -len(_RECEIPT_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        query_params = parse_qs(query)
        target = (query_params.get("target") or [""])[-1]
        destination = (query_params.get("destination") or [""])[-1]
        if not target or not destination:
            return ServiceResponse(
                400, {"error": "'target' and 'destination' query parameters are required"}
            )
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.reset_publish_receipt(run_id, target, destination, principal=principal)

    if method == "POST" and rest.endswith(_PUBLISH_SUFFIX):
        run_id = rest[: -len(_PUBLISH_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.publish(run_id, body, principal=principal)

    return None


def _validate_run_id(run_id: str) -> ServiceResponse | None:
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    return None

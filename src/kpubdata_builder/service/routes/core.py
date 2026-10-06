"""Service metadata and sync build route adapters."""

from __future__ import annotations

from collections.abc import Mapping
from functools import partial
from typing import TYPE_CHECKING

from ...pipeline import DEFAULT_PREVIEW_SEED
from ...spec import JsonValue
from ...tabular import DEFAULT_PREVIEW_LIMIT
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_existing_run_access, check_retry_of
from ._parsing import optional_retry_of, optional_run_id, spec_from_body
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

# Dataset is loaded into memory then sliced, so limit itself does not reduce fetch amount,
# but sample/diff size in response is clearly bounded by this value. Matches existing
# stage preview upper bound (MAX_STAGE_PREVIEW_LIMIT, service/stages.py).
MAX_PREVIEW_LIMIT = 1000


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del query
    if method == "GET" and path == "/version":
        return service.version()
    if method == "GET" and path == "/catalog":
        return service.catalog()
    if method == "POST" and path == "/validate":
        spec = spec_from_body(body)
        return spec if isinstance(spec, ServiceResponse) else service.validate(spec)
    if method == "POST" and path == "/preview":
        spec = spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        # If limit is specified, must be positive int - do not fall back to default
        # on bad value. Exceeding MAX_PREVIEW_LIMIT rejected as 400 by service.preview().
        if body is not None and "limit" in body:
            limit_value = body["limit"]
            # bool is int subtype, but limit makes no sense for bool, so reject.
            if (
                not isinstance(limit_value, int)
                or isinstance(limit_value, bool)
                or limit_value < 1
                or limit_value > MAX_PREVIEW_LIMIT
            ):
                return ServiceResponse(
                    400,
                    {"error": f"'limit' must be a positive integer up to {MAX_PREVIEW_LIMIT}"},
                )
            limit = limit_value
        else:
            limit = DEFAULT_PREVIEW_LIMIT
        # sample_mode/seed follow same principle: do not fall back to default on bad (#497).
        sample_mode = "first"
        if body is not None and "sample_mode" in body:
            sample_mode_value = body["sample_mode"]
            if not isinstance(sample_mode_value, str) or sample_mode_value not in (
                "first",
                "random",
            ):
                return ServiceResponse(400, {"error": "'sample_mode' must be 'first' or 'random'"})
            sample_mode = sample_mode_value
        seed = DEFAULT_PREVIEW_SEED
        if body is not None and "seed" in body:
            seed_value = body["seed"]
            if not isinstance(seed_value, int) or isinstance(seed_value, bool):
                return ServiceResponse(400, {"error": "'seed' must be an integer"})
            seed = seed_value
        return service.preview(
            spec, limit=limit, sample_mode=sample_mode, seed=seed, principal=principal
        )
    if method == "POST" and path == "/build":
        spec = spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        run_id = optional_run_id(body)
        if isinstance(run_id, ServiceResponse):
            return run_id
        # Caller could supply run_id directly without checking who it belongs to (#635).
        # Passing someone else's run_id would overwrite that run's output and return results
        # in response. Async POST /builds applies the same rule (#991).
        if run_id is not None:
            # 400 for an id that already ended: this route's 409 is a build response.
            denied = check_existing_run_access(
                service, run_id, principal, used_status=400, synchronous=True
            )
            if denied is not None:
                return denied
        retry_of = optional_retry_of(body)
        if isinstance(retry_of, ServiceResponse):
            return retry_of
        denied = check_retry_of(service, run_id, retry_of, principal)
        if denied is not None:
            return denied
        # ``retry_of`` is passed only when the request named one: a service that overrides
        # ``build`` with the signature it had before #1042 keeps working for every other
        # request.
        build = service.build if retry_of is None else partial(service.build, retry_of=retry_of)
        return build(
            spec,
            run_id=run_id,
            created_by=principal.label,
            owner_id=principal.owner_id,
            principal=principal,
        )
    return None

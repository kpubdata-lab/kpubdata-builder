"""Monitoring route adapters (#516)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ..auth import Principal
from ..responses import ServiceResponse
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
    if method == "GET" and path == "/metrics":
        # Administrators only (#1101): the page holds no user's data, but queue depth
        # and error rates are the deployment's, not every signed-in user's.
        if not principal.is_admin:
            return ServiceResponse(403, {"error": "forbidden: administrator role required"})
        return service.metrics_page()
    if method == "GET" and path == "/monitoring/summary":
        return service.monitoring_summary()
    if method == "GET" and path == "/monitoring/builds":
        query_params = parse_qs(query)
        window = query_params.get("window", ["24h"])[-1]
        bucket = query_params.get("bucket", ["hour"])[-1]
        return service.monitoring_builds(window=window, bucket=bucket, principal=principal)
    return None

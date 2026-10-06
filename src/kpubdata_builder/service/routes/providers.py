"""Provider route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import unquote

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
    del query
    if method == "GET" and path == "/providers":
        return service.providers(principal=principal)
    if not path.startswith("/providers/"):
        return None
    rest = path[len("/providers/") :]
    segments = rest.split("/")
    if len(segments) != 2 or not segments[0]:
        return ServiceResponse(404, {"error": f"not found: {method} {path}"})
    provider = unquote(segments[0]).strip().lower()
    operation = segments[1]
    if not provider:
        return ServiceResponse(400, {"error": "provider must not be empty"})
    if method == "GET" and operation == "status":
        return service.provider_status(provider, principal=principal)
    if method == "POST" and operation == "test":
        return service.provider_status(provider, principal=principal)
    if method == "POST" and operation == "probe":
        return service.probe_provider(provider, body, principal=principal)
    if method == "GET" and operation == "credential":
        return service.provider_credential(provider, principal=principal)
    if method == "PUT" and operation == "credential":
        return service.put_provider_credential(provider, body, principal=principal)
    if method == "DELETE" and operation == "credential":
        return service.delete_provider_credential(provider, principal=principal)
    return None

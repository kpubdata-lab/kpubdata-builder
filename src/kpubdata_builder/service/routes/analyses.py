"""Saved analysis route adapter (#783)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ...spec import JsonValue
from ..auth import Principal
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_PREFIX = "/analyses/"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del query
    if path == "/analyses":
        if method == "GET":
            return service.list_analyses(principal=principal)
        if method == "POST":
            return service.create_analysis(body, principal=principal)
        return None
    if not path.startswith(_PREFIX):
        return None
    rest = path[len(_PREFIX) :]
    analysis_id, _, action = rest.partition("/")
    if not analysis_id:
        return None
    if action == "run" and method == "POST":
        return service.run_analysis(analysis_id, principal=principal)
    if action:
        return None
    if method == "GET":
        return service.get_analysis(analysis_id, principal=principal)
    if method == "DELETE":
        return service.delete_analysis(analysis_id, principal=principal)
    return None

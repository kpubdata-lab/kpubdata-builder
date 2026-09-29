"""Warehouse table route adapter (#797)."""

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

_PREFIX = "/warehouse/tables/"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del query
    if method == "POST" and path == "/warehouse/query":
        return service.query_warehouse(body, principal=principal)
    if method == "POST" and path == "/warehouse/rows":
        return service.read_warehouse_rows(body, principal=principal)
    if method == "GET" and path == "/warehouse/tables":
        return service.list_warehouse_tables(principal=principal)
    if method == "GET" and path.startswith(_PREFIX):
        name = unquote(path[len(_PREFIX) :])
        if not name or "/" in name:
            return ServiceResponse(400, {"error": "table name must be one path segment"})
        return service.get_warehouse_table(name, principal=principal)
    return None

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
_EXPORTS = "/warehouse/exports"


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
    if method == "POST" and path == "/warehouse/aggregate":
        return service.aggregate_warehouse(body, principal=principal)
    if path == _EXPORTS or path.startswith(_EXPORTS + "/"):
        return _exports(service, method, path, body, principal)
    if method == "GET" and path == "/warehouse/tables":
        return service.list_warehouse_tables(principal=principal)
    if method == "GET" and path.startswith(_PREFIX):
        name = unquote(path[len(_PREFIX) :])
        if not name or "/" in name:
            return ServiceResponse(400, {"error": "table name must be one path segment"})
        return service.get_warehouse_table(name, principal=principal)
    return None


def _exports(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    principal: Principal,
) -> RouteResponse | None:
    """Query exports (#819): create and list, then one export, then its download."""
    if path == _EXPORTS:
        if method == "POST":
            return service.create_warehouse_export(body, principal=principal)
        if method == "GET":
            return service.list_warehouse_exports(principal=principal)
        return None
    parts = path[len(_EXPORTS) + 1 :].split("/")
    export_id = unquote(parts[0])
    if not export_id:
        return None
    if len(parts) == 1:
        if method == "GET":
            return service.get_warehouse_export(export_id, principal=principal)
        if method == "DELETE":
            return service.delete_warehouse_export(export_id, principal=principal)
        return None
    if len(parts) == 2 and parts[1] == "download" and method == "GET":
        return service.download_warehouse_export(export_id, principal=principal)
    return None

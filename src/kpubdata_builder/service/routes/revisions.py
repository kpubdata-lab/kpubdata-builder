"""Revision store route adapter (#820)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, unquote

from ...spec import JsonValue
from ..auth import Principal
from ..responses import ServiceResponse
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_PREFIX = "/revisions/"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    if not path.startswith(_PREFIX):
        return None
    parts = [unquote(p) for p in path[len(_PREFIX) :].split("/")]
    api = service._revisions_api
    if len(parts) == 2:
        kind, doc_id = parts
        if method == "PUT":
            return api.save(kind, doc_id, body, principal=principal)
        if method == "GET":
            raw = parse_qs(query).get("revision")
            revision: int | None = None
            if raw:
                try:
                    revision = int(raw[-1])
                except ValueError:
                    return ServiceResponse(400, {"error": "revision must be an integer"})
            return api.get(kind, doc_id, revision, principal=principal)
        return None
    if len(parts) == 3:
        kind, doc_id, action = parts
        if action == "history" and method == "GET":
            return api.history(kind, doc_id, principal=principal)
        if action == "revert" and method == "POST":
            return api.revert(kind, doc_id, body, principal=principal)
    return None

"""File source upload CRUD route (#498).

Unlike other route adapters, ``POST /uploads`` accepts raw binary body, not JSON.
Therefore, it is not placed in ``ROUTE_ADAPTERS`` (standard ``RouteAdapter``
signature, accepts JSON body only), but is called directly by ``app._dispatch_impl``
after authentication (``raw_body`` is the only endpoint that needs it). GET/DELETE
do not require binary body.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...stages._path_safety import validate_path_segment
from ..auth import Principal
from ..responses import ServiceResponse
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_PREFIX = "/uploads/"


def handle(
    service: BuilderService,
    method: str,
    path: str,
    principal: Principal,
    *,
    query: str,
    raw_body: bytes | None,
) -> RouteResponse | None:
    if method == "POST" and path == "/uploads":
        return _handle_create(service, principal, query=query, raw_body=raw_body)
    if method == "GET" and path == "/uploads":
        return service.list_uploads(principal=principal)

    if path.startswith(_PREFIX) and "/" not in path[len(_PREFIX) :]:
        upload_id = path[len(_PREFIX) :]
        try:
            validate_path_segment(upload_id, field_name="upload_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        if method == "GET":
            return service.get_upload(upload_id, principal=principal)
        if method == "DELETE":
            return service.delete_upload(upload_id, principal=principal)
    return None


def _handle_create(
    service: BuilderService, principal: Principal, *, query: str, raw_body: bytes | None
) -> RouteResponse:
    if not raw_body:
        return ServiceResponse(400, {"error": "request body must not be empty"})
    params = parse_qs(query)
    format_values = params.get("format")
    if not format_values or not format_values[-1].strip():
        return ServiceResponse(
            400, {"error": "'format' query parameter is required (csv/json/jsonl/parquet)"}
        )
    encoding_values = params.get("encoding")
    encoding = encoding_values[-1] if encoding_values else "utf-8"
    filename_values = params.get("filename")
    original_filename = filename_values[-1] if filename_values else None
    return service.create_upload(
        raw_body,
        format=format_values[-1],
        encoding=encoding,
        original_filename=original_filename,
        principal=principal,
    )


__all__ = ["handle"]

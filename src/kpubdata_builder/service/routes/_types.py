"""Route adapter common types."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from ...spec import JsonValue
from ..auth import Principal
from ..responses import FileResponse, ServiceResponse

if TYPE_CHECKING:
    from ..app import BuilderService

RouteResponse = ServiceResponse | FileResponse
RouteAdapter = Callable[
    ["BuilderService", str, str, Mapping[str, JsonValue] | None, str, Principal],
    RouteResponse | None,
]

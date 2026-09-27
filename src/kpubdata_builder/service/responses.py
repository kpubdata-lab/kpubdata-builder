"""Service response model independent of HTTP transport."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..spec import JsonValue


@dataclass(frozen=True)
class ServiceResponse:
    """Status code and JSON-serializable response body."""

    status_code: int
    body: dict[str, JsonValue]


@dataclass(frozen=True)
class FileResponse:
    """File serving response."""

    status_code: int
    file_path: Path
    filename: str

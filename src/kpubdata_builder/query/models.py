"""Read-only query request/result models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..spec import JsonValue

QueryStage = Literal["silver", "gold"]


@dataclass(frozen=True)
class QueryRequest:
    dataset_id: str
    run_id: str
    stage: QueryStage
    sql: str
    source: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[dict[str, JsonValue], ...]
    truncated: bool
    execution_ms: int
    startup_ms: int
    engine_execution_ms: int
    # Per column: name, logical_type, wire_encoding (#735). Tells a client which columns
    # arrive as exact decimal text rather than JSON numbers.
    column_meta: tuple[dict[str, JsonValue], ...] = ()


__all__ = ["QueryRequest", "QueryResult", "QueryStage"]

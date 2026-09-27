"""Internal protocol for tabular engine (#49).

Explicitly documents engine surface that Silver phase depends on as structural type. Not public API,
for internal contract documentation/type checking; current implementation is polars_engine module
function set. Per single-engine (Polars) principle, no dual-engine abstraction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import polars as pl

from ..spec import JsonValue
from .types import PreviewSlice, SchemaInfo, TableStatistics


class TabularEngine(Protocol):
    """Minimum operation set that tabular engine must provide (internal)."""

    def records_to_dataframe(self, records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame: ...

    def dataframe_to_records(self, df: pl.DataFrame) -> list[dict[str, JsonValue]]: ...

    def infer_schema(self, df: pl.DataFrame) -> SchemaInfo: ...

    def compute_statistics(self, df: pl.DataFrame) -> TableStatistics: ...

    def generate_preview(self, df: pl.DataFrame, limit: int) -> PreviewSlice: ...


__all__ = ["TabularEngine"]

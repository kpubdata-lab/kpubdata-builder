"""Builder's tabular engine: DuckDB (ADR 0021).

``duckdb_runtime`` (connections, temp directories, resource limits, the version floor),
``sql`` (identifier quoting), ``dtypes`` (DuckDB types in Builder's dtype vocabulary),
``duckdb_load`` (records and Parquet files into tables), ``duckdb_casts``,
``duckdb_summary`` (schema, statistics, preview) and ``wire`` (how values cross to a
JSON client). Polars is no longer imported (#876).

The value objects every stage reports in are re-exported here.
"""

from __future__ import annotations

from .cast_names import CastReport
from .types import DEFAULT_PREVIEW_LIMIT, ColumnInfo, PreviewSlice, SchemaInfo, TableStatistics

__all__ = [
    "DEFAULT_PREVIEW_LIMIT",
    "CastReport",
    "ColumnInfo",
    "PreviewSlice",
    "SchemaInfo",
    "TableStatistics",
]

"""Polars-based tabular engine package.

Expose schema/statistics/preview generation (polars_engine), type casting helpers
(polars_helpers), and public value objects (types) in one place.

Principles:
    - Single Polars engine (no dual-engine)
    - Public root API doesn't re-expose Polars return types directly
    - records ↔ DataFrame conversion used only internally/submodules (convert)

The DuckDB migration (ADR 0021) adds its modules beside the Polars engine —
``duckdb_runtime`` (connections, temp directories, the version floor), ``sql``
(identifier quoting, bound parameters), ``dtypes`` (DuckDB types in Builder's dtype
vocabulary), ``duckdb_casts``, ``duckdb_load`` and ``duckdb_summary``. Silver runs on them
(#869); the stages still on Polars read Silver through ``polars_bridge``. None is
re-exported here.
"""

from __future__ import annotations

from .polars_engine import (
    DEFAULT_PREVIEW_LIMIT,
    compute_statistics,
    generate_preview,
    infer_schema,
)
from .polars_helpers import (
    CastReport,
    CastResult,
    cast_columns,
    validate_required_columns,
)
from .types import ColumnInfo, PreviewSlice, SchemaInfo, TableStatistics

__all__ = [
    "DEFAULT_PREVIEW_LIMIT",
    "CastReport",
    "CastResult",
    "ColumnInfo",
    "PreviewSlice",
    "SchemaInfo",
    "TableStatistics",
    "cast_columns",
    "compute_statistics",
    "generate_preview",
    "infer_schema",
    "validate_required_columns",
]

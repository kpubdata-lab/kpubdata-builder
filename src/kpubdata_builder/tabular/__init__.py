"""Polars-based tabular engine package.

Expose schema/statistics/preview generation (polars_engine), type casting helpers
(polars_helpers), and public value objects (types) in one place.

Principles:
    - Single Polars engine (no dual-engine)
    - Public root API doesn't re-expose Polars return types directly
    - records ↔ DataFrame conversion used only internally/submodules (convert)
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

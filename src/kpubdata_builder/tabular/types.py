"""Public value objects for tabular engine (#49).

Defines schema/statistics/preview representations consumed by Silver phase. To avoid
exposing Polars types in public surface, dtype is expressed as string only.

Key components:
    - ColumnInfo: Per-column schema info
    - SchemaInfo: Full-table schema summary
    - TableStatistics: Row/null/duplicate statistics
    - PreviewSlice: Top N rows preview slice
"""

from __future__ import annotations

from dataclasses import dataclass

from ..spec import JsonValue


@dataclass(frozen=True)
class ColumnInfo:
    """Inferred schema info for single column.

    Attributes:
        name: Column name.
        dtype: String representation of Polars dtype (e.g. "Int64", "String").
        nullable: Whether any null values exist in column.
        unique_count: Count of unique values (null included, per Polars n_unique).
    """

    name: str
    dtype: str
    nullable: bool
    unique_count: int


@dataclass(frozen=True)
class SchemaInfo:
    """Overall table schema summary.

    Attributes:
        columns: ColumnInfo tuple preserving column order.
    """

    columns: tuple[ColumnInfo, ...] = ()


@dataclass(frozen=True)
class TableStatistics:
    """Table-level statistics summary.

    Attributes:
        row_count: Total row count.
        null_counts: Null count per column.
        duplicate_rate: Duplicate row ratio (0.0 ~ 1.0). Empty table is 0.0.
    """

    row_count: int
    null_counts: dict[str, int]
    duplicate_rate: float


@dataclass(frozen=True)
class PreviewSlice:
    """Preview slice of top N rows.

    Attributes:
        rows: Preview rows (plain dict). Does not expose Polars types.
        total_rows: Total rows of original table.
    """

    rows: tuple[dict[str, JsonValue], ...]
    total_rows: int


__all__ = [
    "ColumnInfo",
    "PreviewSlice",
    "SchemaInfo",
    "TableStatistics",
]

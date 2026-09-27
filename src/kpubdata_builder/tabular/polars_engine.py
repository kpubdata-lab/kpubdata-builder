"""Polars-based tabular engine implementation (#49).

Derives schema info, table statistics, preview slice from DataFrame.
records ↔ DataFrame conversion reuses convert module to provide single entry point.

Key functions:
    - infer_schema: DataFrame → SchemaInfo
    - compute_statistics: DataFrame → TableStatistics
    - generate_preview: DataFrame → PreviewSlice
"""

from __future__ import annotations

import polars as pl

from .convert import dataframe_to_records, records_to_dataframe
from .types import ColumnInfo, PreviewSlice, SchemaInfo, TableStatistics

DEFAULT_PREVIEW_LIMIT = 5


def infer_schema(df: pl.DataFrame) -> SchemaInfo:
    """Infer column schema of DataFrame as SchemaInfo.

    nullable is determined by whether column has any null, unique_count is
    Per Polars n_unique (null counted as one unique value).

    Args:
        df: DataFrame to infer schema from.

    Returns:
        SchemaInfo: Schema summary preserving column order.
    """
    columns = tuple(
        ColumnInfo(
            name=name,
            dtype=str(dtype),
            nullable=df.get_column(name).null_count() > 0,
            unique_count=df.get_column(name).n_unique(),
        )
        for name, dtype in df.schema.items()
    )
    return SchemaInfo(columns=columns)


def compute_statistics(df: pl.DataFrame) -> TableStatistics:
    """Calculate row/null/duplicate statistics of DataFrame.

    duplicate_rate is (total rows - unique rows) / total rows; empty table is
    0.0.

    Args:
        df: DataFrame to calculate statistics for.

    Returns:
        TableStatistics: Row count, nulls per column, duplicate row ratio.
    """
    row_count = df.height
    null_counts = {name: df.get_column(name).null_count() for name in df.columns}
    duplicate_rate = 0.0 if row_count == 0 else 1.0 - (df.n_unique() / row_count)
    return TableStatistics(
        row_count=row_count,
        null_counts=null_counts,
        duplicate_rate=duplicate_rate,
    )


def generate_preview(df: pl.DataFrame, limit: int = DEFAULT_PREVIEW_LIMIT) -> PreviewSlice:
    """Generate preview slice of top N rows of DataFrame.

    Args:
        df: DataFrame to create preview from.
        limit: Maximum rows to include (default DEFAULT_PREVIEW_LIMIT).

    Returns:
        PreviewSlice: Top rows and total row count.

    Raises:
        ValueError: If limit is negative. Negative limit passed to df.head returns "last
            all except last row", creating unexpectedly large preview (#190).
    """
    if limit < 0:
        raise ValueError(f"preview limit must be >= 0, got {limit}")
    rows = tuple(dataframe_to_records(df.head(limit)))
    return PreviewSlice(rows=rows, total_rows=df.height)


__all__ = [
    "DEFAULT_PREVIEW_LIMIT",
    "compute_statistics",
    "dataframe_to_records",
    "generate_preview",
    "infer_schema",
    "records_to_dataframe",
]

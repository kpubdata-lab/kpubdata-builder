"""Silver summarization (#46)."""

from __future__ import annotations

import polars as pl

from ...tabular import SchemaInfo, TableStatistics, compute_statistics, infer_schema


def build_schema(table: pl.DataFrame) -> SchemaInfo:
    """generates table schema summary."""
    return infer_schema(table)


def build_statistics(table: pl.DataFrame) -> TableStatistics:
    """generates table statistics summary."""
    return compute_statistics(table)

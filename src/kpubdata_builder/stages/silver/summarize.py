"""Silver summarization (#46), on DuckDB (#869)."""

from __future__ import annotations

from ...tabular import SchemaInfo, TableStatistics
from ...tabular.duckdb_load import TableHandle


def build_schema(table: TableHandle) -> SchemaInfo:
    """generates table schema summary."""
    return table.schema()


def build_statistics(table: TableHandle) -> TableStatistics:
    """generates table statistics summary."""
    return table.statistics()

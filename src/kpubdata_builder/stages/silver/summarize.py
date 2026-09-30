"""Silver summarization (#46), on DuckDB (#869)."""

from __future__ import annotations

from ...tabular import SchemaInfo, TableStatistics
from ...tabular.duckdb_load import TableHandle
from ...tabular.duckdb_summary import schema_of, statistics_of


def build_schema(table: TableHandle) -> SchemaInfo:
    """generates table schema summary."""
    return schema_of(table.connection, table.table)


def build_statistics(table: TableHandle) -> TableStatistics:
    """generates table statistics summary."""
    return statistics_of(table.connection, table.table)

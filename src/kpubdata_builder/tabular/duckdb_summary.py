"""Schema, statistics and preview of a DuckDB table, as ``polars_engine`` gives them (#869).

The meanings are the ones Silver has always recorded:

- ``nullable`` — the column holds at least one null;
- ``unique_count`` — distinct values **counting null as one value** (Polars ``n_unique``);
- ``duplicate_rate`` — ``1 - distinct rows / rows``, 0.0 for an empty table;
- ``wire_encoding`` — decided per column from its dtype, and for an integer column from
  whether any value is beyond ±(2**53 - 1) (``wire.py``, #735);
- the preview is the first ``limit`` rows, in order, as plain values.
"""

from __future__ import annotations

from typing import cast

import duckdb

from ..spec import JsonValue
from .dtypes import logical_type
from .duckdb_load import LoadedTable, fetch_rows
from .sql import quote_identifier
from .types import ColumnInfo, PreviewSlice, SchemaInfo, TableStatistics
from .wire import JS_SAFE_INTEGER, WireEncoding

_INTEGER = ("Int8", "Int16", "Int32", "Int64", "Int128", "UInt8", "UInt16", "UInt32", "UInt64")
_TEXT_LIKE = ("String", "Date", "Datetime", "Time", "Duration", "Categorical", "Enum")


def _wire(dtype: str, low: object, high: object) -> WireEncoding:
    base = dtype.split("(", 1)[0]
    if base == "Decimal":
        return "decimal_string"
    if base in _INTEGER:
        if (isinstance(high, int) and high > JS_SAFE_INTEGER) or (
            isinstance(low, int) and low < -JS_SAFE_INTEGER
        ):
            return "decimal_string"
        return "number"
    if base in ("Float32", "Float64"):
        return "number"
    if base == "Boolean":
        return "boolean"
    if base in _TEXT_LIKE:
        return "string"
    return "json"


def schema_of(connection: duckdb.DuckDBPyConnection, table: LoadedTable) -> SchemaInfo:
    """Every column's dtype, nullability, distinct count and wire encoding."""
    if not table.physical:
        return SchemaInfo(columns=())
    parts: list[str] = []
    for physical, dtype in zip(table.physical, table.dtypes, strict=True):
        column = quote_identifier(physical)
        parts.append(f"count(*) - count({column})")
        parts.append(f"count(DISTINCT {column})")
        if dtype.split("(", 1)[0] in _INTEGER:
            parts.append(f"min({column})")
            parts.append(f"max({column})")
        else:
            parts.append("NULL")
            parts.append("NULL")
    row = connection.execute(f"SELECT {', '.join(parts)} FROM {table.relation.sql}").fetchone()
    assert row is not None
    columns = []
    for index, (name, dtype) in enumerate(zip(table.names, table.dtypes, strict=True)):
        nulls, distinct, low, high = row[index * 4 : index * 4 + 4]
        columns.append(
            ColumnInfo(
                name=name,
                dtype=dtype,
                nullable=nulls > 0,
                unique_count=int(distinct) + (1 if nulls > 0 else 0),
                logical_type=logical_type(dtype),
                wire_encoding=_wire(dtype, low, high),
            )
        )
    return SchemaInfo(columns=tuple(columns))


def statistics_of(connection: duckdb.DuckDBPyConnection, table: LoadedTable) -> TableStatistics:
    """Row count, nulls per column and the duplicate row rate."""
    source = table.relation.sql
    if not table.physical:
        return TableStatistics(row_count=table.row_count, null_counts={}, duplicate_rate=0.0)
    nulls = ", ".join(f"count(*) - count({quote_identifier(p)})" for p in table.physical)
    columns = ", ".join(quote_identifier(p) for p in table.physical)
    row = connection.execute(
        f"SELECT count(*), {nulls}, (SELECT count(*) FROM (SELECT DISTINCT {columns} "
        f"FROM {source})) FROM {source}"
    ).fetchone()
    assert row is not None
    row_count = int(row[0])
    distinct = int(row[-1])
    return TableStatistics(
        row_count=row_count,
        null_counts={name: int(value) for name, value in zip(table.names, row[1:-1], strict=True)},
        duplicate_rate=0.0 if row_count == 0 else 1.0 - (distinct / row_count),
    )


def preview_of(
    connection: duckdb.DuckDBPyConnection, table: LoadedTable, *, limit: int
) -> PreviewSlice:
    """The first ``limit`` rows and the table's row count.

    Raises:
        ValueError: ``limit`` is negative (#190).
    """
    if limit < 0:
        raise ValueError(f"preview limit must be >= 0, got {limit}")
    rows = cast(tuple[dict[str, JsonValue], ...], fetch_rows(connection, table, limit=limit))
    return PreviewSlice(rows=rows, total_rows=table.row_count)


__all__ = ["preview_of", "schema_of", "statistics_of"]

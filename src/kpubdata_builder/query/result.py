"""A DuckDB result as the wire sends it (#874, #735).

Columns get Builder's canonical dtype (``tabular.dtypes``, #866), never a DuckDB name;
their ``logical_type`` is that dtype without parameters and their ``wire_encoding`` is
decided as ``tabular.wire`` describes — an integer column becomes ``decimal_string``
when a returned value is beyond ±(2**53 - 1). An aggregate keeps the type DuckDB gives
it: a ``SUM`` of BIGINT is a 128-bit integer (``int128``), and the wire decides from the
values whether it travels as a number (#874).

Two things DuckDB does differently from the Polars engine before it: a column that is
Null in Builder comes out of any projection as INTEGER (``int32``, every value null),
and an instant (a zoned datetime) is sent in UTC — ``Datetime(..., time_zone='UTC')`` —
whatever zone the column was stored with.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import duckdb

from ..spec import JsonValue
from ..tabular.dtypes import canonical_dtype, logical_type
from ..tabular.sql import quote_identifier
from ..tabular.wire import JS_SAFE_INTEGER, WireEncoding, encode_value

_INTEGER = ("Int8", "Int16", "Int32", "Int64", "Int128", "UInt8", "UInt16", "UInt32", "UInt64")
_ZONED = "TIMESTAMP WITH TIME ZONE"
_TEXT_LIKE = ("String", "Date", "Datetime", "Time", "Duration", "Categorical", "Enum")


class ResultError(ValueError):
    """The result cannot be sent: a duplicate column name, or a type with no Builder name."""


def result_dtype(duckdb_type: str) -> str:
    """The Builder dtype of a result column.

    An INTERVAL is a ``Duration``: Builder's Duration columns are INTERVALs in the
    sandbox, and DuckDB hands an interval to Python as a duration (a month as 30 days).
    """
    if duckdb_type.strip() == "INTERVAL":
        return "Duration(time_unit='us')"
    return canonical_dtype(duckdb_type)


def _wire(dtype: str, values: Sequence[Any]) -> WireEncoding:
    base = dtype.split("(", 1)[0]
    if base == "Decimal":
        return "decimal_string"
    if base in _INTEGER:
        beyond = any(
            isinstance(v, int) and not isinstance(v, bool) and abs(v) > JS_SAFE_INTEGER
            for v in values
        )
        return "decimal_string" if beyond else "number"
    if base in ("Float32", "Float64"):
        return "number"
    if base == "Boolean":
        return "boolean"
    if base in _TEXT_LIKE:
        return "string"
    return "json"


def _zoned_columns(columns: Sequence[str], types: Sequence[str]) -> list[int]:
    zoned = [i for i, t in enumerate(types) if t == _ZONED]
    if any(i not in zoned and "TIME ZONE" in t for i, t in enumerate(types)):
        raise ResultError("a zoned datetime inside a list or struct cannot be sent")
    return zoned


def _readable(columns: Sequence[str], zoned: Sequence[int]) -> str:
    """A projection reading zoned datetimes as UTC wall time (no pytz, see ``to_wire``)."""
    return ", ".join(
        f"CAST({quote_identifier(name)} AS TIMESTAMP) AS {quote_identifier(name)}"
        if i in zoned
        else quote_identifier(name)
        for i, name in enumerate(columns)
    )


def _mark_utc(row: Sequence[Any], zoned: Sequence[int]) -> tuple[Any, ...]:
    return tuple(
        v.replace(tzinfo=dt.timezone.utc) if i in zoned and isinstance(v, dt.datetime) else v
        for i, v in enumerate(row)
    )


@dataclass(frozen=True)
class StreamedTable:
    """A result held in a table of the in-memory database (a cursor sees it; a TEMP table
    is its own connection's only), encoded and read a batch at a time (#874)."""

    connection: duckdb.DuckDBPyConnection
    table: str
    columns: list[str]
    dtypes: list[str]
    encodings: list[WireEncoding]
    row_count: int
    zoned: list[int]

    @property
    def column_meta(self) -> list[dict[str, JsonValue]]:
        return [
            {"name": name, "logical_type": logical_type(dtype), "wire_encoding": encoding}
            for name, dtype, encoding in zip(self.columns, self.dtypes, self.encodings, strict=True)
        ]

    def rows(
        self, *, batch_size: int = 10_000
    ) -> Iterator[tuple[tuple[Any, ...], dict[str, JsonValue]]]:
        """``(raw, encoded)`` for every row, in the result's order."""
        cursor = self.connection.cursor()
        try:
            # A table is read back in the order it was written (preserve_insertion_order).
            cursor.execute(f"SELECT {_readable(self.columns, self.zoned)} FROM {self.table}")
            while batch := cursor.fetchmany(batch_size):
                for row in batch:
                    raw = _mark_utc(row, self.zoned)
                    yield (
                        raw,
                        {
                            name: encode_value(value, encoding)
                            for name, value, encoding in zip(
                                self.columns, raw, self.encodings, strict=True
                            )
                        },
                    )
        finally:
            cursor.close()


def stream_result(connection: duckdb.DuckDBPyConnection, sql: str, *, limit: int) -> StreamedTable:
    """``sql``'s first ``limit`` rows, kept in a temporary table and described for the wire.

    The rows are not read into Python here: an integer column's wire encoding is decided
    from its minimum and maximum in SQL.

    Raises:
        ResultError: Two columns share a name, or a column's type has no Builder name.
    """
    relation = connection.sql(sql)
    columns = list(relation.columns)
    if len(set(columns)) != len(columns):
        raise ResultError("the result has two columns with one name")
    types = [str(t) for t in relation.types]
    try:
        dtypes = [result_dtype(t) for t in types]
    except ValueError as exc:
        raise ResultError(str(exc)) from exc
    zoned = _zoned_columns(columns, types)
    table = "_kpubdata_result"
    connection.execute(
        f"CREATE TABLE {table} AS SELECT * FROM ({sql}) AS _kpubdata_query LIMIT {int(limit)}"
    )
    counted = connection.execute(f"SELECT count(*) FROM {table}").fetchone()
    row_count = int(counted[0]) if counted else 0
    encodings: list[WireEncoding] = []
    for name, dtype in zip(columns, dtypes, strict=True):
        if dtype.split("(", 1)[0] in _INTEGER:
            column = quote_identifier(name)
            bounds = connection.execute(
                f"SELECT min({column}), max({column}) FROM {table}"
            ).fetchone()
            encodings.append(_wire(dtype, list(bounds or ())))
        else:
            encodings.append(_wire(dtype, []))
    return StreamedTable(connection, table, columns, dtypes, encodings, row_count, zoned)


@dataclass(frozen=True)
class WireResult:
    columns: list[str]
    column_meta: list[dict[str, JsonValue]]
    rows: list[dict[str, JsonValue]]
    #: The values as DuckDB gave them, for callers that look at them (PII scans).
    raw_rows: list[tuple[Any, ...]]

    def renamed(self, names: Sequence[str]) -> WireResult:
        """The same result with its columns called ``names``, in order — for a query
        over internal column aliases (``Sandbox.alias``)."""
        mapping = dict(zip(self.columns, names, strict=True))
        return WireResult(
            list(names),
            [{**meta, "name": mapping[str(meta["name"])]} for meta in self.column_meta],
            [{mapping[key]: value for key, value in row.items()} for row in self.rows],
            self.raw_rows,
        )

    def head(self, count: int) -> WireResult:
        return WireResult(self.columns, self.column_meta, self.rows[:count], self.raw_rows[:count])


def to_wire(
    relation: duckdb.DuckDBPyRelation,
    *,
    limit: int | None = None,
    stored: Sequence[str | None] | None = None,
) -> WireResult:
    """Fetch ``relation`` (at most ``limit`` rows) and encode it for the wire.

    ``stored`` gives, per column, the Builder dtype the file records when the column is a
    stored column read as it is (a page of rows): a Null column is then reported Null,
    which DuckDB's INTEGER cannot say.

    Raises:
        ResultError: Two columns share a name, or a column's type has no Builder name.
    """
    columns = list(relation.columns)
    if len(set(columns)) != len(columns):
        raise ResultError("the result has two columns with one name")
    try:
        dtypes = [result_dtype(str(t)) for t in relation.types]
    except ValueError as exc:
        raise ResultError(str(exc)) from exc
    if stored is not None:
        dtypes = [
            "Null" if recorded == "Null" else dtype
            for dtype, recorded in zip(dtypes, stored, strict=True)
        ]
    zoned = _zoned_columns(columns, [str(t) for t in relation.types])
    if zoned:
        # DuckDB hands an instant to Python only through pytz, which Builder does not
        # use: read it as UTC wall time (the connection's zone) and mark it UTC here.
        relation = relation.project(_readable(columns, zoned))
    raw = relation.fetchall() if limit is None else relation.limit(limit).fetchall()
    if zoned:
        raw = [_mark_utc(row, zoned) for row in raw]
    encodings = [_wire(dtype, [row[i] for row in raw]) for i, dtype in enumerate(dtypes)]
    meta: list[dict[str, JsonValue]] = [
        {"name": name, "logical_type": logical_type(dtype), "wire_encoding": encoding}
        for name, dtype, encoding in zip(columns, dtypes, encodings, strict=True)
    ]
    rows = [
        {
            name: encode_value(value, encoding)
            for name, value, encoding in zip(columns, row, encodings, strict=True)
        }
        for row in raw
    ]
    return WireResult(columns, meta, rows, raw)


__all__ = [
    "ResultError",
    "StreamedTable",
    "WireResult",
    "result_dtype",
    "stream_result",
    "to_wire",
]

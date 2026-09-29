"""How a column's values cross the wire to a JSON client (#735).

JSON numbers are read as IEEE 754 doubles. An integer beyond 2**53 - 1 or a Decimal sent
as a JSON number is already a different value by the time a browser holds it:

    Int64  9007199254740993  → 9007199254740992
    Decimal("0.1")           → 0.1000000000000000055511151231257827

So every column carries what it was (`logical_type`) and how it is sent
(`wire_encoding`), and its values are encoded to match:

    decimal_string  every Decimal column, and an integer column holding any value outside
                    ±(2**53 - 1). The value is its exact decimal text.
    number          the other integer and float columns. Non-finite floats become null,
                    since JSON has no NaN or Infinity.
    string          text, categorical and temporal columns. Dates and times are ISO 8601.
    boolean         boolean columns.
    json            anything else (lists, structs); nested values are made JSON-safe.

The decision is per column, not per value, so a client reads one field to know how to
treat every cell below it. An integer column switches to `decimal_string` as a whole when
one value would not survive, and stays `number` otherwise — in-range integers keep
arriving as numbers. That makes the encoding a property of one response, not of the
column (#794): another page or query over the same column can come back the other way,
so a client reads `wire_encoding` from every response.

Which columns are identifiers (postcodes, PNU, legal-dong codes) is a separate decision
(#702) and is not made here.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Literal, cast

import polars as pl

from ..spec import JsonValue
from .types import ColumnInfo

WireEncoding = Literal["number", "decimal_string", "string", "boolean", "json"]

JS_SAFE_INTEGER = 2**53 - 1
"""The largest integer a JavaScript number holds exactly (`Number.MAX_SAFE_INTEGER`)."""


def logical_type(dtype: pl.DataType) -> str:
    """The column's type without parameters: `int64`, `decimal`, `datetime`, `string`…"""
    return dtype.base_type().__name__.lower()


def wire_encoding(series: pl.Series) -> WireEncoding:
    """How this column's values are sent. Reads the values only for integer columns."""
    dtype = series.dtype
    if dtype.is_decimal():
        return "decimal_string"
    if dtype.is_integer():
        low, high = series.min(), series.max()
        if (isinstance(high, int) and high > JS_SAFE_INTEGER) or (
            isinstance(low, int) and low < -JS_SAFE_INTEGER
        ):
            return "decimal_string"
        return "number"
    if dtype.is_float():
        return "number"
    if dtype == pl.Boolean:
        return "boolean"
    if dtype.is_temporal() or dtype == pl.String or dtype == pl.Categorical or dtype == pl.Enum:
        return "string"
    return "json"


def encode_value(value: object, encoding: str) -> JsonValue:
    """Encode one cell for its column's wire encoding."""
    if value is None:
        return None
    if encoding == "decimal_string":
        if isinstance(value, Decimal):
            # `f` keeps the scale and never switches to exponent form: 12.50 stays "12.50".
            return format(value, "f")
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return _json_safe(value)


def _json_safe(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return cast(JsonValue, value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def encode_rows(
    rows: Iterable[Mapping[str, object]], columns: Sequence[ColumnInfo]
) -> tuple[dict[str, JsonValue], ...]:
    """Encode rows by their columns' wire encodings. Unknown keys are made JSON-safe."""
    encodings = {column.name: column.wire_encoding for column in columns}
    return tuple(
        {
            str(key): encode_value(value, encodings.get(str(key), "json"))
            for key, value in row.items()
        }
        for row in rows
    )


def column_meta(columns: Sequence[ColumnInfo]) -> list[dict[str, JsonValue]]:
    """The per-column fields a client needs to read the rows: name, logical type, encoding."""
    return [
        {"name": c.name, "logical_type": c.logical_type, "wire_encoding": c.wire_encoding}
        for c in columns
    ]


__all__ = [
    "JS_SAFE_INTEGER",
    "WireEncoding",
    "column_meta",
    "encode_rows",
    "encode_value",
    "logical_type",
    "wire_encoding",
]

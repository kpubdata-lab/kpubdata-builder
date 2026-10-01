"""A Polars column's ``logical_type`` and ``wire_encoding`` (#735), as Builder derived
them before #876 — the oracle for ``duckdb_summary`` in tests."""

from __future__ import annotations

import polars as pl

from kpubdata_builder.tabular.wire import JS_SAFE_INTEGER, WireEncoding


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

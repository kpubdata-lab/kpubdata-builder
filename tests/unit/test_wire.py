"""Values keep their precision on the wire (#735).

A JSON number is read as an IEEE 754 double. These tests pin which columns are sent as
exact decimal text instead, and that the values survive — including the negative case the
issue was filed for: without the encoding, 9007199254740993 arrives as ...992.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import polars as pl
import pytest

from kpubdata_builder.tabular.polars_engine import infer_schema
from kpubdata_builder.tabular.wire import (
    JS_SAFE_INTEGER,
    column_meta,
    encode_rows,
    encode_value,
    logical_type,
    wire_encoding,
)

FIRST_UNSAFE = 9007199254740993
INT64_MAX = 2**63 - 1
INT64_MIN = -(2**63)
UINT64_MAX = 2**64 - 1


def _as_double(value: int) -> int:
    """What a JavaScript client holds after JSON.parse of the number `value`."""
    return int(float(value))


def _roundtrip(frame: pl.DataFrame) -> tuple[list[dict[str, object]], dict[str, str]]:
    columns = infer_schema(frame).columns
    rows = json.loads(json.dumps(list(encode_rows(frame.to_dicts(), columns))))
    return rows, {c.name: c.wire_encoding for c in columns}


def test_a_double_cannot_hold_the_first_unsafe_integer() -> None:
    """The failure #735 describes, stated once so the rest has something to prevent."""
    assert _as_double(FIRST_UNSAFE) == FIRST_UNSAFE - 1
    assert _as_double(JS_SAFE_INTEGER) == JS_SAFE_INTEGER


@pytest.mark.parametrize("value", [FIRST_UNSAFE, INT64_MAX, INT64_MIN, -FIRST_UNSAFE])
def test_an_out_of_range_int64_column_is_sent_as_exact_text(value: int) -> None:
    rows, encodings = _roundtrip(pl.DataFrame({"n": [1, value]}, schema={"n": pl.Int64}))

    assert encodings["n"] == "decimal_string"
    # The whole column switches, so a client reads one field to treat every cell alike.
    assert rows == [{"n": "1"}, {"n": str(value)}]
    assert int(rows[1]["n"]) == value


def test_uint64_max_survives() -> None:
    rows, encodings = _roundtrip(pl.DataFrame({"n": [UINT64_MAX]}, schema={"n": pl.UInt64}))

    assert encodings["n"] == "decimal_string"
    assert rows == [{"n": "18446744073709551615"}]


def test_in_range_integers_stay_json_numbers() -> None:
    """What #735 must not break: the safe max itself still travels as a number."""
    rows, encodings = _roundtrip(
        pl.DataFrame({"n": [JS_SAFE_INTEGER, -JS_SAFE_INTEGER, 0]}, schema={"n": pl.Int64})
    )

    assert encodings["n"] == "number"
    assert rows == [{"n": JS_SAFE_INTEGER}, {"n": -JS_SAFE_INTEGER}, {"n": 0}]


def test_decimals_are_exact_text_keeping_their_scale() -> None:
    frame = pl.DataFrame(
        {"amount": [Decimal("0.1"), Decimal("12.50"), Decimal("-1000000.00")]},
        schema={"amount": pl.Decimal(precision=20, scale=2)},
    )
    rows, encodings = _roundtrip(frame)

    assert encodings["amount"] == "decimal_string"
    assert [row["amount"] for row in rows] == ["0.10", "12.50", "-1000000.00"]


def test_decimal_point_one_is_sent_as_its_text() -> None:
    assert encode_value(Decimal("0.1"), "decimal_string") == "0.1"


def test_null_stays_null_in_every_encoding() -> None:
    frame = pl.DataFrame(
        {"big": [FIRST_UNSAFE, None], "small": [1, None], "amount": [Decimal("1.5"), None]},
        schema={"big": pl.Int64, "small": pl.Int64, "amount": pl.Decimal(10, 1)},
    )
    rows, _ = _roundtrip(frame)

    assert rows[1] == {"big": None, "small": None, "amount": None}


def test_non_finite_floats_become_null() -> None:
    rows, encodings = _roundtrip(pl.DataFrame({"x": [1.5, float("nan"), float("inf")]}))

    assert encodings["x"] == "number"
    assert rows == [{"x": 1.5}, {"x": None}, {"x": None}]


def test_temporal_text_and_boolean_columns() -> None:
    frame = pl.DataFrame(
        {
            "day": [date(2026, 9, 29)],
            "at": [datetime(2026, 9, 29, 12, 30)],
            "name": ["01234"],
            "flag": [True],
        }
    )
    rows, encodings = _roundtrip(frame)

    assert encodings == {"day": "string", "at": "string", "name": "string", "flag": "boolean"}
    # A leading-zero code held as text is left alone — no numeric conversion happens here.
    assert rows == [
        {"day": "2026-09-29", "at": "2026-09-29T12:30:00", "name": "01234", "flag": True}
    ]


def test_logical_types_drop_dtype_parameters() -> None:
    assert logical_type(pl.Int64()) == "int64"
    assert logical_type(pl.Decimal(10, 2)) == "decimal"
    assert logical_type(pl.Datetime("us")) == "datetime"
    assert logical_type(pl.String()) == "string"


def test_column_meta_lists_name_type_and_encoding_in_order() -> None:
    frame = pl.DataFrame({"a": [FIRST_UNSAFE], "b": [1.0]}, schema={"a": pl.Int64, "b": pl.Float64})

    assert column_meta(infer_schema(frame).columns) == [
        {"name": "a", "logical_type": "int64", "wire_encoding": "decimal_string"},
        {"name": "b", "logical_type": "float64", "wire_encoding": "number"},
    ]


def test_an_empty_integer_column_is_a_number_column() -> None:
    assert wire_encoding(pl.Series("n", [], dtype=pl.Int64)) == "number"

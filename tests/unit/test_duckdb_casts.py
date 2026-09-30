"""DuckDB casts give what the Polars casts give, value by value (#868).

Every declared cast × every source dtype × a set of awkward inputs is run through both
``polars_helpers.cast_columns`` and ``duckdb_casts.cast_table``. Results are compared as
text in Polars' own spelling (``kpubdata_float_text`` is checked against Polars first),
so dates, datetimes, NaN and -0.0 are compared exactly.

The only allowed differences are :data:`DIVERGENCES`: inputs Polars turns into a value
other than the one written. There the DuckDB cast gives null, and the Silver audit
fails the build instead of storing a wrong value.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import struct
from collections.abc import Sequence
from typing import cast

import duckdb
import polars as pl
import pytest

from kpubdata_builder.tabular.dtypes import canonical_dtype
from kpubdata_builder.tabular.duckdb_casts import (
    cast_table,
    register_functions,
    text_expression,
    zfill_expression,
    zfill_violations,
)
from kpubdata_builder.tabular.polars_helpers import cast_columns
from kpubdata_builder.tabular.sql import quote_identifier

TARGETS = (
    "int",
    "int64",
    "float",
    "float64",
    "str",
    "string",
    "date",
    "datetime",
    "bool",
    "int_comma",
    "float_comma",
    "year_month",
)

STRINGS: list[str | None] = [
    None,
    "",
    " ",
    # integers
    "12",
    "-12",
    "+12",
    "007",
    "-0",
    " 12",
    "12 ",
    "12\n",
    "12.7",
    "1e3",
    "0x10",
    "1_000",
    "1,000",
    "1,234.5",
    " 1,000 ",
    "9223372036854775807",
    "9223372036854775808",
    "-9223372036854775808",
    "--1",
    "+-1",
    "١٢",
    # floats
    "1.",
    ".5",
    "+.5",
    "-.5e-2",
    "1.5E-3",
    "1e",
    "e3",
    "1e+",
    "1..2",
    ".",
    "-",
    "inf",
    "-inf",
    "+inf",
    "INF",
    "Infinity",
    "inFinity",
    "nan",
    "NaN",
    "-nan",
    "1.7976931348623157e309",
    "1e-400",
    "0x1p3",
    # dates
    "2024-01-01",
    "2024-1-1",
    "2024-01-1",
    " 2024-01-01",
    "2024-01-01 ",
    "\t2024-01-01",
    "+2024-01-01",
    "+ 2024-01-01",
    "2024/01/02",
    "20240103",
    "2024-02-30",
    "2024-02-29",
    "2023-02-29",
    "2024-13-01",
    "02024-01-01",
    "10000-01-01",
    "0001-01-01",
    "9999-12-31",
    "24-01-01",
    "1-01-01",
    "0000-01-01",
    "-0001-01-01",
    # datetimes
    "2024-01-01T12:30",
    "2024-01-01T12:30:00",
    "2024-01-01T12:30:00.5",
    "2024-01-01T12:30:00.123456789",
    "2024-01-01T1:2:3",
    "2024-1-1T12:30:00",
    " 2024-01-01T12:30:00",
    "2024-01-01T12:30:00Z",
    "2024-01-01T12:30:00z",
    "2024-01-01t12:30:00",
    "2024-01-01 12:30",
    "2024-01-01 12:30:00",
    "2024-01-01T24:00:00",
    "2024-01-01T12:60:00",
    "2024-01-01T12:30:60",
    "2024-01-01T12:30:00+09:00",
    "2024-01-01T12:30:00-05:00",
    "2024-01-01T12:30:00+0900",
    "2024-01-01T12:30:00+09",
    "2024-01-01T12:30:00UTC",
    # booleans and year_month
    "true",
    "True",
    " YES ",
    "y",
    "n",
    "f",
    "0",
    "1",
    "maybe",
    "　yes　",
    "2024-01",
    "202407",
    "2024-13",
    "20230",
    " 2024-07 ",
]

_RNG = random.Random(868)
FLOATS: list[float | None] = [
    None,
    0.0,
    -0.0,
    1.0,
    1.5,
    1.9,
    -1.9,
    2.5,
    -2.5,
    0.1,
    1e-5,
    1e-7,
    1e16,
    1e19,
    1e20,
    2.0**53,
    9007199254740993.0,
    math.nan,
    math.inf,
    -math.inf,
    5e-324,
] + [struct.unpack("d", struct.pack("Q", _RNG.getrandbits(64)))[0] for _ in range(200)]
INTS: list[int | None] = [
    None,
    0,
    1,
    -1,
    7,
    19000,
    2932896,
    -719162,
    2**31,
    2**53,
    2**53 + 1,
    -(2**53) - 1,
    2**62,
    -(2**63),
]
BOOLS: list[bool | None] = [None, True, False]
DATES: list[dt.date | None] = [None, dt.date(2024, 1, 2), dt.date(1, 1, 1), dt.date(9999, 12, 31)]
DATETIMES: list[dt.datetime | None] = [
    None,
    dt.datetime(2024, 1, 1, 12, 30),
    dt.datetime(2024, 1, 1, 12, 30, 0, 500000),
    dt.datetime(1970, 1, 1),
]

SOURCES: dict[str, tuple[pl.DataType, str, Sequence[object]]] = {
    "String": (pl.String(), "VARCHAR", STRINGS),
    "Int64": (pl.Int64(), "BIGINT", INTS),
    "Float64": (pl.Float64(), "DOUBLE", FLOATS),
    "Boolean": (pl.Boolean(), "BOOLEAN", BOOLS),
    "Date": (pl.Date(), "DATE", DATES),
    "Datetime": (pl.Datetime("us"), "TIMESTAMP", DATETIMES),
}

#: (source, target family, input): Polars returns a value that is not the one written,
#: so the DuckDB cast returns null and the audit fails the build.
_BAD_YEARS = ("24-01-01", "1-01-01", "0000-01-01", "-0001-01-01")
_ZONED = (
    "2024-01-01T12:30:00+09:00",
    "2024-01-01T12:30:00-05:00",
    "2024-01-01T12:30:00+0900",
    "2024-01-01T12:30:00+09",
    "2024-01-01T12:30:00UTC",
)
DIVERGENCES: set[tuple[str, str, object]] = {
    *(("String", "date", v) for v in _BAD_YEARS),
    *(("String", "datetime", v) for v in (*_ZONED, "2024-01-01T12:30:60")),
    *(("Int64", "float", v) for v in (2**53 + 1, -(2**53) - 1, 2**62, -(2**63))),
}
_FAMILY = {
    "int": "int",
    "int64": "int",
    "float": "float",
    "float64": "float",
    "date": "date",
    "datetime": "datetime",
}


def _beyond_python(value: object, target: str) -> bool:
    if value is None or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    unit = 1 if target == "date" else 86_400_000_000
    return not (-719162 * unit <= math.trunc(value) < 2932897 * unit)


def _polars(dtype: pl.DataType, values: Sequence[object], target: str) -> list[str | None] | None:
    frame = pl.DataFrame({"v": pl.Series("v", list(values), dtype=dtype)})
    try:
        result = cast_columns(frame, {"v": target}, audit=True)
    except Exception:  # noqa: BLE001 - a combination Polars does not support is skipped
        return None
    column = result.df.get_column("v")
    return cast(list[str | None], column.cast(pl.Utf8).to_list())


def _duckdb(duck_type: str, values: Sequence[object], target: str) -> tuple[list[str | None], str]:
    connection = duckdb.connect()
    register_functions(connection)
    connection.execute(f"CREATE TABLE t (i INTEGER, v {duck_type})")
    connection.executemany("INSERT INTO t VALUES (?, ?)", list(enumerate(values)))
    cast_table(connection, "t", {"v": target}, into="c")
    (dtype,) = [str(r[1]) for r in connection.execute("DESCRIBE c").fetchall() if r[0] == "v"]
    canonical = canonical_dtype(dtype)
    text = text_expression(quote_identifier("v"), canonical)
    rows = connection.execute(f"SELECT {text} FROM c ORDER BY i").fetchall()
    return [r[0] for r in rows], canonical


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("source", sorted(SOURCES))
def test_duckdb_cast_matches_polars(source: str, target: str) -> None:
    polars_dtype, duck_type, values = SOURCES[source]
    if target in ("date", "datetime") and source in ("Int64", "Float64"):
        # Polars renders, or panics on, dates beyond year 9999; those inputs are pinned
        # separately (test_out_of_range_dates_are_null).
        values = [v for v in values if not _beyond_python(v, target)]
    expected = _polars(polars_dtype, values, target)
    if expected is None:
        pytest.skip(f"Polars does not cast {source} to {target}")

    actual, canonical = _duckdb(duck_type, values, target)

    polars_frame = pl.DataFrame({"v": pl.Series("v", list(values), dtype=polars_dtype)})
    expected_dtype = str(cast_columns(polars_frame, {"v": target}).schema["v"])
    assert canonical == expected_dtype
    family = _FAMILY.get(target.lower(), target.lower())
    mismatches = [
        (value, want, got)
        for value, want, got in zip(values, expected, actual, strict=True)
        if want != got and (source, family, value) not in DIVERGENCES
    ]
    assert not mismatches, mismatches[:10]
    for value, want, got in zip(values, expected, actual, strict=True):
        if (source, family, value) in DIVERGENCES:
            assert got is None and want is not None, (value, want, got)


def test_float_to_integer_truncates_as_stated() -> None:
    """R6: DuckDB's CAST rounds 1.9 to 2; the Builder cast truncates, as Polars does."""
    values, _ = _duckdb("DOUBLE", [1.9, -1.9, 2.5, -2.5], "int")

    assert values == ["1", "-1", "2", "-2"]


def test_a_cast_that_nulls_values_is_reported() -> None:
    """R4/R5: no silent cast-to-null — the audit sees every null a cast introduced."""
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('1'), (' 2'), ('x'), (NULL)) v(v)")

    outcome = cast_table(connection, "t", {"v": "int"}, into="c")

    assert outcome.has_nulls_introduced
    (report,) = outcome.reports
    assert (report.nulls_before, report.nulls_after) == (1, 3)


def test_an_unknown_dtype_and_a_missing_column_are_refused() -> None:
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t AS SELECT 1 AS v")

    with pytest.raises(ValueError, match="Unsupported dtype"):
        cast_table(connection, "t", {"v": "decimal"}, into="c")
    with pytest.raises(ValueError, match="Cannot cast missing column"):
        cast_table(connection, "t", {"w": "int"}, into="c")


def test_float_text_matches_polars_on_random_doubles() -> None:
    rng = random.Random(1)
    values = [struct.unpack("d", struct.pack("Q", rng.getrandbits(64)))[0] for _ in range(5000)]
    values += [rng.uniform(-1, 1) * 10 ** rng.randint(-25, 25) for _ in range(5000)]

    actual, _ = _duckdb("DOUBLE", values, "str")

    assert actual == pl.Series(values, dtype=pl.Float64).cast(pl.Utf8).to_list()


@pytest.mark.parametrize("target", ["date", "datetime"])
def test_out_of_range_dates_are_null(target: str) -> None:
    unit = 1 if target == "date" else 86_400_000_000
    values = [2932896 * unit, 2932897 * unit, -719162 * unit, -719163 * unit, 2**62, -(2**63)]

    actual, _ = _duckdb("BIGINT", values, target)

    assert [v is not None for v in actual] == [True, False, True, False, False, False]


# ------------------------------------------------------------------ zfill (R13)


def test_zfill_matches_polars() -> None:
    values = ["12", "-12", "+12", "", "abc", "가나", None, "1-2", "--1", "-", "+", "12345"]
    connection = duckdb.connect()
    connection.execute("CREATE TABLE t (i INTEGER, v VARCHAR)")
    connection.executemany("INSERT INTO t VALUES (?, ?)", list(enumerate(values)))

    rows = connection.execute(
        f"SELECT {zfill_expression(quote_identifier('v'), 5)} FROM t ORDER BY i"
    ).fetchall()

    assert [r[0] for r in rows] == pl.Series(values, dtype=pl.Utf8).str.zfill(5).to_list()


def test_zfill_never_truncates() -> None:
    """R13: a value longer than the width is a failure, counted in characters."""
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES ('12'), ('12345'), ('가나다라마바')) v(v)"
    )

    assert zfill_violations(connection, "t", "v", 3) == (2, 6)
    assert zfill_violations(connection, "t", "v", 6) == (0, 0)

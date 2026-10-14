"""Filter literals are cast with one bounded DuckDB connection per request (#1341).

``check_plan`` and ``check_aggregate_plan`` cast every filter value to its column's type
before the query worker starts. They used to open a fresh, unconfigured in-memory
connection for each value, so a filter ``in`` with 100 values opened 100 connections,
each with DuckDB's default memory and thread count. These tests count the connections
instead of timing them, and check that the casts and their error messages are the ones
the per-value connection gave.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import duckdb
import pytest

from kpubdata_builder.query.aggregate import AggregatePlan, Measure, check_aggregate_plan
from kpubdata_builder.query.rows import (
    MAX_FILTERS,
    MAX_IN_VALUES,
    RowFilter,
    RowsPlan,
    check_plan,
    typed_literal,
)
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.dtypes import scalar_sql_type

_REAL_CONNECT = duckdb.connect


class _Recorded:
    """A real connection that, when closed, records the settings it ran with."""

    def __init__(self, connection: duckdb.DuckDBPyConnection, opened: list[dict[str, Any]]):
        self._connection = connection
        self._opened = opened

    def execute(self, *args: Any) -> duckdb.DuckDBPyConnection:
        return self._connection.execute(*args)

    def __enter__(self) -> _Recorded:
        return self

    def __exit__(self, *exc: object) -> None:
        names = ("threads", "memory_limit", "temp_directory", "enable_external_access")
        names += ("autoload_known_extensions", "autoinstall_known_extensions")
        names += ("lock_configuration", "TimeZone")
        row = self._connection.execute(
            "SELECT " + ", ".join(f"current_setting('{n}')" for n in names)
        ).fetchone()
        assert row is not None
        self._opened[-1].update(zip(names, row, strict=True))
        self._connection.close()


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Each DuckDB connection opened while the test runs, by the settings it ran with."""
    opened: list[dict[str, Any]] = []

    def counting(*args: Any, **kwargs: Any) -> _Recorded:
        opened.append({})
        return _Recorded(_REAL_CONNECT(*args, **kwargs), opened)

    monkeypatch.setattr(duckdb, "connect", counting)
    yield opened


def _reference(value: JsonValue, dtype: str) -> object:
    """The cast as it was before #1341: one fresh connection for the one value."""
    if isinstance(value, (dict, list)):
        raise ValueError("a filter value must be a scalar")
    storage = scalar_sql_type(dtype)
    zoned = storage == "TIMESTAMP WITH TIME ZONE"
    expression = f"timezone('UTC', CAST(? AS {storage}))" if zoned else f"CAST(? AS {storage})"
    with _REAL_CONNECT(":memory:") as connection:
        connection.execute("SET TimeZone = 'UTC'")
        try:
            row = connection.execute(f"SELECT {expression}", [value]).fetchone()
        except duckdb.Error as exc:
            raise ValueError(f"{value!r} is not a valid {dtype}") from exc
    if row is None or row[0] is None:
        raise ValueError(f"{value!r} is not a valid {dtype}")
    if zoned and isinstance(row[0], dt.datetime):
        return row[0].replace(tzinfo=dt.timezone.utc)
    return row[0]


_ZONED = "Datetime(time_unit='us', time_zone='Asia/Seoul')"
_NAIVE = "Datetime(time_unit='us', time_zone=None)"
_DECIMAL = "Decimal(precision=10, scale=2)"

#: (value, dtype, expected) — expected is the value, or the ValueError message.
CASES: list[tuple[JsonValue, str, object]] = [
    ("12", "Int64", 12),
    (12, "Int64", 12),
    ("170141183460469231731687303715884105727", "Int128", 2**127 - 1),
    (300, "Int8", ValueError("300 is not a valid Int8")),
    ("abc", "Int64", ValueError("'abc' is not a valid Int64")),
    ("1.5", "Float64", 1.5),
    (2, "Float64", 2.0),
    ("nope", "Float64", ValueError("'nope' is not a valid Float64")),
    ("1.50", _DECIMAL, Decimal("1.50")),
    ("1.005", _DECIMAL, Decimal("1.01")),
    ("123456789012", _DECIMAL, ValueError(f"'123456789012' is not a valid {_DECIMAL}")),
    ("2020-01-02", "Date", dt.date(2020, 1, 2)),
    ("2020-02-30", "Date", ValueError("'2020-02-30' is not a valid Date")),
    ("2024-01-01T09:00:00", _NAIVE, dt.datetime(2024, 1, 1, 9)),
    (
        "2024-01-01T09:00:00+09:00",
        _ZONED,
        dt.datetime(2024, 1, 1, 0, tzinfo=dt.timezone.utc),
    ),
    ("not a time", _ZONED, ValueError(f"'not a time' is not a valid {_ZONED}")),
    ("true", "Boolean", True),
    (False, "Boolean", False),
    ("maybe", "Boolean", ValueError("'maybe' is not a valid Boolean")),
    ("0012", "String", "0012"),
    (12, "String", "12"),
    ("x", "Null", ValueError("no value can be compared with a Null column")),
    ([1], "Int64", ValueError("a filter value must be a scalar")),
    ({"a": 1}, "String", ValueError("a filter value must be a scalar")),
    ("1", "List(Int64)", ValueError("no value can be compared with a List(Int64) column")),
]


def _outcome(value: JsonValue, dtype: str, cast: Any) -> object:
    try:
        return cast(value, dtype)
    except ValueError as exc:
        return ValueError(str(exc))


def _same(left: object, right: object) -> bool:
    if isinstance(left, ValueError) or isinstance(right, ValueError):
        return type(left) is type(right) and str(left) == str(right)
    return left == right and type(left) is type(right)


@pytest.mark.parametrize(("value", "dtype", "expected"), CASES)
def test_cast_and_message_match_the_per_value_connection(
    value: JsonValue, dtype: str, expected: object
) -> None:
    reference = _outcome(value, dtype, _reference)
    actual = _outcome(value, dtype, typed_literal)
    assert _same(reference, expected), reference
    assert _same(actual, reference), actual


def _plan_with_in(values: tuple[JsonValue, ...]) -> RowsPlan:
    return RowsPlan(offset=0, page_size=10, filters=(RowFilter("n", "in", values=values),))


def test_an_in_filter_opens_one_connection_not_one_per_value(
    connects: list[dict[str, Any]],
) -> None:
    check_plan(_plan_with_in(tuple(str(i) for i in range(MAX_IN_VALUES))), {"n": "Int64"})

    assert len(connects) == 1


def test_the_largest_plan_opens_one_connection(connects: list[dict[str, Any]]) -> None:
    filters = tuple(
        RowFilter(f"c{i}", "in", values=tuple(str(v) for v in range(MAX_IN_VALUES)))
        for i in range(MAX_FILTERS)
    )
    schema = {f"c{i}": "Int64" for i in range(MAX_FILTERS)}

    check_plan(RowsPlan(offset=0, page_size=10, filters=filters), schema)

    assert len(connects) == 1


def test_an_aggregate_plan_opens_one_connection(connects: list[dict[str, Any]]) -> None:
    plan = AggregatePlan(
        group_by=("g",),
        measures=(Measure("count_rows", "rows"),),
        filters=(
            RowFilter("n", "in", values=tuple(str(i) for i in range(50))),
            RowFilter("d", "gte", "2020-01-01"),
        ),
    )

    check_aggregate_plan(plan, {"g": "String", "n": "Int64", "d": "Date"})

    assert len(connects) == 1


def test_a_plan_without_values_opens_no_connection(connects: list[dict[str, Any]]) -> None:
    check_plan(
        RowsPlan(offset=0, page_size=10, filters=(RowFilter("n", "is_null"),)), {"n": "Int64"}
    )

    assert connects == []


def test_the_cast_connection_is_bounded_and_shut_off(connects: list[dict[str, Any]]) -> None:
    check_plan(_plan_with_in(("1", "2")), {"n": "Int64"})

    (settings,) = connects
    assert settings["threads"] == 1
    amount, unit = str(settings["memory_limit"]).split()
    assert unit == "MiB" and float(amount) <= 64
    assert settings["temp_directory"] == ""
    assert settings["enable_external_access"] is False
    assert settings["autoload_known_extensions"] is False
    assert settings["autoinstall_known_extensions"] is False
    assert settings["lock_configuration"] is True
    assert settings["TimeZone"] == "UTC"


def test_the_first_value_that_does_not_fit_is_the_one_reported() -> None:
    plan = RowsPlan(
        offset=0,
        page_size=10,
        filters=(
            RowFilter("n", "in", values=("1", "x", "y")),
            RowFilter("d", "eq", "never"),
        ),
    )

    with pytest.raises(ValueError, match=r"^'x' is not a valid Int64$"):
        check_plan(plan, {"n": "Int64", "d": "Date"})


def test_a_value_after_a_refused_one_still_casts_on_a_fresh_request() -> None:
    with pytest.raises(ValueError):
        check_plan(_plan_with_in(("x",)), {"n": "Int64"})

    check_plan(_plan_with_in(("1",)), {"n": "Int64"})

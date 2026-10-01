"""Polars tabular helper: conversion, validation, casting behavior."""

from __future__ import annotations

import polars as pl
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular import CastReport
from tests.support.polars_convert import records_to_dataframe
from tests.support.polars_helpers import CastResult, cast_columns, validate_required_columns


def test_records_to_dataframe_rejects_heterogeneous_column() -> None:
    # String+number mixed in one column → don't silently coerce, fail explicitly (#187).
    records: list[dict[str, JsonValue]] = [{"v": 1}, {"v": "two"}]

    with pytest.raises(TabularError, match="heterogeneous column types"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_numeric_mix_and_nulls() -> None:
    # int/float mix and nulls are compatible; allow (#187 avoid false positives).
    records: list[dict[str, JsonValue]] = [{"v": 1}, {"v": 2.5}, {"v": None}]

    df = records_to_dataframe(records)

    assert df.height == 3


def test_records_to_dataframe_infers_float_beyond_default_window() -> None:
    # Default inference window (front rows) exceeded; full scan finds float appearing first
    # → inferred Float64, 2.5 preserved (#216).
    records: list[dict[str, JsonValue]] = [{"b": 1}] * 150 + [{"b": 2.5}]

    df = records_to_dataframe(records)

    assert df.schema["b"] == pl.Float64
    assert df["b"].to_list()[-1] == 2.5


def test_records_to_dataframe_rejects_large_int_mixed_with_float() -> None:
    # Integer > 2^53 mixed with float → upcast to f64 rounds, so reject (#198).
    records: list[dict[str, JsonValue]] = [{"v": 9007199254740993}, {"v": 2.5}]

    with pytest.raises(TabularError, match="precision loss"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_large_int_only_column() -> None:
    # Float absent; pure integer column preserves exactly, so allow (#198).
    big = 9007199254740993
    records: list[dict[str, JsonValue]] = [{"v": big}, {"v": 1}]

    df = records_to_dataframe(records)

    assert df["v"].to_list() == [big, 1]


def test_records_to_dataframe_rejects_large_int_mixed_with_float_in_nested_list() -> None:
    # Large int+float mix in nested list → f64 upcast rounds, reject (#198).
    records: list[dict[str, JsonValue]] = [{"v": [9007199254740993]}, {"v": [2.5]}]

    with pytest.raises(TabularError, match="precision loss"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_rejects_large_int_mixed_with_float_in_nested_struct() -> None:
    # Same field in nested struct with large int+float → reject (#198).
    records: list[dict[str, JsonValue]] = [
        {"v": {"x": 9007199254740993}},
        {"v": {"x": 2.5}},
    ]

    with pytest.raises(TabularError, match="precision loss"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_large_int_and_float_in_separate_struct_fields() -> None:
    # Separate struct fields are separate columns → large int and float safe together (#198).
    big = 9007199254740993
    records: list[dict[str, JsonValue]] = [{"v": {"i": big, "f": 2.5}}]

    df = records_to_dataframe(records)

    assert df.height == 1


def test_records_to_dataframe_detects_nested_list_heterogeneity() -> None:
    # list[int] vs list[str] aren't same "list" category → element type matters, reject (#199).
    records: list[dict[str, JsonValue]] = [{"v": [1]}, {"v": ["x"]}]

    with pytest.raises(TabularError, match="heterogeneous column types"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_detects_nested_struct_heterogeneity() -> None:
    # struct{x:int} vs struct{x:str} → inspect field types and reject (#199).
    records: list[dict[str, JsonValue]] = [{"v": {"x": 1}}, {"v": {"x": "s"}}]

    with pytest.raises(TabularError, match="heterogeneous column types"):
        _ = records_to_dataframe(records)


def test_records_to_dataframe_allows_optional_nested_field() -> None:
    # Nested struct optional field (one side null/absent) → don't block false positive (#199).
    records: list[dict[str, JsonValue]] = [
        {"v": {"x": 1, "y": "a"}},
        {"v": {"x": 2}},
        {"v": {"x": 3, "y": None}},
    ]

    df = records_to_dataframe(records)

    assert df.height == 3


def test_records_to_dataframe_converts_raw_records_without_mutating_input() -> None:
    # Verify conversion to DataFrame doesn't mutate input records.
    records: list[dict[str, JsonValue]] = [
        {"id": "1", "amount": "1000", "district": "강남구"},
        {"id": "2", "amount": "2500", "district": "서초구"},
    ]

    df = records_to_dataframe(records)

    assert df.shape == (2, 3)
    assert df.to_dicts() == records
    assert records[0]["amount"] == "1000"


def test_records_to_dataframe_accepts_empty_records() -> None:
    # Verify empty input handled as empty DataFrame without exception.
    df = records_to_dataframe(())

    assert df.shape == (0, 0)


def test_validate_required_columns_returns_dataframe_when_columns_exist() -> None:
    # Required columns present → return original DataFrame unchanged.
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    result = validate_required_columns(df, ("id", "amount"))

    assert result is df


def test_validate_required_columns_raises_for_missing_columns() -> None:
    # ValueError raised with missing column list.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Missing required columns: amount, district"):
        _ = validate_required_columns(df, ("id", "amount", "district"))


def test_cast_columns_casts_named_dtypes_without_changing_original_dataframe() -> None:
    # Verify string dtype aliases cast to correct Polars types.
    df = records_to_dataframe(
        (
            {"id": "1", "amount": "1000", "ratio": "1.5", "active": "true"},
            {"id": "2", "amount": "bad", "ratio": "", "active": "false"},
        )
    )

    casted = cast_columns(
        df,
        {
            "id": "str",
            "amount": "int",
            "ratio": "float",
            "active": "bool",
        },
    )

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["id"] == pl.Utf8
    assert casted.schema["amount"] == pl.Int64
    assert casted.schema["ratio"] == pl.Float64
    assert casted.schema["active"] == pl.Boolean
    assert casted.to_dicts() == [
        {"id": "1", "amount": 1000, "ratio": 1.5, "active": True},
        {"id": "2", "amount": None, "ratio": None, "active": False},
    ]
    assert df.schema["amount"] == pl.Utf8


def test_cast_columns_accepts_polars_dtypes() -> None:
    # Polars dtype classes themselves accepted as input.
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    casted = cast_columns(df, {"amount": pl.Int64})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["amount"] == pl.Int64
    assert casted.to_dicts() == [{"id": "1", "amount": 1000}]


def test_cast_columns_accepts_polars_dtype_instance() -> None:
    """pl.Int64() (instantiated DataType) should work."""
    # DataType instances handled correctly.
    df = records_to_dataframe(({"id": "1", "amount": "1000"},))

    casted = cast_columns(df, {"amount": pl.Int64()})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["amount"] == pl.Int64


def test_cast_columns_accepts_parameterized_dtype_instance() -> None:
    """Parameterized dtype instances like pl.Datetime('ms') should work."""
    # Parameterized DataType instances preserved.
    df = records_to_dataframe(({"ts": "2025-01-01 00:00:00"},))

    casted = cast_columns(df, {"ts": pl.Datetime("ms")})

    assert isinstance(casted, pl.DataFrame)
    assert casted.schema["ts"] == pl.Datetime("ms")


def test_cast_columns_raises_for_missing_column() -> None:
    # Non-existent column cast request fails immediately.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Cannot cast missing column"):
        _ = cast_columns(df, {"amount": "int"})


def test_cast_columns_raises_for_unknown_dtype() -> None:
    # Unsupported dtype name explicitly rejected.
    df = records_to_dataframe(({"id": "1"},))

    with pytest.raises(ValueError, match="Unsupported dtype"):
        _ = cast_columns(df, {"id": "money"})


def test_cast_columns_audit_returns_cast_result() -> None:
    # audit=True returns CastResult wrapper.
    df = records_to_dataframe(
        (
            {"amount": "1000"},
            {"amount": "2000"},
        )
    )

    result = cast_columns(df, {"amount": "int"}, audit=True)

    assert isinstance(result, CastResult)
    assert result.df.schema["amount"] == pl.Int64
    assert result.has_nulls_introduced is False
    assert result.reports == ()


def test_cast_columns_audit_detects_data_loss() -> None:
    # Cast failure increasing nulls recorded in report.
    df = records_to_dataframe(
        (
            {"amount": "1000"},
            {"amount": "bad"},
            {"amount": "3000"},
        )
    )

    result = cast_columns(df, {"amount": "int"}, audit=True)

    assert isinstance(result, CastResult)
    assert result.has_nulls_introduced is True
    assert len(result.reports) == 1
    report = result.reports[0]
    assert report.column == "amount"
    assert report.nulls_before == 0
    assert report.nulls_after == 1
    assert report.nulls_introduced == 1


def test_cast_columns_audit_empty_dtypes() -> None:
    # No cast targets → original DataFrame and empty report.
    df = records_to_dataframe(({"id": "1"},))

    result = cast_columns(df, {}, audit=True)

    assert isinstance(result, CastResult)
    assert result.df is df
    assert result.reports == ()


def test_cast_report_nulls_introduced() -> None:
    # null_count_increase property returns simple difference.
    report = CastReport(column="x", nulls_before=1, nulls_after=3)
    assert report.nulls_introduced == 2


class TestOutOfRangeValues:
    """#974: values Polars cannot hold raise TabularError, not raw Polars exceptions."""

    def test_int_beyond_128_bits(self) -> None:
        records: list[dict[str, JsonValue]] = [{"v": 2**130}]  # type: ignore[dict-item]
        with pytest.raises(TabularError, match="column 'v'.*supported range"):
            _ = records_to_dataframe(records)

    def test_negative_int_beyond_128_bits(self) -> None:
        records: list[dict[str, JsonValue]] = [{"v": -(2**130)}]  # type: ignore[dict-item]
        with pytest.raises(TabularError, match="column 'v'.*supported range"):
            _ = records_to_dataframe(records)

    def test_decimal_beyond_38_digits(self) -> None:
        from decimal import Decimal

        records: list[dict[str, JsonValue]] = [{"v": Decimal("1" + "0" * 38)}]  # type: ignore[dict-item]
        with pytest.raises(TabularError, match="column 'v'.*significant digits"):
            _ = records_to_dataframe(records)

    def test_out_of_range_inside_a_nested_list(self) -> None:
        records: list[dict[str, JsonValue]] = [{"v": [1, 2**130]}]  # type: ignore[dict-item]
        with pytest.raises(TabularError, match="supported range"):
            _ = records_to_dataframe(records)

    def test_int128_boundary_values_load(self) -> None:
        boundary = 2**127 - 1
        records: list[dict[str, JsonValue]] = [{"v": boundary}, {"v": -boundary - 1}]
        df = records_to_dataframe(records)
        assert len(df) == 2

"""Quality rules on DuckDB (#872): edge cases, comparability, no raw engine errors."""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from kpubdata_builder.quality import evaluate_quality
from kpubdata_builder.quality.models import QualityCheckResult
from kpubdata_builder.spec.models import CompareColumnsRule, QualityPolicy, RangeRule
from kpubdata_builder.stages.silver.models import SilverDataset, ValidationResult
from kpubdata_builder.stages.silver.preview import build_preview
from kpubdata_builder.stages.silver.summarize import build_schema, build_statistics
from kpubdata_builder.tabular.duckdb_load import TableHandle
from kpubdata_builder.tabular.polars_bridge import handle_from_frame

_EVALUATOR = Path(__file__).parents[2] / "src" / "kpubdata_builder" / "quality" / "evaluator.py"


def _silver(frame: pl.DataFrame, tmp_path: Path) -> SilverDataset:
    table = handle_from_frame(frame, workdir=tmp_path)
    return SilverDataset(
        table=table,
        schema=build_schema(table),
        statistics=build_statistics(table),
        preview=build_preview(table, limit=5),
        validation=ValidationResult(ok=True),
        source_bronze="x",
    )


def _one(results: tuple[QualityCheckResult, ...], rule: str) -> QualityCheckResult:
    (found,) = [r for r in results if r.rule == rule]
    return found


def _range(frame: pl.DataFrame, tmp_path: Path, **bounds: Any) -> QualityCheckResult | None:
    policy = QualityPolicy(range=(RangeRule(column="v", **bounds),))
    found = [r for r in evaluate_quality(_silver(frame, tmp_path), policy, source_key="s")]
    return found[0] if found else None


# ------------------------------------------------------------------------ numeric edges


def test_nan_and_infinity_are_out_of_a_finite_range(tmp_path: Path) -> None:
    """NaN sorts above every number (as it did under Polars), so it breaks a max."""
    frame = pl.DataFrame({"v": [1.0, float("nan"), float("inf"), float("-inf"), None]})

    result = _range(frame, tmp_path, min=0.0, max=10.0)

    assert result is not None
    assert (result.affected_rows, result.evaluated_rows) == (3, 4)
    only_min = _range(frame, tmp_path, min=0.0)
    assert only_min is not None and only_min.affected_rows == 1  # -inf


def test_decimal_columns_compare_exactly(tmp_path: Path) -> None:
    frame = pl.DataFrame(
        {
            "v": pl.Series(
                [Decimal("1.10"), Decimal("1.11"), Decimal("2.00")], dtype=pl.Decimal(38, 2)
            )
        }
    )

    result = _range(frame, tmp_path, min=1.1, max=2.0)

    assert result is not None and (result.affected_rows, result.evaluated_rows) == (0, 3)


def test_integers_beyond_javascript_safety_compare_exactly(tmp_path: Path) -> None:
    big = 2**53 + 1
    frame = pl.DataFrame({"v": [big, big + 1, 2**70]}, schema={"v": pl.Int128})

    result = _range(frame, tmp_path, max=float(2**60))

    assert result is not None and (result.affected_rows, result.evaluated_rows) == (1, 3)


def test_an_all_null_column_is_not_evaluated(tmp_path: Path) -> None:
    frame = pl.DataFrame({"v": pl.Series([None, None], dtype=pl.Null), "w": [1, 2]})
    policy = QualityPolicy(
        range=(RangeRule(column="v", min=0.0),),
        compare_columns=(CompareColumnsRule(left="v", right="w", operator="lt"),),
        max_null_ratio={"v": 0.5},
    )

    results = evaluate_quality(_silver(frame, tmp_path), policy, source_key="s")

    # Nothing to compare is not a pass: only the null ratio is evaluated.
    assert [r.rule for r in results] == ["max_null_ratio"]
    assert _one(results, "max_null_ratio").status == "warn"


def test_an_empty_table_evaluates_what_it_can(tmp_path: Path) -> None:
    frame = pl.DataFrame({"v": pl.Series([], dtype=pl.Int64)})
    policy = QualityPolicy(
        min_rows=1,
        range=(RangeRule(column="v", min=0.0),),
        max_null_ratio={"v": 0.1},
    )

    results = evaluate_quality(_silver(frame, tmp_path), policy, source_key="s")

    assert [(r.rule, r.status) for r in results] == [("min_rows", "warn")]


# ------------------------------------------------------------------------ comparability


@pytest.mark.parametrize(
    "values",
    [["a", "b"], [True, False], [dt.date(2020, 1, 1)] * 2, [[1], [2]]],
    ids=["text", "boolean", "date", "list"],
)
def test_a_range_on_a_non_numeric_column_is_not_comparable(
    values: list[object], tmp_path: Path
) -> None:
    """A numeric rule: Polars compared a boolean or date column with numbers silently."""
    result = _range(pl.DataFrame({"v": values}), tmp_path, min=1.5, max=2.5)

    assert result is not None
    assert result.status == "warn" and result.detail is not None
    assert "cannot be compared with numeric range" in result.detail


@pytest.mark.parametrize(
    ("left", "right", "comparable"),
    [
        (pl.Series([1, 2]), pl.Series([1.5, 2.5]), True),
        (pl.Series([1, 2]), pl.Series([Decimal("1"), Decimal("3")], dtype=pl.Decimal(38, 0)), True),
        (pl.Series(["a", "b"]), pl.Series(["a", "c"]), True),
        (pl.Series([dt.date(2020, 1, 1)] * 2), pl.Series([dt.date(2021, 1, 1)] * 2), True),
        (pl.Series([1, 2]), pl.Series(["1", "2"]), False),
        (pl.Series([1, 2]), pl.Series([dt.date(2020, 1, 1)] * 2), False),
        (
            pl.Series([Decimal("1"), Decimal("2")], dtype=pl.Decimal(38, 0)),
            pl.Series(["1", "2"]),
            False,
        ),
        (pl.Series([True, False]), pl.Series([1, 0]), False),
    ],
)
def test_compare_columns_needs_numbers_or_one_dtype(
    left: pl.Series, right: pl.Series, comparable: bool, tmp_path: Path
) -> None:
    frame = pl.DataFrame({"l": left, "r": right})
    policy = QualityPolicy(
        compare_columns=(CompareColumnsRule(left="l", right="r", operator="lte"),)
    )

    result = _one(
        evaluate_quality(_silver(frame, tmp_path), policy, source_key="s"), "compare_columns"
    )

    if comparable:
        assert result.detail is None and result.evaluated_rows == 2
    else:
        assert result.detail is not None and "cannot be compared" in result.detail


def test_a_compare_counts_rows_where_both_sides_have_values(tmp_path: Path) -> None:
    frame = pl.DataFrame({"l": [1, 5, None, 3], "r": [2, 4, 1, None]})
    policy = QualityPolicy(
        compare_columns=(CompareColumnsRule(left="l", right="r", operator="lt", severity="fail"),)
    )

    result = _one(
        evaluate_quality(_silver(frame, tmp_path), policy, source_key="s"), "compare_columns"
    )

    assert (result.affected_rows, result.evaluated_rows, result.status) == (1, 2, "fail")


# ------------------------------------------------------------------------ errors


def test_an_engine_error_never_reaches_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: a DuckDB error, with whatever path or SQL it names, is not shown."""
    frame = pl.DataFrame({"v": [1, 2]})
    silver = _silver(frame, tmp_path)

    def broken(self: TableHandle, sql: str, params: object = ()) -> list[tuple[object, ...]]:
        raise duckdb.IOException(f"IO Error: cannot open {tmp_path}/secret/table.parquet")

    monkeypatch.setattr(TableHandle, "fetch", broken)
    policy = QualityPolicy(range=(RangeRule(column="v", min=0.0),))

    (result,) = evaluate_quality(silver, policy, source_key="s")

    assert result.detail == "column dtype Int64 cannot be compared with numeric range"
    assert "secret" not in repr(result)


# ------------------------------------------------------------------------ schema


def test_schema_dtypes_are_builder_canonical(tmp_path: Path) -> None:
    frame = pl.DataFrame({"a": [1], "b": ["x"], "c": [dt.datetime(2020, 1, 1)]})

    results = evaluate_quality(
        _silver(frame, tmp_path),
        None,
        source_key="s",
        column_dtypes={"a": "int", "b": "float", "c": "datetime"},
    )

    assert [(r.column, r.status, r.actual, r.threshold) for r in results] == [
        ("a", "pass", "Int64", "Int64"),
        ("b", "fail", "String", "Float64"),
        (
            "c",
            "pass",
            "Datetime(time_unit='us', time_zone=None)",
            "Datetime(time_unit='us', time_zone=None)",
        ),
    ]
    with pytest.raises(ValueError, match="Unsupported dtype"):
        evaluate_quality(_silver(frame, tmp_path), None, source_key="s", column_dtypes={"a": "num"})


def test_the_evaluator_does_not_import_polars() -> None:
    source = _EVALUATOR.read_text(encoding="utf-8")

    # A Polars dtype passed by a library caller is still read, lazily.
    assert not re.search(r"^(import polars|from polars)", source, flags=re.M)

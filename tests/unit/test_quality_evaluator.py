"""Quality/Schema structured evaluator unit tests (#486).

Verify ``quality.evaluate_quality`` shared by Preview/Build at SilverDataset level.
Pipeline integration (WARN continue/FAIL gate/manifest preserve) is test_pipeline.py,
drift scope is test_drift.py, API surface is test_dataset_api.py/test_stage_api.py respectively.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.quality import evaluate_quality
from kpubdata_builder.quality.models import QualityCheckResult
from kpubdata_builder.spec.models import CompareColumnsRule, QualityPolicy, RangeRule
from kpubdata_builder.stages.silver.models import SilverDataset, ValidationResult
from kpubdata_builder.tabular import PreviewSlice, compute_statistics, infer_schema
from kpubdata_builder.tabular.polars_bridge import handle_from_frame


def _silver(df: pl.DataFrame) -> SilverDataset:
    return SilverDataset(
        table=handle_from_frame(df, workdir=Path(tempfile.mkdtemp())),
        schema=infer_schema(df),
        statistics=compute_statistics(df),
        preview=PreviewSlice(rows=(), total_rows=df.height),
        validation=ValidationResult(ok=True),
        source_bronze="test.source",
    )


def _find(
    results: tuple[QualityCheckResult, ...], rule: str, column: str | None = None
) -> QualityCheckResult:
    matches = [r for r in results if r.rule == rule and r.column == column]
    assert len(matches) == 1, f"expected exactly one {rule}/{column} result, got {matches}"
    return matches[0]


class TestLegacyCompatibility:
    """Existing #446 syntax violations are default WARN and evaluate_quality judges identically."""

    def test_max_duplicate_rate_violation_is_warn_by_default(self) -> None:
        df = pl.DataFrame({"id": [1, 1, 2]})  # duplicate_rate = 1/3
        policy = QualityPolicy(max_duplicate_rate=0.1)

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "max_duplicate_rate")
        assert r.status == "warn"
        assert r.category == "duplicate"
        assert r.actual == pytest.approx(1 / 3)
        assert r.threshold == 0.1
        assert r.affected_rows is None  # Exact duplicate row count is not in current statistics.
        assert r.evaluated_rows == 3

    def test_max_duplicate_rate_equal_to_threshold_passes(self) -> None:
        df = pl.DataFrame({"id": [1, 1, 2, 3]})  # duplicate_rate = 0.25
        policy = QualityPolicy(max_duplicate_rate=0.25)

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "max_duplicate_rate").status == "pass"

    def test_max_null_ratio_violation_is_warn_by_default(self) -> None:
        df = pl.DataFrame({"price": [1, None, None, 4]})  # null ratio = 0.5
        policy = QualityPolicy(max_null_ratio={"price": 0.1})

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "max_null_ratio", "price")
        assert r.status == "warn"
        assert r.category == "missing"
        assert r.actual == pytest.approx(0.5)
        assert r.affected_rows == 2
        assert r.evaluated_rows == 4

    def test_min_rows_violation_is_warn_by_default(self) -> None:
        df = pl.DataFrame({"id": [1, 2]})
        policy = QualityPolicy(min_rows=10)

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "min_rows")
        assert r.status == "warn"
        assert r.category == "row_count"
        assert r.actual == 2
        assert r.threshold == 10

    def test_min_rows_equal_to_threshold_passes(self) -> None:
        df = pl.DataFrame({"id": [1, 2, 3]})
        policy = QualityPolicy(min_rows=3)

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "min_rows").status == "pass"

    def test_buildspec_round_trip_preserves_legacy_semantics(self) -> None:
        """Existing quality YAML (no new fields) still works."""
        from kpubdata_builder.spec.loader import parse_spec

        spec = parse_spec(
            {
                "dataset_id": "d",
                "title": "t",
                "description": "d",
                "sources": [{"provider": "p", "dataset": "s"}],
                "exports": [{"kind": "jsonl", "output_path": "o.jsonl"}],
                "quality": {
                    "max_duplicate_rate": 0.01,
                    "max_null_ratio": {"value": 0.05},
                    "min_rows": 100,
                },
            }
        )
        assert spec.quality is not None
        assert spec.quality.max_duplicate_rate == 0.01
        assert spec.quality.max_duplicate_rate_severity == "warn"
        assert spec.quality.max_null_ratio == {"value": 0.05}
        assert spec.quality.max_null_ratio_severity == {}
        assert spec.quality.min_rows == 100
        assert spec.quality.min_rows_severity == "warn"
        assert spec.quality.range == ()
        assert spec.quality.compare_columns == ()


class TestExplicitSeverity:
    def test_max_duplicate_rate_explicit_fail(self) -> None:
        df = pl.DataFrame({"id": [1, 1, 2]})
        policy = QualityPolicy(max_duplicate_rate=0.1, max_duplicate_rate_severity="fail")

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "max_duplicate_rate").status == "fail"

    def test_max_null_ratio_per_column_severity_override(self) -> None:
        df = pl.DataFrame({"a": [1, None], "b": [1, None]})
        policy = QualityPolicy(
            max_null_ratio={"a": 0.0, "b": 0.0},
            max_null_ratio_severity={"a": "fail"},  # b has no override → default warn.
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "max_null_ratio", "a").status == "fail"
        assert _find(results, "max_null_ratio", "b").status == "warn"

    def test_min_rows_explicit_fail(self) -> None:
        df = pl.DataFrame({"id": [1]})
        policy = QualityPolicy(min_rows=5, min_rows_severity="fail")

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "min_rows").status == "fail"


class TestMultipleChecksMixed:
    def test_pass_warn_fail_can_coexist(self) -> None:
        df = pl.DataFrame({"id": [1, 2, 3], "price": [1, None, 3]})
        policy = QualityPolicy(
            max_duplicate_rate=0.5,  # duplicate_rate=0.0 -> pass
            max_null_ratio={"price": 0.1},  # 1/3 → violation → warn (default).
            min_rows=5,
            min_rows_severity="fail",  # 3 < 5 -> fail
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "max_duplicate_rate").status == "pass"
        assert _find(results, "max_null_ratio", "price").status == "warn"
        assert _find(results, "min_rows").status == "fail"


class TestZeroAndUnevaluated:
    def test_row_count_zero_max_null_ratio_not_evaluated(self) -> None:
        df = pl.DataFrame({"price": []}, schema={"price": pl.Float64})
        policy = QualityPolicy(max_null_ratio={"price": 0.5})

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert not any(r.rule == "max_null_ratio" for r in results)

    def test_missing_column_max_null_ratio_not_evaluated(self) -> None:
        df = pl.DataFrame({"id": [1, 2]})
        policy = QualityPolicy(max_null_ratio={"does_not_exist": 0.5})

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert not any(r.rule == "max_null_ratio" for r in results)

    def test_no_policy_configured_yields_no_policy_checks(self) -> None:
        df = pl.DataFrame({"id": [1, 2]})

        results = evaluate_quality(_silver(df), None, source_key="s")

        assert results == ()

    def test_min_rows_zero_rows_still_evaluated_and_fails(self) -> None:
        """row_count 0 is a well-defined min_rows violation, not unevaluated."""
        df = pl.DataFrame({"id": []}, schema={"id": pl.Int64})
        policy = QualityPolicy(min_rows=1)

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "min_rows")
        assert r.status == "warn"
        assert r.actual == 0

    def test_unevaluated_never_produces_pass(self) -> None:
        """Unevaluated rules are completely excluded from result list, no arbitrary PASS created."""
        df = pl.DataFrame({"price": []}, schema={"price": pl.Float64})
        policy = QualityPolicy(max_null_ratio={"price": 0.9})

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert all(r.status != "pass" or r.rule != "max_null_ratio" for r in results)
        assert not any(r.rule == "max_null_ratio" for r in results)


class TestRangeRule:
    def test_inside_range_passes(self) -> None:
        df = pl.DataFrame({"price": [10, 20, 30]})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "range", "price")
        assert r.status == "pass"
        assert r.threshold == {"min": 0, "max": 100}
        assert r.affected_rows == 0
        assert r.evaluated_rows == 3

    def test_min_boundary_is_inclusive(self) -> None:
        df = pl.DataFrame({"price": [0, 5]})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "range", "price").status == "pass"

    def test_max_boundary_is_inclusive(self) -> None:
        df = pl.DataFrame({"price": [100, 5]})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "range", "price").status == "pass"

    def test_below_min_violates(self) -> None:
        df = pl.DataFrame({"price": [-1, 5]})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100, severity="fail"),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "range", "price")
        assert r.status == "fail"
        assert r.affected_rows == 1
        assert r.evaluated_rows == 2

    def test_above_max_violates(self) -> None:
        df = pl.DataFrame({"price": [101, 5]})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "range", "price")
        assert r.status == "warn"
        assert r.affected_rows == 1

    def test_null_values_excluded_from_evaluation(self) -> None:
        df = pl.DataFrame({"price": [None, None, 50]})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "range", "price")
        assert r.evaluated_rows == 1  # 2 nulls excluded from evaluated_rows.
        assert r.affected_rows == 0

    def test_all_null_column_not_evaluated(self) -> None:
        df = pl.DataFrame({"price": [None, None]}, schema={"price": pl.Float64})
        policy = QualityPolicy(range=(RangeRule(column="price", min=0, max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert not any(r.rule == "range" for r in results)

    def test_missing_column_not_evaluated(self) -> None:
        df = pl.DataFrame({"id": [1, 2]})
        policy = QualityPolicy(range=(RangeRule(column="does_not_exist", min=0, max=1),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert not any(r.rule == "range" for r in results)

    def test_warn_severity_default(self) -> None:
        df = pl.DataFrame({"price": [200]})
        policy = QualityPolicy(range=(RangeRule(column="price", max=100),))

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "range", "price").status == "warn"

    def test_single_bound_thresholds_are_preserved(self) -> None:
        df = pl.DataFrame({"price": [10]})
        policy = QualityPolicy(
            range=(
                RangeRule(column="price", min=0),
                RangeRule(column="price", max=100),
            )
        )

        results = [
            r for r in evaluate_quality(_silver(df), policy, source_key="s") if r.rule == "range"
        ]

        assert [r.threshold for r in results] == [
            {"min": 0, "max": None},
            {"min": None, "max": 100},
        ]

    @pytest.mark.parametrize(("severity", "expected"), [("warn", "warn"), ("fail", "fail")])
    def test_incompatible_dtype_is_structured_result(self, severity: str, expected: str) -> None:
        df = pl.DataFrame({"price": ["not-a-number"]})
        policy = QualityPolicy(
            range=(RangeRule(column="price", min=0, max=100, severity=severity),)
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "range", "price")
        assert r.status == expected
        assert r.actual is None
        assert r.threshold == {"min": 0, "max": 100}
        assert r.affected_rows is None
        assert r.evaluated_rows is None
        assert r.detail == "column dtype String cannot be compared with numeric range"


class TestCompareColumnsRule:
    @pytest.mark.parametrize(
        ("operator", "left_vals", "right_vals", "expect_violations"),
        [
            ("eq", [1, 2], [1, 2], 0),
            ("eq", [1, 2], [1, 3], 1),
            ("ne", [1, 2], [1, 3], 1),
            ("ne", [1, 2], [9, 9], 0),
            ("gt", [5, 1], [1, 1], 1),
            ("gte", [1, 1], [1, 2], 1),
            ("lt", [1, 5], [2, 1], 1),
            ("lte", [1, 3], [1, 2], 1),
        ],
    )
    def test_each_operator_counts_violations(
        self, operator: str, left_vals: list[int], right_vals: list[int], expect_violations: int
    ) -> None:
        df = pl.DataFrame({"left": left_vals, "right": right_vals})
        policy = QualityPolicy(
            compare_columns=(CompareColumnsRule(left="left", operator=operator, right="right"),)
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "compare_columns", "left,right")
        assert r.affected_rows == expect_violations
        assert r.status == ("pass" if expect_violations == 0 else "warn")
        assert r.threshold == {"operator": operator, "right_column": "right"}

    def test_null_rows_excluded_from_evaluation(self) -> None:
        df = pl.DataFrame({"left": [1, None, 3], "right": [1, 5, None]})
        policy = QualityPolicy(
            compare_columns=(CompareColumnsRule(left="left", operator="eq", right="right"),)
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "compare_columns", "left,right")
        assert r.evaluated_rows == 1  # Only rows with both columns non-null.
        assert r.affected_rows == 0

    def test_missing_column_not_evaluated(self) -> None:
        df = pl.DataFrame({"left": [1, 2]})
        policy = QualityPolicy(
            compare_columns=(CompareColumnsRule(left="left", operator="eq", right="nope"),)
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert not any(r.rule == "compare_columns" for r in results)

    def test_invalid_operator_rejected_at_load_time(self) -> None:
        from kpubdata_builder.errors import SpecLoadError
        from kpubdata_builder.spec.loader import parse_spec

        with pytest.raises(SpecLoadError):
            parse_spec(
                {
                    "dataset_id": "d",
                    "title": "t",
                    "description": "d",
                    "sources": [{"provider": "p", "dataset": "s"}],
                    "exports": [{"kind": "jsonl", "output_path": "o.jsonl"}],
                    "quality": {
                        "compare_columns": [
                            {"left": "a", "operator": "not_an_operator", "right": "b"}
                        ]
                    },
                }
            )

    def test_explicit_fail_severity(self) -> None:
        df = pl.DataFrame({"left": [10], "right": [1]})
        policy = QualityPolicy(
            compare_columns=(
                CompareColumnsRule(left="left", operator="lte", right="right", severity="fail"),
            )
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        assert _find(results, "compare_columns", "left,right").status == "fail"

    def test_different_operators_remain_distinguishable(self) -> None:
        df = pl.DataFrame({"left": [1], "right": [1]})
        policy = QualityPolicy(
            compare_columns=(
                CompareColumnsRule(left="left", operator="eq", right="right"),
                CompareColumnsRule(left="left", operator="ne", right="right"),
            )
        )

        results = [
            r
            for r in evaluate_quality(_silver(df), policy, source_key="s")
            if r.rule == "compare_columns"
        ]

        assert [r.threshold for r in results] == [
            {"operator": "eq", "right_column": "right"},
            {"operator": "ne", "right_column": "right"},
        ]

    @pytest.mark.parametrize(("severity", "expected"), [("warn", "warn"), ("fail", "fail")])
    def test_incompatible_dtypes_are_structured_result(self, severity: str, expected: str) -> None:
        df = pl.DataFrame({"left": ["x"], "right": [1]})
        policy = QualityPolicy(
            compare_columns=(
                CompareColumnsRule(left="left", operator="gt", right="right", severity=severity),
            )
        )

        results = evaluate_quality(_silver(df), policy, source_key="s")

        r = _find(results, "compare_columns", "left,right")
        assert r.status == expected
        assert r.actual is None
        assert r.threshold == {"operator": "gt", "right_column": "right"}
        assert r.affected_rows is None
        assert r.evaluated_rows is None
        assert r.detail == "column dtypes String and Int64 cannot be compared with operator gt"


class TestSchemaChecks:
    def test_required_column_present_passes(self) -> None:
        df = pl.DataFrame({"id": [1]})

        results = evaluate_quality(_silver(df), None, source_key="s", required_columns=("id",))

        r = _find(results, "required_column", "id")
        assert r.status == "pass"
        assert r.category == "schema"

    def test_required_column_missing_fails(self) -> None:
        df = pl.DataFrame({"id": [1]})

        results = evaluate_quality(
            _silver(df), None, source_key="s", required_columns=("missing_col",)
        )

        r = _find(results, "required_column", "missing_col")
        assert r.status == "fail"

    def test_dtype_match_passes(self) -> None:
        df = pl.DataFrame({"amount": [1, 2]})  # Int64

        results = evaluate_quality(
            _silver(df), None, source_key="s", column_dtypes={"amount": "int64"}
        )

        assert _find(results, "dtype", "amount").status == "pass"

    def test_dtype_mismatch_fails(self) -> None:
        df = pl.DataFrame({"amount": ["a", "b"]})  # String

        results = evaluate_quality(
            _silver(df), None, source_key="s", column_dtypes={"amount": "int64"}
        )

        assert _find(results, "dtype", "amount").status == "fail"

    def test_missing_required_column_does_not_generate_dtype_pass(self) -> None:
        """If required column is absent and dtype cannot be checked, no dtype PASS is created.

        prevent contradictions like "required FAIL + dtype PASS" (#486).
        """
        df = pl.DataFrame({"id": [1]})

        results = evaluate_quality(
            _silver(df),
            None,
            source_key="s",
            required_columns=("amount",),
            column_dtypes={"amount": "int64"},
        )

        assert _find(results, "required_column", "amount").status == "fail"
        assert not any(r.rule == "dtype" and r.column == "amount" for r in results)

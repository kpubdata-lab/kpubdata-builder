"""Quality/Schema structured evaluator (#486).

Provide single pure function ``evaluate_quality`` shared by Preview and Build.
For same SilverDataset and same config (policy/required_columns/column_dtypes),
always return same PASS/WARN/FAIL result whether Preview or Build — both paths
don't have separate evaluation implementations.

Evaluates:
    - schema contract: required column presence, declared dtype match.
    - QualityPolicy: max_duplicate_rate, max_null_ratio (per-column), min_rows,
      range (#486 typed rule), compare_columns (#486 typed rule, limited operators only).

Principles (#486):
    - If rule unset or unevaluable (column missing, denominator 0, etc), exclude
      that check from results — don't pretend it PASSed.
    - Threshold comparison is deterministic. LLM/AI interpretation doesn't touch
      this module.
    - range/compare_columns use only Polars vectorized ops (no free-form eval).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import polars as pl

from ..spec.models import CompareColumnsRule, JsonValue, QualityPolicy, RangeRule
from ..stages.silver.models import SilverDataset
from ..tabular.polars_bridge import to_polars
from ..tabular.polars_helpers import DtypeSpec, _resolve_dtype
from .models import QualityCheckResult, QualityStatus

_COMPARE_OPERATORS: dict[str, Callable[[pl.Expr, pl.Expr], pl.Expr]] = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
}


def _severity_status(violated: bool, severity: str) -> QualityStatus:
    if not violated:
        return "pass"
    return "fail" if severity == "fail" else "warn"


def _range_threshold(rule: RangeRule) -> dict[str, JsonValue]:
    """Preserve original range rule boundaries in result without loss."""
    return {"min": rule.min, "max": rule.max}


def _compare_threshold(rule: CompareColumnsRule) -> dict[str, JsonValue]:
    """Preserve operator and right column of column comparison in result."""
    return {"operator": rule.operator, "right_column": rule.right}


def _schema_results(
    table: pl.DataFrame,
    *,
    source_key: str,
    required_columns: Sequence[str],
    column_dtypes: Mapping[str, DtypeSpec] | None,
) -> list[QualityCheckResult]:
    """Convert required column/dtype contract to QualityCheckResult.

    If required column missing so can't check dtype, skip dtype check entirely
    — don't create contradictions like "required FAIL + dtype PASS".
    """
    results: list[QualityCheckResult] = []
    columns = set(table.columns)
    for col in required_columns:
        present = col in columns
        results.append(
            QualityCheckResult(
                source_key=source_key,
                category="schema",
                rule="required_column",
                column=col,
                status="pass" if present else "fail",
                actual=present,
                threshold=True,
            )
        )
    for col, expected_spec in (column_dtypes or {}).items():
        if col not in columns:
            continue  # Column itself does not exist so dtype cannot be checked — no PASS made.
        expected = _resolve_dtype(expected_spec)
        actual_dtype = table.schema[col]
        results.append(
            QualityCheckResult(
                source_key=source_key,
                category="schema",
                rule="dtype",
                column=col,
                status="pass" if actual_dtype == expected else "fail",
                actual=str(actual_dtype),
                threshold=str(expected),
            )
        )
    return results


def _duplicate_result(
    silver: SilverDataset, policy: QualityPolicy, *, source_key: str
) -> QualityCheckResult | None:
    if policy.max_duplicate_rate is None:
        return None
    actual = silver.statistics.duplicate_rate
    violated = actual > policy.max_duplicate_rate
    return QualityCheckResult(
        source_key=source_key,
        category="duplicate",
        rule="max_duplicate_rate",
        column=None,
        status=_severity_status(violated, policy.max_duplicate_rate_severity),
        actual=actual,
        threshold=policy.max_duplicate_rate,
        # Exact duplicate row count is not in current stats (only duplicate_rate) — cannot
        # Do not reverse-engineer arbitrary integers (#486).
        affected_rows=None,
        evaluated_rows=silver.statistics.row_count,
    )


def _null_ratio_results(
    silver: SilverDataset, policy: QualityPolicy, *, source_key: str
) -> list[QualityCheckResult]:
    results: list[QualityCheckResult] = []
    row_count = silver.statistics.row_count
    for col, max_ratio in policy.max_null_ratio.items():
        if row_count == 0:
            continue  # denominator 0 — don't force ratio to 0/PASS.
        null_count = silver.statistics.null_counts.get(col)
        if null_count is None:
            continue  # Column not in stats (doesn't exist) — cannot evaluate.
        actual = null_count / row_count
        severity = policy.max_null_ratio_severity.get(col, "warn")
        violated = actual > max_ratio
        results.append(
            QualityCheckResult(
                source_key=source_key,
                category="missing",
                rule="max_null_ratio",
                column=col,
                status=_severity_status(violated, severity),
                actual=actual,
                threshold=max_ratio,
                affected_rows=null_count,
                evaluated_rows=row_count,
            )
        )
    return results


def _min_rows_result(
    silver: SilverDataset, policy: QualityPolicy, *, source_key: str
) -> QualityCheckResult | None:
    if policy.min_rows is None:
        return None
    actual = silver.statistics.row_count
    violated = actual < policy.min_rows
    return QualityCheckResult(
        source_key=source_key,
        category="row_count",
        rule="min_rows",
        column=None,
        status=_severity_status(violated, policy.min_rows_severity),
        actual=actual,
        threshold=policy.min_rows,
    )


def _range_result(
    table: pl.DataFrame, rule: RangeRule, *, source_key: str
) -> QualityCheckResult | None:
    if rule.column not in table.columns:
        return None
    non_null = table.filter(pl.col(rule.column).is_not_null())
    evaluated_rows = non_null.height
    if evaluated_rows == 0:
        return None
    conditions: list[pl.Expr] = []
    if rule.min is not None:
        conditions.append(pl.col(rule.column) >= rule.min)
    if rule.max is not None:
        conditions.append(pl.col(rule.column) <= rule.max)
    if not conditions:
        # Both min/max absent — no boundary to check. validate_spec already rejects this config
        # (empty_range_rule), but evaluator doesn't depend on that call.
        return None
    condition = conditions[0]
    for extra in conditions[1:]:
        condition = condition & extra
    try:
        passing = non_null.filter(condition).height
    except pl.exceptions.PolarsError:
        return QualityCheckResult(
            source_key=source_key,
            category="range",
            rule="range",
            column=rule.column,
            status=_severity_status(True, rule.severity),
            actual=None,
            threshold=_range_threshold(rule),
            affected_rows=None,
            evaluated_rows=None,
            detail=(
                f"column dtype {table.schema[rule.column]} cannot be compared with numeric range"
            ),
        )
    affected_rows = evaluated_rows - passing
    return QualityCheckResult(
        source_key=source_key,
        category="range",
        rule="range",
        column=rule.column,
        status=_severity_status(affected_rows > 0, rule.severity),
        actual=affected_rows / evaluated_rows,
        threshold=_range_threshold(rule),
        affected_rows=affected_rows,
        evaluated_rows=evaluated_rows,
    )


def _compare_columns_result(
    table: pl.DataFrame, rule: CompareColumnsRule, *, source_key: str
) -> QualityCheckResult | None:
    if rule.left not in table.columns or rule.right not in table.columns:
        return None
    both = table.filter(pl.col(rule.left).is_not_null() & pl.col(rule.right).is_not_null())
    evaluated_rows = both.height
    if evaluated_rows == 0:
        return None
    comparator = _COMPARE_OPERATORS[rule.operator]
    try:
        satisfied = both.filter(comparator(pl.col(rule.left), pl.col(rule.right))).height
    except pl.exceptions.PolarsError:
        return QualityCheckResult(
            source_key=source_key,
            category="compare_columns",
            rule="compare_columns",
            column=f"{rule.left},{rule.right}",
            status=_severity_status(True, rule.severity),
            actual=None,
            threshold=_compare_threshold(rule),
            affected_rows=None,
            evaluated_rows=None,
            detail=(
                f"column dtypes {table.schema[rule.left]} and {table.schema[rule.right]} "
                f"cannot be compared with operator {rule.operator}"
            ),
        )
    affected_rows = evaluated_rows - satisfied
    return QualityCheckResult(
        source_key=source_key,
        category="compare_columns",
        rule="compare_columns",
        column=f"{rule.left},{rule.right}",
        status=_severity_status(affected_rows > 0, rule.severity),
        actual=affected_rows / evaluated_rows,
        threshold=_compare_threshold(rule),
        affected_rows=affected_rows,
        evaluated_rows=evaluated_rows,
    )


def evaluate_quality(
    silver: SilverDataset,
    policy: QualityPolicy | None,
    *,
    source_key: str,
    required_columns: Sequence[str] = (),
    column_dtypes: Mapping[str, DtypeSpec] | None = None,
) -> tuple[QualityCheckResult, ...]:
    """Evaluate schema contract + QualityPolicy on SilverDataset (#486).

    Common entry point for Preview/Build — pure function (no side effects), so
    identical input always returns identical result. Unevaluable rules excluded
    from results (don't pretend they PASSed).

    Parameters:
        silver: Silver artifact to evaluate.
        policy: QualityPolicy to apply. None evaluates schema contract only
            (#446 backward compat).
        source_key: Source identifier for results.
        required_columns: Required column list, same as used at Silver creation.
        column_dtypes: Expected per-column dtype, same as used at Silver creation.

    Returns:
        Tuple of QualityCheckResult. Contains only actually-evaluated checks (PASS included).
    """
    # Quality still reads a Polars frame until it runs on DuckDB (#872).
    table = to_polars(silver.table)
    results: list[QualityCheckResult] = _schema_results(
        table,
        source_key=source_key,
        required_columns=required_columns,
        column_dtypes=column_dtypes,
    )
    if policy is None:
        return tuple(results)

    dup = _duplicate_result(silver, policy, source_key=source_key)
    if dup is not None:
        results.append(dup)
    results.extend(_null_ratio_results(silver, policy, source_key=source_key))
    min_rows_result = _min_rows_result(silver, policy, source_key=source_key)
    if min_rows_result is not None:
        results.append(min_rows_result)
    for range_rule in policy.range:
        range_result = _range_result(table, range_rule, source_key=source_key)
        if range_result is not None:
            results.append(range_result)
    for compare_rule in policy.compare_columns:
        compare_result = _compare_columns_result(table, compare_rule, source_key=source_key)
        if compare_result is not None:
            results.append(compare_result)
    return tuple(results)


__all__ = ["evaluate_quality"]

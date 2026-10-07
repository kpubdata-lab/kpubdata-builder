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
    - range/compare_columns run as DuckDB SQL over the Silver table (#872), the rule's
      bounds bound as parameters — never free-form eval. Which columns a rule can
      compare is decided here from their Builder dtypes, never by DuckDB's implicit
      casts: ``range`` reads numeric columns (``RangeRule`` is a numeric rule), and
      ``compare_columns`` compares two numeric columns or two of the same dtype. Any
      other pairing is reported as not comparable — under Polars some were compared
      silently (a date or boolean column against a number, a decimal against text).
    - A DuckDB error never reaches the result: the detail is Builder's own sentence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import duckdb

from ..spec.models import CompareColumnsRule, JsonValue, QualityPolicy, RangeRule
from ..stages.silver.models import SilverDataset
from ..tabular.cast_names import NAMED_TARGETS
from ..tabular.duckdb_load import TableHandle, canonical, node_of
from .models import QualityCheckResult, QualityStatus

#: A dtype spec: a name (``"int"``, ``"str"``, …) or, from library callers, a Polars dtype.
DtypeSpec = object

_COMPARE_OPERATORS: dict[str, str] = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}
_NUMERIC = frozenset({"int", "int128", "float", "decimal"})
#: The dtype names a schema contract may declare (``tabular.cast_names``).
_SCHEMA_DTYPES = tuple(NAMED_TARGETS)


def _expected_dtype(spec: DtypeSpec) -> str:
    """The Builder canonical dtype a schema contract declares (schema drift's own names)."""
    if isinstance(spec, str):
        normalized = spec.strip().lower()
        if normalized not in _SCHEMA_DTYPES:
            supported = ", ".join(sorted(_SCHEMA_DTYPES))
            raise ValueError(f"Unsupported dtype: {spec!r}. Supported: {supported}")
        return canonical(node_of(normalized))
    # A Polars dtype (or dtype class) from a library caller: its printed name is the
    # canonical one, read without importing Polars.
    return str(spec() if isinstance(spec, type) else spec)


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
    table: TableHandle,
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
    dtypes = dict(zip(table.columns, table.dtypes, strict=True))
    for col, expected_spec in (column_dtypes or {}).items():
        if col not in columns:
            continue  # Column itself does not exist so dtype cannot be checked — no PASS made.
        expected = _expected_dtype(expected_spec)
        actual_dtype = dtypes[col]
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


def _node(table: TableHandle, column: str) -> tuple[object, ...]:
    return table.table.nodes[table.table.names.index(column)]


def _dtype(table: TableHandle, column: str) -> str:
    return table.table.dtypes[table.table.names.index(column)]


def _counts(
    table: TableHandle, evaluated: str, passing: str, params: list[object]
) -> tuple[int, int]:
    """Rows where ``evaluated`` holds, and of those, rows where ``passing`` holds."""
    (row,) = table.fetch(
        f"SELECT count(*) FILTER (WHERE {evaluated}), "
        f"count(*) FILTER (WHERE {evaluated} AND ({passing})) FROM {table.table.relation.sql}",
        params,
    )
    return int(row[0]), int(row[1])


def _range_result(
    table: TableHandle, rule: RangeRule, *, source_key: str
) -> QualityCheckResult | None:
    if rule.column not in table.columns:
        return None
    if rule.min is None and rule.max is None:
        # Both min/max absent — no boundary to check. validate_spec already rejects this config
        # (empty_range_rule), but evaluator doesn't depend on that call.
        return None
    column = table.table.column(rule.column)
    conditions: list[str] = []
    params: list[object] = []
    for op, bound in ((">=", rule.min), ("<=", rule.max)):
        if bound is not None:
            conditions.append(f"{column} {op} ?")
            params.append(bound)
    not_comparable = QualityCheckResult(
        source_key=source_key,
        category="range",
        rule="range",
        column=rule.column,
        status=_severity_status(True, rule.severity),
        actual=None,
        threshold=_range_threshold(rule),
        affected_rows=None,
        evaluated_rows=None,
        detail=(f"column dtype {_dtype(table, rule.column)} cannot be compared with numeric range"),
    )
    present = f"{column} IS NOT NULL"
    try:
        if _node(table, rule.column)[0] not in _NUMERIC:
            evaluated_rows, _ = _counts(table, present, "true", [])
            # Nothing to compare is not a violation; a value of another kind is.
            return None if evaluated_rows == 0 else not_comparable
        evaluated_rows, passing = _counts(table, present, " AND ".join(conditions), params)
    except duckdb.Error:
        return not_comparable
    if evaluated_rows == 0:
        return None
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


def _comparable(table: TableHandle, left: str, right: str) -> bool:
    """Two numeric columns, or two of the same dtype."""
    if _node(table, left)[0] in _NUMERIC and _node(table, right)[0] in _NUMERIC:
        return True
    kind = _node(table, left)[0]
    return _dtype(table, left) == _dtype(table, right) and kind not in ("list", "struct", "null")


def _compare_columns_result(
    table: TableHandle, rule: CompareColumnsRule, *, source_key: str
) -> QualityCheckResult | None:
    if rule.left not in table.columns or rule.right not in table.columns:
        return None
    left = table.table.column(rule.left)
    right = table.table.column(rule.right)
    not_comparable = QualityCheckResult(
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
            f"column dtypes {_dtype(table, rule.left)} and {_dtype(table, rule.right)} "
            f"cannot be compared with operator {rule.operator}"
        ),
    )
    both = f"{left} IS NOT NULL AND {right} IS NOT NULL"
    try:
        if not _comparable(table, rule.left, rule.right):
            evaluated_rows, _ = _counts(table, both, "true", [])
            return None if evaluated_rows == 0 else not_comparable
        evaluated_rows, satisfied = _counts(
            table, both, f"{left} {_COMPARE_OPERATORS[rule.operator]} {right}", []
        )
    except duckdb.Error:
        return not_comparable
    if evaluated_rows == 0:
        return None
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
    table = silver.table
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

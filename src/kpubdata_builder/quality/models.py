"""Quality/Schema Drift structured result model (#486).

Define QualityCheckResult normalizing evaluation results of Silver statistics/table/
schema contract to PASS/WARN/FAIL, and SchemaDriftFinding for results from
``detect_drift()`` (#445).

Principles:
    - Don't create arbitrary composite Quality Score — only individual checks exist.
    - Rule unset/unevaluated excluded entirely from results (don't pretend PASS).
    - If affected_rows/evaluated_rows meaningless, use None not fake 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..spec.models import JsonPrimitive, JsonValue

QualityStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class QualityCheckResult:
    """Structured result of single quality/schema check (#486).

    Attributes:
        source_key: Source identifier (output-facing key).
        category: Check category (e.g., "duplicate", "missing", "row_count", "schema",
            "range", "compare_columns").
        rule: Specific rule name (e.g., "max_duplicate_rate", "max_null_ratio",
            "min_rows", "required_column", "dtype", "range", "compare_columns").
        column: Related column name. For table-wide rules or column pairs like
            compare_columns (``"{left},{right}"`` format), None or composite value.
        status: "pass" | "warn" | "fail".
        actual: Actual observed value (JSON-serializable scalar).
        threshold: Comparison threshold (JSON-serializable scalar).
        affected_rows: Rows violating rule. None if meaningless or can't count exactly
            (don't guess arbitrary integers).
        evaluated_rows: Rows actually used in evaluation. None if meaningless.
    """

    source_key: str
    category: str
    rule: str
    column: str | None
    status: QualityStatus
    actual: JsonPrimitive
    threshold: JsonValue
    affected_rows: int | None = None
    evaluated_rows: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class SchemaDriftFinding:
    """Structured schema drift observation for API/manifest (#445, #486).

    Same fields as ``stages.silver.drift.DriftFinding`` — deterministic detection
    result moved as-is; drift itself doesn't participate in PASS/WARN/FAIL gate
    (for reference only). Even if AI interpretation added in #448, this structure
    doesn't change.

    Attributes:
        kind: column_added | column_removed | dtype_changed | row_count_jump.
        column: Related column name. None if table-wide issue.
        detail: Human-readable explanation.
    """

    kind: str
    column: str | None
    detail: str


@dataclass(frozen=True)
class DriftEvaluation:
    """Whether a drift comparison actually happened, per source and axis (#700).

    ``SchemaDriftFinding`` says what changed. This says whether anyone looked.

    They are not the same question, and conflating them is the defect this exists
    to remove: an empty finding list used to mean both "compared, nothing changed"
    and "there was nothing to compare against", and the manifest dropped the key in
    both cases. A quality screen then showed a table as clean that had never been
    checked.

    An absent ``drift_evaluation`` key means a run from before this field existed —
    unknown, not healthy.

    Attributes:
        axis: ``schema`` or ``volume``. They do not share a baseline: a coverage
            change should not alter the column set, but it does make row counts
            incomparable.
        evaluated: Whether a baseline was found and the comparison ran.
        reason: When ``evaluated`` is false, why — the value of
            ``warehouse.baseline.NotEvaluatedReason``.
        baseline_snapshot_id: The snapshot compared against, when there was one.
        detail: Human-readable context, safe to show in a report.
    """

    axis: str
    evaluated: bool
    reason: str | None = None
    baseline_snapshot_id: str | None = None
    detail: str | None = None


__all__ = [
    "DriftEvaluation",
    "QualityCheckResult",
    "QualityStatus",
    "SchemaDriftFinding",
]

"""Quality/Schema Drift 구조화 결과 모델 (#486).

Silver 통계·테이블·schema 계약을 평가한 결과를 PASS/WARN/FAIL로 정규화한
``QualityCheckResult``와, ``detect_drift()``(#445)의 결과를 API/manifest에 실을 수
있게 옮긴 ``SchemaDriftFinding``을 정의한다.

원칙:
    - 임의 종합 Quality Score를 만들지 않는다 — 개별 check만 존재한다.
    - rule 미설정/미평가는 결과 목록에서 아예 제외한다(PASS로 가장하지 않는다).
    - affected_rows/evaluated_rows가 의미 없으면 0으로 위장하지 않고 None을 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..spec.models import JsonPrimitive, JsonValue

QualityStatus = Literal["pass", "warn", "fail"]


@dataclass(frozen=True)
class QualityCheckResult:
    """단일 quality/schema check의 구조화된 결과 (#486).

    속성:
        source_key: 소스 식별자 (output-facing key).
        category: check 대분류 (예: "duplicate", "missing", "row_count", "schema",
            "range", "compare_columns").
        rule: 구체적 rule 이름 (예: "max_duplicate_rate", "max_null_ratio",
            "min_rows", "required_column", "dtype", "range", "compare_columns").
        column: 관련 컬럼명. 테이블 전체 규칙이거나 compare_columns처럼 컬럼 쌍인
            경우(``"{left},{right}"`` 형식) 등 단일 컬럼이 아니면 None 또는 합성값.
        status: "pass" | "warn" | "fail".
        actual: 실제 관측값 (JSON 직렬화 가능한 스칼라).
        threshold: 비교 대상 임계값 (JSON 직렬화 가능한 스칼라).
        affected_rows: 위반한 행 수. 의미 없거나 정확히 셀 수 없으면 None(임의
            정수 추정 금지).
        evaluated_rows: 실제로 평가에 사용된 행 수. 의미 없으면 None.
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
    """API/manifest에 실을 구조화된 schema drift 관찰 (#445, #486).

    ``stages.silver.drift.DriftFinding``과 필드가 같다 — deterministic 감지
    결과를 그대로 옮긴 것이며, drift 자체는 PASS/WARN/FAIL 게이트에 관여하지
    않는다(참고용). #448 AI 해석이 붙더라도 이 구조는 바뀌지 않는다.

    속성:
        kind: column_added | column_removed | dtype_changed | row_count_jump.
        column: 관련 컬럼명. 테이블 전체 문제면 None.
        detail: 사람이 읽는 설명.
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

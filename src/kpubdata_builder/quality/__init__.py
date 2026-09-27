"""Quality/Schema Drift 구조화 결과 패키지 (#486).

Preview/Build가 공유하는 단일 evaluator(``evaluate_quality``)와 그 결과 모델
(``QualityCheckResult``, ``SchemaDriftFinding``)을 노출한다.

주요 구성:
    - QualityCheckResult / QualityStatus: 개별 quality/schema check 결과
    - SchemaDriftFinding: API/manifest에 실을 구조화된 drift 관찰
    - evaluate_quality: Preview/Build 공통 평가 진입점
"""

from __future__ import annotations

from .evaluator import evaluate_quality
from .models import (
    DriftEvaluation,
    QualityCheckResult,
    QualityStatus,
    SchemaDriftFinding,
)

__all__ = [
    "DriftEvaluation",
    "QualityCheckResult",
    "QualityStatus",
    "SchemaDriftFinding",
    "evaluate_quality",
]

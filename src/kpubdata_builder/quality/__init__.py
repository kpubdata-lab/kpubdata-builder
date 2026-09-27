"""Quality/Schema Drift structured result package (#486).

Expose single evaluator shared by Preview/Build (``evaluate_quality``) and its
result models (``QualityCheckResult``, ``SchemaDriftFinding``).

Key components:
    - QualityCheckResult / QualityStatus: individual quality/schema check result
    - SchemaDriftFinding: structured drift observation for API/manifest
    - evaluate_quality: common evaluation entry point for Preview/Build
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

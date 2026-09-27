"""Pipeline orchestration package (#48).

Bundles the orchestrator for the Bronze → Silver → Gold → (Export) →
Manifest flow and the execution context.
"""

from __future__ import annotations

from .cancellation import CancellationProbe
from .context import BuildContext
from .orchestrator import BuildResult, CompositionOutcome, SourceBuildOutcome, run_build
from .preview import (
    DEFAULT_PREVIEW_SEED,
    MAX_PREVIEW_DIFF_ITEMS,
    PreviewDiffItem,
    PreviewResult,
    PreviewTransformSummary,
    SampleMode,
    SourcePreview,
    preview_build,
)

__all__ = [
    "DEFAULT_PREVIEW_SEED",
    "MAX_PREVIEW_DIFF_ITEMS",
    "BuildContext",
    "BuildResult",
    "CancellationProbe",
    "CompositionOutcome",
    "PreviewDiffItem",
    "PreviewResult",
    "PreviewTransformSummary",
    "SampleMode",
    "SourceBuildOutcome",
    "SourcePreview",
    "preview_build",
    "run_build",
]

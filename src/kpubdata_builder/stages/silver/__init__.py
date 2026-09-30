"""Silver stage package (#46).

Exposes Silver stage implementation that loads Bronze raw records into DuckDB (#869) and
generates schema validation, statistics summary, and preview.

Main components:
    - SilverDataset / ValidationResult / ValidationProblem: output and validation models
    - build_silver_dataset: BronzeArtifact → SilverDataset
    - persist_silver_dataset: persist Silver output
"""

from __future__ import annotations

from .build import build_silver_dataset
from .models import SilverDataset, ValidationResult
from .normalize import normalize_table
from .persist import SilverPersistResult, persist_silver_dataset
from .preview import build_preview
from .summarize import build_schema, build_statistics
from .validate import ValidationProblem, validate_table

__all__ = [
    "SilverDataset",
    "SilverPersistResult",
    "ValidationProblem",
    "ValidationResult",
    "build_preview",
    "build_schema",
    "build_silver_dataset",
    "build_statistics",
    "normalize_table",
    "persist_silver_dataset",
    "validate_table",
]

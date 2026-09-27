"""Bronze stage public API.

This package re-exposes functions and models needed to represent raw data collection
results and persist them to disk.
"""

from __future__ import annotations

from .build import build_bronze_artifact
from .models import BronzeArtifact, ProvenanceEvent
from .persist import BronzePersistResult, persist_bronze_artifact
from .resolve import build_bronze_artifact_for_source, source_identity

__all__ = [
    "BronzeArtifact",
    "BronzePersistResult",
    "ProvenanceEvent",
    "build_bronze_artifact",
    "build_bronze_artifact_for_source",
    "persist_bronze_artifact",
    "source_identity",
]

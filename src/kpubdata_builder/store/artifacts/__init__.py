"""Artifact/manifest store abstraction (ADR 0010; local files only, ADR 0022).

``ArtifactStore`` Protocol and its implementation ``LocalArtifactStore``. The Protocol is
kept so that another store can be considered later (ADR 0022).
"""

from __future__ import annotations

from pathlib import Path

from .local import LocalArtifactStore
from .protocol import ArtifactStore


def make_artifact_store(output_root: Path) -> ArtifactStore:
    """Create the ``ArtifactStore``, refusing a removed storage setting first (ADR 0022)."""
    from ..backend import refuse_removed_storage_settings

    refuse_removed_storage_settings()
    return LocalArtifactStore(output_root)


__all__ = ["ArtifactStore", "LocalArtifactStore", "make_artifact_store"]

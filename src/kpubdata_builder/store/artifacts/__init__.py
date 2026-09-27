"""Artifact/manifest store abstraction (ADR 0010/0016).

``ArtifactStore`` Protocol and default implementation ``LocalArtifactStore`` (no external deps).
``make_artifact_store()`` factory switches based on ``KPUBDATA_BUILDER_STORAGE_BACKEND``
to sqlite/local
or cubrid implementation. ``CubridArtifactStore`` is lazy imported only on cubrid selection.
"""

from __future__ import annotations

from pathlib import Path

from .local import LocalArtifactStore
from .protocol import ArtifactStore


def make_artifact_store(output_root: Path) -> ArtifactStore:
    """Create ``ArtifactStore`` for selected backend (ADR 0016)."""
    from ..backend import storage_backend

    if storage_backend() == "cubrid":
        from ..backend import get_engine
        from .cubrid import CubridArtifactStore

        return CubridArtifactStore(output_root, get_engine())
    return LocalArtifactStore(output_root)


__all__ = ["ArtifactStore", "LocalArtifactStore", "make_artifact_store"]

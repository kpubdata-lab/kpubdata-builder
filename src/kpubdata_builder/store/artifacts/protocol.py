"""ArtifactStore Protocol (ADR 0010; local files only, ADR 0022).

Abstracts artifact workspace access and manifest document ownership.

- **Artifact bytes** (parquet/CSV/HF layout etc) are on the local filesystem —
  ``query/engine.py`` lazy scans actual paths in a separate process via
  ``pl.scan_parquet``, so ``run_dir()`` returns a filesystem path.
- **manifest documents**: the ``manifest.json`` file is canonical (ADR 0003, restored
  without exception by ADR 0022).
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    """Artifact workspace + manifest document storage interface."""

    def run_dir(self, run_id: str) -> Path:
        """Run artifact workspace path (FS). Used for byte read/write, serving, querying."""
        ...

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        """Return manifest document (None if absent or corrupted)."""
        ...

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        """Record manifest document to the canonical ``manifest.json``."""
        ...

    def list_run_ids(self) -> list[str]:
        """List of run_ids with manifest."""
        ...


__all__ = ["ArtifactStore"]

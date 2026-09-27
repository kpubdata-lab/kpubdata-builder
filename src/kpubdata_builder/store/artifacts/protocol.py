"""ArtifactStore Protocol (ADR 0010/0016).

Abstracts artifact workspace access and manifest document ownership. Approved design (ADR 0016):

- **Artifact bytes** (parquet/CSV/HF layout etc) on both backends use local filesystem
  (OCI block volume) — ``query/engine.py`` lazy scans actual paths in separate
  process via ``pl.scan_parquet``
  lazy scans actual paths, so file paths needed; putting large files in RDBMS BLOB is
  antipattern, no shared object store benefit with single replica. Therefore,
  ``run_dir()`` returns FS path identically in both implementations.
- **manifest documents** differ by backend. ``CubridArtifactStore`` uses CUBRID row as canonical
  keeps FS as mirror (cache) (ADR 0003 supersede). ``LocalArtifactStore`` uses FS
  file itself as canonical.
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
        """Return manifest document (None if absent or corrupted). CUBRID
        backend prefers canonical row."""
        ...

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        """Record manifest document to authoritative store (CUBRID row + FS mirror)."""
        ...

    def list_run_ids(self) -> list[str]:
        """List of run_ids with manifest."""
        ...


__all__ = ["ArtifactStore"]

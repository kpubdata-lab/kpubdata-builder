"""LocalArtifactStore — local filesystem-based artifact/manifest store (default).

Wrap current behavior byte-identical. Manifest canonical is ``output_root/<run_id>/manifest.json``
file; ``get_manifest`` uses same path safety as existing ``datasets_service.read_manifest``
validation performed. No-external-deps default (AGENTS.md).
"""

from __future__ import annotations

import json
from pathlib import Path

from ...stages._path_safety import ensure_within

_MANIFEST_FILENAME = "manifest.json"


class LocalArtifactStore:
    """Filesystem-based ArtifactStore implementation."""

    def __init__(self, output_root: Path) -> None:
        self._output_root = output_root

    def run_dir(self, run_id: str) -> Path:
        return self._output_root / run_id

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        manifest_path = self._output_root / run_id / _MANIFEST_FILENAME
        try:
            ensure_within(self._output_root, manifest_path, label="manifest file")
        except ValueError:
            return None
        if not manifest_path.is_file():
            return None
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        run_dir = self._output_root / run_id
        manifest_path = run_dir / _MANIFEST_FILENAME
        ensure_within(self._output_root, manifest_path, label="manifest file")
        run_dir.mkdir(parents=True, exist_ok=True)
        # Same deterministic serialization as manifest_writer (sorted keys, UTF-8, indent=2).
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def list_run_ids(self) -> list[str]:
        if not self._output_root.exists():
            return []
        return [
            d.name
            for d in self._output_root.iterdir()
            if d.is_dir() and (d / _MANIFEST_FILENAME).is_file()
        ]


__all__ = ["LocalArtifactStore"]

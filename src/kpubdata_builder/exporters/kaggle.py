"""Kaggle-compatible dataset directory exporter.

Output structure::

    {output_dir}/{output_path}         ← CSV data file
    {output_dir}/dataset-metadata.json ← Kaggle metadata
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from .base import BaseExporter, ExportResult, ensure_output_dir
from .csv import _format_cell, _resolve_columns


class KaggleExporter(BaseExporter):
    """Exporter that outputs in Kaggle format (CSV + dataset-metadata.json)."""

    @property
    def name(self) -> str:
        return "kaggle"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = ensure_output_dir(output_dir, target.output_path)
        columns = _resolve_columns(artifact)

        buffer = io.StringIO()
        if columns:
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(columns)
            for record in artifact.records:
                writer.writerow([_format_cell(record.get(column)) for column in columns])
        content = buffer.getvalue()

        try:
            fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_name, destination)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            raise ExportError(f"Failed to export Kaggle artifact to {destination}: {exc}") from exc

        metadata_path = destination.parent / "dataset-metadata.json"
        resource = {"path": destination.name, "description": "Main dataset file"}
        metadata: dict[str, Any]

        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ExportError(
                    f"Failed to read existing Kaggle metadata at {metadata_path}: {exc}"
                ) from exc
            if not isinstance(metadata, dict):
                metadata = {}
            resources_obj = metadata.get("resources")
            resources = resources_obj if isinstance(resources_obj, list) else []
            if not any(
                isinstance(entry, dict) and entry.get("path") == resource["path"]
                for entry in resources
            ):
                resources.append(resource)
            metadata["resources"] = resources
        else:
            metadata = {"resources": [resource]}

        # id/title/licenses are authoritative fields; each export updates with current
        # artifact values. stale values in existing file may cause publisher verification
        # failure or incorrect Kaggle dataset upload (#202). preserve other keys.
        metadata["title"] = artifact.metadata.get("title", "Dataset")
        metadata["id"] = artifact.metadata.get("dataset_id", "unknown/dataset")
        # do not guess license. previously silently assigned CC-BY-4.0 if not declared,
        # but this file is authoritative for Kaggle, so assigning it to others' data makes
        # false claims on their behalf. Public License types 2-4 restrict commercial use or
        # modification; guessing would clearly misclassify such data.
        declared_license = artifact.metadata.get("license")
        if not isinstance(declared_license, str) or not declared_license.strip():
            raise ExportError(
                "Kaggle export requires an explicit license: set 'license' on the BuildSpec. "
                "It is written to dataset-metadata.json, which Kaggle treats as authoritative."
            )
        metadata["licenses"] = [{"name": declared_license.strip()}]

        try:
            fd, tmp_meta = tempfile.mkstemp(dir=metadata_path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
                os.replace(tmp_meta, metadata_path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_meta)
                raise
        except OSError as exc:
            raise ExportError(f"Failed to write Kaggle metadata to {metadata_path}: {exc}") from exc

        return ExportResult(
            output_path=destination,
            file_size=destination.stat().st_size,
            format=self.name,
        )

"""JSONL exporter stub implementation.

This module provides baseline exporter to serialize ArtifactDataset records to
newline-delimited JSON (JSONL) format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TextIO

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ._json_safe import json_safe
from ._rows import BATCH_SIZE, write_text_atomically
from .base import BaseExporter, ExportResult, ensure_output_dir


def write_jsonl(artifact: ArtifactDataset, handle: TextIO) -> None:
    """One JSON object per row, streamed from the data source (#873)."""
    for record in artifact.data_source.iter_records(batch_size=BATCH_SIZE):
        # allow_nan=False: NaN/Infinity are non-standard JSON tokens, so fail with
        # ValueError instead of silently recording (#217).
        handle.write(
            json.dumps(json_safe(record), ensure_ascii=False, sort_keys=True, allow_nan=False)
        )
        handle.write("\n")


class JsonlExporter(BaseExporter):
    """exporter that writes records as newline-delimited JSON.

    Example:
        >>> JsonlExporter().name
        'jsonl'
    """

    @property
    def name(self) -> str:
        """returns exporter name."""
        return "jsonl"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """exports standard records to JSONL file.

        Args:
            artifact: record batch to serialize to JSONL.
            target: export target with output path and options.
            output_dir: build-based output directory.

        Returns:
            ExportResult: generated JSONL file metadata.

        Raises:
            ExportError: if file write fails.
        """
        destination = ensure_output_dir(output_dir, target.output_path)
        try:
            write_text_atomically(destination, lambda handle: write_jsonl(artifact, handle))
        except OSError as exc:
            raise ExportError(f"Failed to export JSONL artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )

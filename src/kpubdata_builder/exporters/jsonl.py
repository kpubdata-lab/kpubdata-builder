"""JSONL exporter stub implementation.

This module provides baseline exporter to serialize ArtifactDataset records to
newline-delimited JSON (JSONL) format.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ._json_safe import json_safe
from .base import BaseExporter, ExportResult, ensure_output_dir


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
            fd, tmp_name = tempfile.mkstemp(dir=destination.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    for record in artifact.records:
                        # allow_nan=False: NaN/Infinity are non-standard JSON tokens, so fail
                        # with ValueError instead of silently recording (#217).
                        f.write(
                            json.dumps(
                                json_safe(record),
                                ensure_ascii=False,
                                sort_keys=True,
                                allow_nan=False,
                            )
                        )
                        f.write("\n")
                os.replace(tmp_name, destination)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        except OSError as exc:
            raise ExportError(f"Failed to export JSONL artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )

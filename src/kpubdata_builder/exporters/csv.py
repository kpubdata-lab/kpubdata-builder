"""CSV exporter implementation.

This module provides exporter to serialize ArtifactDataset records to RFC 4180 style
CSV file. Columns follow artifact.schema order if present, else order of first
appearance in records. Values containing commas/quotes/newlines are auto-quoted by
stdlib csv.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import tempfile
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget, JsonValue
from ._json_safe import json_safe
from .base import BaseExporter, ExportResult, ensure_output_dir


def _resolve_columns(artifact: ArtifactDataset) -> list[str]:
    """determines column order for CSV header."""
    columns: dict[str, None] = {}
    if artifact.schema:
        for key in artifact.schema:
            columns.setdefault(key, None)
    for record in artifact.records:
        for key in record:
            columns.setdefault(key, None)
    return list(columns.keys())


# leading characters that spreadsheets interpret as formulas.
# prefix with single quote if cell starts with this character to prevent formula execution
# (CSV injection mitigation, CWE-1236).
_FORMULA_TRIGGER_CHARS = frozenset("=+-@\t\r")


def _format_cell(value: JsonValue) -> str:
    """converts single cell value to CSV string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        text = value
        if text and text[0] in _FORMULA_TRIGGER_CHARS:
            text = "'" + text
        return text
    # date/datetime/Decimal cannot be serialized by json.dumps. due to a single cell
    # entire build would fail with unknown cause (#629 follow-up).
    safe = json_safe(value)
    if isinstance(safe, str):
        return safe
    return json.dumps(safe, ensure_ascii=False, sort_keys=True)


class CsvExporter(BaseExporter):
    """exporter that writes records to CSV.

    Example:
        >>> CsvExporter().name
        'csv'
    """

    @property
    def name(self) -> str:
        """returns exporter name."""
        return "csv"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """exports standard records to CSV file.

        Args:
            artifact: record batch to serialize to CSV.
            target: export target with output path and options.
            output_dir: build-based output directory.

        Returns:
            ExportResult: generated CSV file metadata.

        Raises:
            ExportError: if file write fails.
        """
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
            raise ExportError(f"Failed to export CSV artifact to {destination}: {exc}") from exc

        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )

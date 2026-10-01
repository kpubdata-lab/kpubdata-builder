"""markdown exporter: converts ArtifactDataset to human-readable documentation."""

from __future__ import annotations

import contextlib
import itertools
import os
import tempfile
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ._rows import resolve_columns
from .base import BaseExporter, ExportResult, ensure_output_dir

SAMPLE_ROW_LIMIT = 5


class MarkdownExporter(BaseExporter):
    """exporter that outputs dataset as README-style markdown documentation."""

    @property
    def name(self) -> str:
        """returns exporter name."""
        return "markdown"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """exports artifact to human-readable markdown document."""
        destination = ensure_output_dir(output_dir, target.output_path)
        content = _render_markdown(artifact)
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
            raise ExportError(
                f"Failed to export Markdown artifact to {destination}: {exc}"
            ) from exc
        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )


def _render_markdown(artifact: ArtifactDataset) -> str:
    """assembles each artifact section into markdown document."""
    lines: list[str] = []
    lines.extend(_title_section(artifact))
    lines.extend(_schema_section(artifact))
    lines.extend(_sample_section(artifact))
    lines.extend(_provenance_section(artifact))
    return "\n".join(lines) + "\n"


def _title_section(artifact: ArtifactDataset) -> list[str]:
    """outputs metadata title, description, and record count."""
    title = artifact.metadata.get("title", "Dataset Artifact")
    lines = [f"# {title}", ""]
    description = artifact.metadata.get("description")
    if description:
        lines.extend([description, ""])
    lines.extend([f"- Records: {artifact.data_source.row_count}", ""])
    return lines


def _column_names(artifact: ArtifactDataset) -> list[str]:
    """returns schema column names, or the keys the rows carry if absent."""
    if artifact.schema:
        return list(artifact.schema.keys())
    return resolve_columns(artifact)


def _schema_section(artifact: ArtifactDataset) -> list[str]:
    """outputs schema as markdown table (field | type)."""
    lines = ["## Schema", ""]
    columns = _column_names(artifact)
    if not columns:
        lines.extend(["_No schema available._", ""])
        return lines
    lines.append("| field | type |")
    lines.append("| --- | --- |")
    for name in columns:
        dtype = artifact.schema.get(name, "unknown") if artifact.schema else "unknown"
        lines.append(f"| {name} | {dtype} |")
    lines.append("")
    return lines


def _sample_section(artifact: ArtifactDataset) -> list[str]:
    """outputs up to SAMPLE_ROW_LIMIT records as markdown table."""
    lines = ["## Sample Rows", ""]
    # Only the sample is read: the rest of the rows never leave the data source (#873).
    sample = list(
        itertools.islice(
            artifact.data_source.iter_records(batch_size=SAMPLE_ROW_LIMIT), SAMPLE_ROW_LIMIT
        )
    )
    if not sample:
        lines.extend(["_No records available._", ""])
        return lines
    columns = _column_names(artifact)
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for record in sample:
        cells = [_format_cell(record.get(col)) for col in columns]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _provenance_section(artifact: ArtifactDataset) -> list[str]:
    """outputs provenance items (source names) as bullet list."""
    lines = ["## Provenance", ""]
    if not artifact.provenance:
        lines.extend(["_No provenance recorded._", ""])
        return lines
    for source in artifact.provenance:
        lines.append(f"- {source}")
    lines.append("")
    return lines


def _format_cell(value: object) -> str:
    """safely converts markdown table cell values to strings."""
    if value is None:
        return ""
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")

"""Tests for the Markdown exporter."""

from __future__ import annotations

from pathlib import Path

from kpubdata_builder.artifact import ArtifactDataset
from kpubdata_builder.exporters.markdown import MarkdownExporter
from kpubdata_builder.spec import ExportTarget


def _export(artifact: ArtifactDataset, tmp_path: Path) -> str:
    target = ExportTarget(kind="markdown", output_path="dataset.md")
    result = MarkdownExporter().export(artifact, target, tmp_path)
    assert result.output_path.exists()
    assert result.format == "markdown"
    return result.output_path.read_text(encoding="utf-8")


def test_title_and_description_rendered(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=({"date": "2025-04-01", "temp": 15},),
        schema={"date": "string", "temp": "int"},
        metadata={"title": "날씨 데이터셋", "description": "기상청 API 기반 데이터셋."},
        provenance=("datago.weather",),
    )
    content = _export(artifact, tmp_path)
    assert "# 날씨 데이터셋" in content
    assert "기상청 API 기반 데이터셋." in content


def test_schema_table_rendered(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=({"date": "2025-04-01", "temp": 15},),
        schema={"date": "string", "temp": "int"},
    )
    content = _export(artifact, tmp_path)
    assert "## Schema" in content
    assert "| field | type |" in content
    assert "| date | string |" in content
    assert "| temp | int |" in content


def test_sample_rows_rendered(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=(
            {"date": "2025-04-01", "sky": "맑음"},
            {"date": "2025-04-02", "sky": "흐림"},
        ),
        schema={"date": "string", "sky": "string"},
    )
    content = _export(artifact, tmp_path)
    assert "## Sample Rows" in content
    assert "맑음" in content
    assert "흐림" in content


def test_sample_rows_limited_to_five(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=tuple({"n": i} for i in range(10)),
        schema={"n": "int"},
    )
    content = _export(artifact, tmp_path)
    sample_part = content.split("## Sample Rows")[1].split("## Provenance")[0]
    data_rows = [
        line
        for line in sample_part.splitlines()
        if line.startswith("|") and "---" not in line and "| n |" not in line
    ]
    assert len(data_rows) == 5


def test_provenance_rendered(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=({"a": 1},),
        schema={"a": "int"},
        provenance=("datago.weather", "datago.air_quality"),
    )
    content = _export(artifact, tmp_path)
    assert "## Provenance" in content
    assert "- datago.weather" in content
    assert "- datago.air_quality" in content


def test_schema_falls_back_to_record_keys(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=({"city": "Seoul", "pop": 1000},),
    )
    content = _export(artifact, tmp_path)
    assert "| city | unknown |" in content
    assert "| pop | unknown |" in content


def test_empty_dataset_does_not_break(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records()
    content = _export(artifact, tmp_path)
    assert "# Dataset Artifact" in content
    assert "_No schema available._" in content
    assert "_No records available._" in content
    assert "_No provenance recorded._" in content


def test_cell_with_pipe_is_escaped(tmp_path: Path) -> None:
    artifact = ArtifactDataset.from_records(
        records=({"note": "a|b"},),
        schema={"note": "string"},
    )
    content = _export(artifact, tmp_path)
    assert "a\\|b" in content


def test_cell_with_carriage_return_is_escaped(tmp_path: Path) -> None:
    # #225: \r breaks markdown tables, must be replaced with space.
    artifact = ArtifactDataset.from_records(
        records=({"note": "line1\rline2"},),
        schema={"note": "string"},
    )
    content = _export(artifact, tmp_path)
    # \r is removed/space-substituted so raw CR must not be in output.
    assert "\r" not in content
    # Cell contents joined by space.
    assert "line1 line2" in content

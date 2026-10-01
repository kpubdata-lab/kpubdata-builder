"""Lock down JsonlExporter output rules via test.

Core contract of JSONL is "one line = one record", so regression tests lock
down line count, Unicode preservation, key sorting, empty data policy, and returned metadata.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder import ArtifactDataset
from kpubdata_builder.exporters import JsonlExporter
from kpubdata_builder.spec import ExportTarget, JsonValue


def test_each_record_is_one_json_line(tmp_path: Path) -> None:
    # 2 records means file is exactly 2 lines; each line is independent JSON.
    artifact = ArtifactDataset.from_records(records=({"id": "1"}, {"id": "2"}))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    lines = result.output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == [{"id": "1"}, {"id": "2"}]


def test_unicode_is_preserved_without_ascii_escaping(tmp_path: Path) -> None:
    # Verify Korean is preserved as-is, not escaped as \uXXXX.
    artifact = ArtifactDataset.from_records(records=({"name": "대기오염정보"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_text(encoding="utf-8")
    assert "대기오염정보" in raw
    assert "\\u" not in raw


def test_keys_are_sorted_for_deterministic_output(tmp_path: Path) -> None:
    # Keys sorted regardless of insertion order for deterministic output.
    artifact = ArtifactDataset.from_records(records=({"b": "2", "a": "1"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == '{"a": "1", "b": "2"}\n'


def test_non_empty_output_ends_with_single_trailing_newline(tmp_path: Path) -> None:
    # non-empty output ends with exactly one newline (no trailing blank line).
    artifact = ArtifactDataset.from_records(records=({"id": "1"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")


def test_empty_records_write_empty_file(tmp_path: Path) -> None:
    # empty data policy is recorded as empty file (no content).
    artifact = ArtifactDataset.from_records(records=())
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == ""
    assert result.file_size == 0


def test_returns_metadata_pointing_to_created_file(tmp_path: Path) -> None:
    # Verify returned Path actually points to created file and metadata is accurate.
    artifact = ArtifactDataset.from_records(records=({"id": "1"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.jsonl"
    assert result.output_path.is_file()
    assert result.file_size == result.output_path.stat().st_size
    assert result.format == "jsonl"


def test_non_finite_float_is_rejected(tmp_path: Path) -> None:
    # NaN/Infinity become non-standard JSON tokens, so reject silently without recording,
    # fail with ValueError (same contract as bronze guard) (#217).
    bad_record = cast(dict[str, JsonValue], {"v": float("nan")})
    artifact = ArtifactDataset.from_records(records=(bad_record,))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    with pytest.raises(ValueError, match="Out of range float values"):
        JsonlExporter().export(artifact, target, tmp_path)


def test_non_serializable_value_surfaces_type_error(tmp_path: Path) -> None:
    # Non-JsonValue serializable values (e.g., set) surface as TypeError in json.dumps.
    # Explicitly lock boundary where write failure's OSError is not wrapped as ExportError.
    bad_record = cast(dict[str, JsonValue], {"bad": {1, 2}})
    artifact = ArtifactDataset.from_records(records=(bad_record,))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    with pytest.raises(TypeError):
        JsonlExporter().export(artifact, target, tmp_path)

"""Fix CsvExporter output rules via tests.

CSV must properly quote values containing comma, quote, and newline characters;
column order must be deterministic. Regression tests lock header composition,
cell formatting, empty data policy, and returned metadata.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, CsvExporter
from kpubdata_builder.spec import ExportTarget


def _read_rows(path: Path) -> list[list[str]]:
    # Re-read recorded CSV with csv.reader to verify quoting/escaping.
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def test_writes_header_and_one_row_per_record(tmp_path: Path) -> None:
    # 2 records should be header 1 line + data 2 lines.
    artifact = ArtifactDataset(records=({"id": "1", "name": "a"}, {"id": "2", "name": "b"}))
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["id", "name"], ["1", "a"], ["2", "b"]]


def test_column_order_follows_schema_when_present(tmp_path: Path) -> None:
    # If schema present, header order follows schema key order.
    artifact = ArtifactDataset(
        records=({"b": "2", "a": "1"},),
        schema={"a": "str", "b": "str"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["a", "b"], ["1", "2"]]


def test_column_order_is_first_seen_when_no_schema(tmp_path: Path) -> None:
    # Without schema, follow first appearance order in records, fill missing keys with empty cells.
    artifact = ArtifactDataset(records=({"id": "1"}, {"id": "2", "extra": "x"}))
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["id", "extra"], ["1", ""], ["2", "x"]]


def test_quotes_values_with_comma_quote_and_newline(tmp_path: Path) -> None:
    # Verify values with comma/quote/newline are quoted and round-trip.
    artifact = ArtifactDataset(
        records=({"v": 'a,b "c" \n d'},),
        schema={"v": "str"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["v"], ['a,b "c" \n d']]


def test_formats_special_cell_values(tmp_path: Path) -> None:
    # None as empty cell, bool as lowercase, nested list/dict as deterministic JSON string.
    artifact = ArtifactDataset(
        records=({"nullable": None, "flag": True, "nested": {"b": 2, "a": 1}, "items": [1, 2]},),
        schema={"nullable": "str", "flag": "bool", "nested": "json", "items": "json"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    assert rows[0] == ["nullable", "flag", "nested", "items"]
    assert rows[1] == ["", "true", '{"a": 1, "b": 2}', "[1, 2]"]


def test_preserves_unicode(tmp_path: Path) -> None:
    # Confirm Korean text preserved without corruption.
    artifact = ArtifactDataset(records=({"district": "강남구"},), schema={"district": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_text(encoding="utf-8")
    assert "강남구" in raw


def test_empty_records_without_schema_writes_empty_file(tmp_path: Path) -> None:
    # No schema or records means empty file (size 0) recorded.
    artifact = ArtifactDataset(records=())
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == ""
    assert result.file_size == 0


def test_schema_without_records_writes_header_only(tmp_path: Path) -> None:
    # Schema without records records header only.
    artifact = ArtifactDataset(records=(), schema={"id": "str", "name": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == "id,name\n"


def test_returns_metadata_pointing_to_created_file(tmp_path: Path) -> None:
    # Confirm returned Path points to actually created file and metadata accurate.
    artifact = ArtifactDataset(records=({"id": "1"},), schema={"id": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.csv"
    assert result.output_path.is_file()
    assert result.file_size == result.output_path.stat().st_size
    assert result.format == "csv"


def test_formula_injection_trigger_chars_are_prefixed(tmp_path: Path) -> None:
    # String starting with formula trigger character (=, +, -, @, tab) gets single-quote prefix
    # (CWE-1236). Carriage return (\r) CSV round-trip unstable so
    # separate test_formula_injection_cr_prefix verifies with raw file contents.
    trigger_values = ["=CMD", "+SUM()", "-1+1", "@SUM", "\tTAB"]
    artifact = ArtifactDataset(
        records=tuple({"v": val} for val in trigger_values),
        schema={"v": "str"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    data_rows = rows[1:]  # header excluded
    for i, val in enumerate(trigger_values):
        assert data_rows[i][0] == "'" + val, f"트리거 값 {val!r}에 접두사가 없음"


def test_formula_injection_cr_prefix(tmp_path: Path) -> None:
    # String starting with carriage return (\r) also gets single-quote prefix.
    # csv.reader round-trip handles \r unreliably so verify with raw file contents.
    artifact = ArtifactDataset(records=({"v": "\rCR"},), schema={"v": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_bytes()
    # Single quote prefix before \r results in b"'\r" sequence in file.
    assert b"'\r" in raw, "캐리지리턴 앞에 홑따옴표 접두사가 없음"


def test_formula_injection_normal_strings_unchanged(tmp_path: Path) -> None:
    # Normal string not starting with trigger char must remain unchanged.
    normal_values = ["hello", "world", "1234", "", "한글", "abc=def"]
    artifact = ArtifactDataset(
        records=tuple({"v": val} for val in normal_values),
        schema={"v": "str"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    data_rows = rows[1:]
    for i, val in enumerate(normal_values):
        assert data_rows[i][0] == val, f"일반 값 {val!r}이 변경됨"


def test_formula_injection_numeric_values_unchanged(tmp_path: Path) -> None:
    # numbers (int/float) not checked for trigger chars and converted via str().
    artifact = ArtifactDataset(
        records=({"i": -1, "f": -3.14},),
        schema={"i": "int", "f": "float"},
    )
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    result = CsvExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    assert rows[1] == ["-1", "-3.14"]


def test_registry_exposes_csv_exporter() -> None:
    # Confirm CSV exporter registered in registry with kind string "csv".
    assert isinstance(EXPORTER_REGISTRY["csv"], CsvExporter)


def test_wraps_io_failure_in_export_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Confirm file write failure wrapped as ExportError.
    import os

    artifact = ArtifactDataset(records=({"id": "1"},), schema={"id": "str"})
    target = ExportTarget(kind="csv", output_path="out/data.csv")

    def raise_on_replace(src: str, dst: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", raise_on_replace)

    with pytest.raises(ExportError):
        CsvExporter().export(artifact, target, tmp_path)

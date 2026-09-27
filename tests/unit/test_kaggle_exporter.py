"""Lock down KaggleExporter output rules via test.

CSV body must follow schema-first column order like CsvExporter, and
dataset-metadata.json must contain title/id/licenses/resources. License
override, existing metadata merge, and I/O failure policy are locked by regression tests.
"""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, KaggleExporter
from kpubdata_builder.spec import ExportTarget


def _read_rows(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def _read_metadata(directory: Path) -> dict[str, object]:
    raw = (directory / "dataset-metadata.json").read_text(encoding="utf-8")
    return json.loads(raw)


def test_writes_csv_following_schema_and_valid_metadata(tmp_path: Path) -> None:
    # CSV header follows schema order; metadata json must be valid.
    artifact = ArtifactDataset(
        records=({"b": "2", "a": "1"}, {"a": "3", "b": "4"}),
        schema={"a": "str", "b": "str"},
        metadata={"title": "Air Quality", "dataset_id": "kpub/air", "license": "CC-BY-4.0"},
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["a", "b"], ["1", "2"], ["3", "4"]]

    metadata = _read_metadata(result.output_path.parent)
    assert metadata["title"] == "Air Quality"
    assert metadata["id"] == "kpub/air"
    assert metadata["licenses"] == [{"name": "CC-BY-4.0"}]
    assert metadata["resources"] == [{"path": "data.csv", "description": "Main dataset file"}]


def test_empty_records_with_schema_writes_header_only(tmp_path: Path) -> None:
    # If schema exists but records absent, write header line only.
    artifact = ArtifactDataset(
        records=(), schema={"id": "str", "name": "str"}, metadata={"license": "CC-BY-4.0"}
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == "id,name\n"


def test_license_override_from_metadata(tmp_path: Path) -> None:
    # If metadata.license exists, its value is reflected in licenses name.
    artifact = ArtifactDataset(
        records=({"id": "1"},),
        schema={"id": "str"},
        metadata={"license": "CC0-1.0", "title": "X", "dataset_id": "kpub/x"},
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    metadata = _read_metadata(result.output_path.parent)
    assert metadata["licenses"] == [{"name": "CC0-1.0"}]


def test_formula_injection_trigger_chars_prefixed_in_kaggle(tmp_path: Path) -> None:
    # KaggleExporter shares _format_cell, so formula-trigger values must have prefix.
    artifact = ArtifactDataset(
        records=({"cmd": '=HYPERLINK("evil.com")'},),
        schema={"cmd": "str"},
        metadata={"title": "T", "dataset_id": "kpub/t", "license": "CC-BY-4.0"},
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    assert rows[1][0] == '\'=HYPERLINK("evil.com")'


def test_registry_exposes_kaggle_exporter() -> None:
    # Verify Kaggle exporter is registered with kind string "kaggle" in registry.
    assert isinstance(EXPORTER_REGISTRY["kaggle"], KaggleExporter)


def test_merges_resource_into_existing_metadata(tmp_path: Path) -> None:
    # Exporting twice to same directory accumulates both paths in resources.
    target_one = ExportTarget(kind="kaggle", output_path="out/first.csv")
    target_two = ExportTarget(kind="kaggle", output_path="out/second.csv")
    artifact = ArtifactDataset(
        records=({"id": "1"},),
        schema={"id": "str"},
        metadata={"title": "First", "dataset_id": "kpub/first", "license": "CC-BY-4.0"},
    )

    first = KaggleExporter().export(artifact, target_one, tmp_path)
    KaggleExporter().export(
        ArtifactDataset(
            records=({"id": "2"},),
            schema={"id": "str"},
            metadata={"title": "Second", "dataset_id": "kpub/second", "license": "CC-BY-4.0"},
        ),
        target_two,
        tmp_path,
    )

    metadata = _read_metadata(first.output_path.parent)
    # Authoritative fields (title/id/licenses) update to latest export; resources accumulate (#202).
    assert metadata["title"] == "Second"
    assert metadata["id"] == "kpub/second"
    paths = {entry["path"] for entry in metadata["resources"]}  # type: ignore[index, union-attr]
    assert paths == {"first.csv", "second.csv"}


def test_reexport_refreshes_stale_top_level_metadata(tmp_path: Path) -> None:
    # After config change, re-running to same file must update stale id/title/licenses (#202).
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    KaggleExporter().export(
        ArtifactDataset(
            records=({"id": "1"},),
            schema={"id": "str"},
            metadata={"title": "Old", "dataset_id": "kpub/old", "license": "CC-BY-4.0"},
        ),
        target,
        tmp_path,
    )
    result = KaggleExporter().export(
        ArtifactDataset(
            records=({"id": "1"},),
            schema={"id": "str"},
            metadata={"title": "New", "dataset_id": "kpub/new", "license": "CC0-1.0"},
        ),
        target,
        tmp_path,
    )

    metadata = _read_metadata(result.output_path.parent)
    assert metadata["title"] == "New"
    assert metadata["id"] == "kpub/new"
    assert metadata["licenses"] == [{"name": "CC0-1.0"}]


def test_wraps_io_failure_in_export_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Verify file write failures are wrapped as ExportError.
    # Declare license — without it, license check fails before I/O is even touched,
    # so ExportError is raised, test passes for different reason than name suggests.
    artifact = ArtifactDataset(
        records=({"id": "1"},), schema={"id": "str"}, metadata={"license": "CC-BY-4.0"}
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    def raise_on_replace(src: str, dst: str) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "replace", raise_on_replace)

    with pytest.raises(ExportError):
        KaggleExporter().export(artifact, target, tmp_path)


class TestTheLicenseIsNeverGuessed:
    """``dataset-metadata.json`` is the file Kaggle reads as canonical.

    Without declaration, silently applying ``CC-BY-4.0`` was making false claims on others' data.
    For data like Korean Copyright Act Type 2-4 with commercial/derivative restrictions, that's
    clear mislabeling.
    """

    def _artifact(self, **metadata: object) -> ArtifactDataset:
        return ArtifactDataset(
            records=({"a": "1"},),
            schema={"a": "str"},
            metadata={"title": "T", "dataset_id": "kpub/t", **metadata},
        )

    def test_an_undeclared_license_refuses_the_export(self, tmp_path: Path) -> None:
        target = ExportTarget(kind="kaggle", output_path="out/data.csv")

        with pytest.raises(ExportError, match="requires an explicit license"):
            KaggleExporter().export(self._artifact(), target, tmp_path)

    def test_a_blank_license_is_not_a_declaration(self, tmp_path: Path) -> None:
        target = ExportTarget(kind="kaggle", output_path="out/data.csv")

        with pytest.raises(ExportError, match="requires an explicit license"):
            KaggleExporter().export(self._artifact(license="   "), target, tmp_path)

    def test_a_declared_license_is_written_verbatim(self, tmp_path: Path) -> None:
        target = ExportTarget(kind="kaggle", output_path="out/data.csv")

        result = KaggleExporter().export(self._artifact(license="KOGL-Type-1"), target, tmp_path)

        metadata = _read_metadata(result.output_path.parent)
        assert metadata["licenses"] == [{"name": "KOGL-Type-1"}]

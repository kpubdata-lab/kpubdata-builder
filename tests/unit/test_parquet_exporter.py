"""Lock ParquetExporter output rules via tests.

Parquet is a binary columnar format, so when re-read with polars, records
are preserved, column types are maintained, empty data policy, and returned metadata are locked via
regression tests.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, ParquetExporter
from kpubdata_builder.spec import ExportTarget


def test_records_round_trip_through_parquet(tmp_path: Path) -> None:
    # Record 2 records, re-read with read_parquet, and verify they match the originals.
    records = ({"id": "1", "amount": 1000}, {"id": "2", "amount": 2500})
    artifact = ArtifactDataset(records=records)
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.to_dicts() == [dict(record) for record in records]


def test_column_types_are_preserved(tmp_path: Path) -> None:
    # Verify that int columns remain as integer type after round-trip.
    artifact = ArtifactDataset(records=({"id": "1", "amount": 1000},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.schema["amount"] == pl.Int64
    assert frame.schema["id"] == pl.Utf8


def test_preserves_unicode(tmp_path: Path) -> None:
    # Verify that Korean values are preserved after round-trip.
    artifact = ArtifactDataset(records=({"district": "강남구"},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    assert pl.read_parquet(result.output_path).to_dicts() == [{"district": "강남구"}]


def test_empty_records_with_schema_keeps_columns(tmp_path: Path) -> None:
    # Empty data with schema yields 0 rows but preserves column names.
    artifact = ArtifactDataset(records=(), schema={"id": "str", "amount": "int"})
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.height == 0
    assert frame.columns == ["id", "amount"]


def test_empty_records_without_schema_writes_readable_empty_file(tmp_path: Path) -> None:
    # No schema and no records result in a readable Parquet file with 0 rows and 0 columns.
    artifact = ArtifactDataset(records=())
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    frame = pl.read_parquet(result.output_path)
    assert frame.shape == (0, 0)


def test_returns_metadata_pointing_to_created_file(tmp_path: Path) -> None:
    # Verify that returned Path points to the actually-created file and metadata is accurate.
    artifact = ArtifactDataset(records=({"id": "1"},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    result = ParquetExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.parquet"
    assert result.output_path.is_file()
    assert result.file_size == result.output_path.stat().st_size
    assert result.format == "parquet"


def test_registry_exposes_parquet_exporter() -> None:
    # Verify Parquet exporter is registered in the registry with kind string "parquet".
    assert isinstance(EXPORTER_REGISTRY["parquet"], ParquetExporter)


def test_wraps_write_failure_in_export_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Verify Parquet write failure is wrapped in ExportError.
    artifact = ArtifactDataset(records=({"id": "1"},))
    target = ExportTarget(kind="parquet", output_path="out/data.parquet")

    def raise_os_error(self: pl.DataFrame, *args: object, **kwargs: object) -> None:
        del self, args, kwargs
        raise OSError("disk full")

    monkeypatch.setattr(pl.DataFrame, "write_parquet", raise_os_error)

    with pytest.raises(ExportError):
        ParquetExporter().export(artifact, target, tmp_path)

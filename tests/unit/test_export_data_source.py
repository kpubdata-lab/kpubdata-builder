"""Exporters read a replayable data source, not a tuple of records (#873)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import polars as pl
import pytest

from kpubdata_builder.artifact import ArtifactDataset, ArtifactDataSource, RecordsSource
from kpubdata_builder.exporters import BaseExporter, ExportResult, registry
from kpubdata_builder.exporters.base import ensure_output_dir
from kpubdata_builder.pipeline.export import TableSource, export_gold_package
from kpubdata_builder.spec import ExportTarget
from kpubdata_builder.stages.gold import ExportPlan, GoldPackage
from kpubdata_builder.tabular.duckdb_load import TableHandle
from tests.support.polars_bridge import handle_from_frame

_FRAME = pl.DataFrame(
    {"id": [str(i) for i in range(2500)], "v": list(range(2500)), "note": ["=1+1"] * 2500}
)
_ALL = (
    ExportTarget(kind="csv", output_path="out/data.csv"),
    ExportTarget(kind="jsonl", output_path="out/data.jsonl"),
    ExportTarget(kind="parquet", output_path="out/data.parquet"),
    ExportTarget(kind="markdown", output_path="out/data.md"),
    ExportTarget(kind="huggingface", output_path="hf"),
)


def _package(tmp_path: Path, targets: tuple[ExportTarget, ...]) -> tuple[GoldPackage, Path]:
    table = handle_from_frame(_FRAME, workdir=tmp_path / "work")
    table.cache.clear()  # the bridge's frame is not what an export reads
    gold = tmp_path / "gold"
    gold.mkdir()
    table_path = gold / "table.parquet"
    table.write_parquet(table_path)
    package = GoldPackage(
        dataset_name="d",
        table=table,
        export_plan=ExportPlan(targets=targets),
        source_silver="s",
        metadata={"title": "T", "license": "CC-BY-4.0", "dataset_id": "o/d"},
    )
    return package, table_path


def test_sources_are_replayable(tmp_path: Path) -> None:
    table = handle_from_frame(_FRAME, workdir=tmp_path)
    for source in (TableSource(table), RecordsSource(tuple(_FRAME.to_dicts()))):
        assert isinstance(source, ArtifactDataSource)
        first = list(source.iter_records(batch_size=7))
        second = list(source.iter_records())
        assert first == second == _FRAME.to_dicts()
        assert source.row_count == 2500


def test_the_canonical_export_path_never_materializes_the_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: no Polars frame, no whole-table read — rows come in batches."""
    package, table_path = _package(tmp_path, _ALL)
    batch_sizes: list[int] = []
    original = TableHandle.iter_rows

    def spy(self: TableHandle, *, batch_size: int = 1000) -> Iterator[dict[str, object]]:
        batch_sizes.append(batch_size)
        return original(self, batch_size=batch_size)

    def refuse(*_: object, **__: object) -> Any:
        raise AssertionError("the export read the whole table into a frame")

    monkeypatch.setattr(TableHandle, "iter_rows", spy)
    monkeypatch.setattr(TableHandle, "rows", refuse)

    paths = export_gold_package(package, output_dir=tmp_path / "gold", table_path=table_path)

    assert len(paths) == 5
    assert batch_sizes and max(batch_sizes) <= 1000
    out = tmp_path / "gold" / "out"
    assert len((out / "data.jsonl").read_text(encoding="utf-8").splitlines()) == 2500
    csv_lines = (out / "data.csv").read_text(encoding="utf-8").splitlines()
    assert csv_lines[0] == "id,v,note" and len(csv_lines) == 2501
    # The CSV formula guard still applies on the streamed path.
    assert csv_lines[1] == "0,0,'=1+1"
    assert "- Records: 2500" in (out / "data.md").read_text(encoding="utf-8")
    infos = json.loads((tmp_path / "gold" / "hf" / "dataset_infos.json").read_text("utf-8"))
    assert infos["num_examples"] == 2500


def test_parquet_exports_copy_the_gold_file(tmp_path: Path) -> None:
    targets = (
        ExportTarget(kind="parquet", output_path="out/data.parquet"),
        ExportTarget(kind="huggingface", output_path="hf"),
    )
    package, table_path = _package(tmp_path, targets)

    export_gold_package(package, output_dir=tmp_path / "gold", table_path=table_path)

    written = table_path.read_bytes()
    assert (tmp_path / "gold" / "out" / "data.parquet").read_bytes() == written
    shard = tmp_path / "gold" / "hf" / "data" / "train-00000-of-00001.parquet"
    assert shard.read_bytes() == written


def test_a_failed_export_leaves_no_partial_file(tmp_path: Path) -> None:
    """Atomic: a row that cannot be written (NaN in JSONL) leaves nothing behind."""
    artifact = ArtifactDataset.from_records([{"v": 1.0}, {"v": float("nan")}])
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    with pytest.raises(ValueError):
        registry.get_exporter("jsonl").export(artifact, target, tmp_path)

    assert list((tmp_path / "out").iterdir()) == []


class _TwoPassExporter(BaseExporter):
    """An external plugin: counts the rows, then writes them — two reads of one source."""

    @property
    def name(self) -> str:
        return "twopass"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = ensure_output_dir(output_dir, target.output_path)
        count = sum(1 for _ in artifact.data_source.iter_records())
        ids = [str(r["id"]) for r in artifact.data_source.iter_records(batch_size=100)]
        destination.write_text(f"{count}\n{ids[0]}..{ids[-1]}\n", encoding="utf-8")
        return ExportResult(destination, destination.stat().st_size, self.name)


def test_an_entry_point_plugin_reads_the_source_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(registry, "_EXPORTER_FACTORIES", dict(registry._EXPORTER_FACTORIES))
    entry = SimpleNamespace(name="twopass", load=lambda: _TwoPassExporter)
    monkeypatch.setattr(registry, "entry_points", lambda group: [entry])

    assert registry.load_entry_point_exporters() == ["twopass"]
    package, table_path = _package(
        tmp_path, (ExportTarget(kind="twopass", output_path="out/plugin.txt"),)
    )
    (path,) = export_gold_package(package, output_dir=tmp_path / "gold", table_path=table_path)

    assert path.read_text(encoding="utf-8") == "2500\n0..2499\n"

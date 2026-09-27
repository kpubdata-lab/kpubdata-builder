"""Verify HuggingFaceExporter (#9) layout, card, and metadata generation."""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, HuggingFaceExporter
from kpubdata_builder.spec import ExportTarget


def _artifact() -> ArtifactDataset:
    return ArtifactDataset(
        records=({"id": "1", "name": "강남구"}, {"id": "2", "name": "서초구"}),
        schema={"id": "str", "name": "str"},
        metadata={
            "title": "아파트 실거래가",
            "description": "서울 아파트 실거래가",
            "license": "cc-by-4.0",
        },
        provenance=("datago.apt_trade",),
    )


def test_creates_hf_directory_layout(tmp_path: Path) -> None:
    # Generate HF standard layout of data/ + README.md + dataset_infos.json.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    assert result.output_path == tmp_path / "hf/apt_trade"
    assert (result.output_path / "data").is_dir()
    assert (result.output_path / "README.md").is_file()
    assert (result.output_path / "dataset_infos.json").is_file()
    assert result.format == "huggingface"
    assert result.file_size > 0


def test_default_format_is_parquet_and_round_trips(tmp_path: Path) -> None:
    # Default format is parquet; verify data round-trip preservation.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    data_file = result.output_path / "data" / "train-00000-of-00001.parquet"
    assert data_file.is_file()
    assert pl.read_parquet(data_file).to_dicts() == [
        {"id": "1", "name": "강남구"},
        {"id": "2", "name": "서초구"},
    ]


def test_jsonl_format_option(tmp_path: Path) -> None:
    # With format=jsonl option, record jsonl shards as one record per line.
    target = ExportTarget(
        kind="huggingface", output_path="hf/apt_trade", options={"format": "jsonl"}
    )

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    data_file = result.output_path / "data" / "train-00000-of-00001.jsonl"
    lines = data_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"id": "1", "name": "강남구"}


def test_readme_has_yaml_front_matter_and_unicode(tmp_path: Path) -> None:
    # Verify README.md starts with YAML front matter and Korean is preserved.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    text = (result.output_path / "README.md").read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert text.count("---") >= 2  # front matter start/end
    assert "license: cc-by-4.0" in text
    assert "# 아파트 실거래가" in text
    assert "- datago.apt_trade" in text


def test_dataset_infos_is_valid_json_with_features(tmp_path: Path) -> None:
    # Verify dataset_infos.json is valid JSON and contains features/num_examples.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    result = HuggingFaceExporter().export(_artifact(), target, tmp_path)

    infos = json.loads((result.output_path / "dataset_infos.json").read_text(encoding="utf-8"))
    assert infos["features"] == {"id": "str", "name": "str"}
    assert infos["num_examples"] == 2
    assert infos["provenance"] == ["datago.apt_trade"]


def test_rejects_unsupported_format(tmp_path: Path) -> None:
    # Reject unsupported formats with ExportError.
    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade", options={"format": "xml"})

    with pytest.raises(ExportError, match="format"):
        HuggingFaceExporter().export(_artifact(), target, tmp_path)


def test_reexport_with_format_change_removes_stale_shards(tmp_path: Path) -> None:
    # When re-running with different format to same output_path, previous shard files must not
    # remain (#203).
    parquet_target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")
    jsonl_target = ExportTarget(
        kind="huggingface", output_path="hf/apt_trade", options={"format": "jsonl"}
    )

    HuggingFaceExporter().export(_artifact(), parquet_target, tmp_path)
    result = HuggingFaceExporter().export(_artifact(), jsonl_target, tmp_path)

    data_dir = result.output_path / "data"
    shards = sorted(p.name for p in data_dir.iterdir())
    # parquet shards should disappear; only jsonl shards remain.
    assert shards == ["train-00000-of-00001.jsonl"]


def test_jsonl_format_rejects_non_finite_float(tmp_path: Path) -> None:
    # NaN/Infinity in jsonl shards become non-standard JSON tokens, so fail with ValueError
    # (same contract as bronze guard) (#217).
    artifact = ArtifactDataset(records=({"v": float("inf")},))
    target = ExportTarget(
        kind="huggingface", output_path="hf/apt_trade", options={"format": "jsonl"}
    )

    with pytest.raises(ValueError, match="Out of range float values"):
        HuggingFaceExporter().export(artifact, target, tmp_path)


def test_registry_exposes_huggingface_exporter() -> None:
    # Verify HF exporter is registered with kind "huggingface" in registry.
    assert isinstance(EXPORTER_REGISTRY["huggingface"], HuggingFaceExporter)


def test_failing_export_leaves_no_temp_dir_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Temporary .hf_tmp_* directories must not remain even if TabularError occurs (#222).
    import kpubdata_builder.exporters.huggingface as hf_module
    from kpubdata_builder.errors import TabularError

    target = ExportTarget(kind="huggingface", output_path="hf/apt_trade")

    def raise_tabular_error(records: object) -> None:
        raise TabularError("혼합 타입 컬럼")

    # Must replace records_to_dataframe imported directly from huggingface module for patch to
    # apply.
    monkeypatch.setattr(hf_module, "records_to_dataframe", raise_tabular_error)

    with pytest.raises(ExportError):
        HuggingFaceExporter().export(_artifact(), target, tmp_path)

    # Temporary directories (.hf_tmp_*) must not remain.
    hf_parent = tmp_path / "hf"
    if hf_parent.exists():
        leaked = [p for p in hf_parent.iterdir() if p.name.startswith(".hf_tmp_")]
        assert leaked == [], f"Temp dirs leaked: {leaked}"


class TestJsonlRecordsGoThroughJsonSafe:
    """#629 fix applied only to jsonl exporter and was missing here.

    Gold table comes from Polars, so when ``casts: {deal_date: date}`` is declared
    records contain ``date``/``Decimal`` objects as-is. So the same spec
    works with ``kind: jsonl`` but dies with TypeError on ``kind: huggingface``.
    """

    def _artifact(self) -> ArtifactDataset:
        import datetime
        from decimal import Decimal

        return ArtifactDataset(
            records=({"deal_date": datetime.date(2024, 3, 1), "price": Decimal("12345.67")},),
            schema={"deal_date": "date", "price": "decimal"},
            metadata={"title": "T", "dataset_id": "kpub/t", "license": "CC-BY-4.0"},
        )

    def test_dates_and_decimals_are_written(self, tmp_path: Path) -> None:
        target = ExportTarget(kind="huggingface", output_path="out/hf", options={"format": "jsonl"})

        result = HuggingFaceExporter().export(self._artifact(), target, tmp_path)

        data_files = list(result.output_path.rglob("*.jsonl"))
        assert len(data_files) == 1
        record = json.loads(data_files[0].read_text(encoding="utf-8").strip())
        assert record["deal_date"] == "2024-03-01"
        # Decimal remains string — changing to float alters amount decimal places
        # silently.
        assert record["price"] == "12345.67"

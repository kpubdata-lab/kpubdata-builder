"""Gold stage (#47): SilverDataset → export-ready GoldPackage + persist verification."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.spec import ExportTarget, JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.gold import (
    ExportPlan,
    GoldPackage,
    build_gold_package,
    persist_gold_package,
)
from kpubdata_builder.stages.silver import SilverDataset, build_silver_dataset


def _silver(records: tuple[Mapping[str, JsonValue], ...]) -> SilverDataset:
    bronze = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=tuple(dict(record) for record in records),
        fetched_at=utc_now(),
    )
    return build_silver_dataset(bronze)


class TestBuildGoldPackage:
    def test_packages_silver_table_with_export_plan(self) -> None:
        silver = _silver(({"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}))
        exports = (ExportTarget(kind="jsonl", output_path="data.jsonl"),)

        package = build_gold_package(silver, dataset_name="apt_trade", exports=exports)

        assert isinstance(package, GoldPackage)
        assert package.dataset_name == "apt_trade"
        assert package.table.to_dicts() == silver.table.to_dicts()
        assert isinstance(package.export_plan, ExportPlan)
        assert package.export_plan.targets == exports
        assert package.source_silver == "datago.apt_trade"

    def test_empty_export_plan_when_no_targets(self) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="d")

        assert package.export_plan.targets == ()


class TestPersistGoldPackage:
    def test_writes_parquet_and_package_json(self, tmp_path: Path) -> None:
        silver = _silver(({"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}))
        package = build_gold_package(
            silver,
            dataset_name="apt_trade",
            exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        )

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        assert result.table_path.exists()
        assert result.package_path.exists()
        assert pl.read_parquet(result.table_path).to_dicts() == package.table.to_dicts()

        meta = cast(
            dict[str, JsonValue], json.loads(result.package_path.read_text(encoding="utf-8"))
        )
        assert meta["dataset_name"] == "apt_trade"
        assert meta["row_count"] == 2
        export_plan = cast(dict[str, JsonValue], meta["export_plan"])
        targets = cast(list[dict[str, JsonValue]], export_plan["targets"])
        assert targets[0]["kind"] == "jsonl"
        assert meta["source_silver"] == "datago.apt_trade"

    def test_rejects_non_finite_float_in_metadata(self, tmp_path: Path) -> None:
        # NaN/Infinity in export options become non-standard JSON tokens, so don't quietly record
        # them; fail with ValueError (same contract as bronze guard) (#217).
        package = build_gold_package(
            _silver(({"id": "1"},)),
            dataset_name="apt_trade",
            exports=(
                ExportTarget(
                    kind="jsonl",
                    output_path="data.jsonl",
                    options={"threshold": float("nan")},
                ),
            ),
        )

        with pytest.raises(ValueError, match="Out of range float values"):
            _ = persist_gold_package(package, output_root=tmp_path, run_id="run1")

    def test_rejects_unsafe_dataset_name(self, tmp_path: Path) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="../escape")

        with pytest.raises(ValueError, match="dataset_name"):
            _ = persist_gold_package(package, output_root=tmp_path, run_id="run1")

    def test_rejects_unsafe_run_id(self, tmp_path: Path) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="apt_trade")

        with pytest.raises(ValueError, match="run_id"):
            _ = persist_gold_package(package, output_root=tmp_path, run_id="../escape")


class TestBuildGoldPackageWithSplits:
    def test_builds_splits_when_spec_provided(self) -> None:
        silver = _silver(
            (
                {"id": "1", "amount": 1000, "year": "2024"},
                {"id": "2", "amount": 2500, "year": "2025"},
                {"id": "3", "amount": 1500, "year": "2024"},
            )
        )
        from kpubdata_builder.spec import SplitSpec

        splits_spec = SplitSpec(mode="key", key="year")

        package = build_gold_package(silver, dataset_name="apt_trade", splits_spec=splits_spec)

        assert package.splits is not None
        assert set(package.splits.keys()) == {"2024", "2025"}
        assert len(package.splits["2024"]) == 2
        assert len(package.splits["2025"]) == 1

    def test_no_splits_when_spec_none(self) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="d")

        assert package.splits is None


class TestPersistGoldPackageWithSplits:
    def test_writes_splits_to_subdirectory(self, tmp_path: Path) -> None:
        silver = _silver(
            (
                {"id": "1", "amount": 1000, "year": "2024"},
                {"id": "2", "amount": 2500, "year": "2025"},
            )
        )
        from kpubdata_builder.spec import SplitSpec

        splits_spec = SplitSpec(mode="key", key="year")
        package = build_gold_package(silver, dataset_name="apt_trade", splits_spec=splits_spec)

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        assert result.splits_paths
        assert "2024" in result.splits_paths
        assert "2025" in result.splits_paths
        assert result.splits_paths["2024"].exists()
        assert result.splits_paths["2025"].exists()

        # Read split files to verify content.
        split_2024 = pl.read_parquet(result.splits_paths["2024"])
        assert len(split_2024) == 1
        assert split_2024["year"][0] == "2024"

    def test_splits_metadata_included_in_package_json(self, tmp_path: Path) -> None:
        silver = _silver(({"id": "1", "year": "2024"}, {"id": "2", "year": "2025"}))
        from kpubdata_builder.spec import SplitSpec

        splits_spec = SplitSpec(mode="key", key="year")
        package = build_gold_package(silver, dataset_name="apt_trade", splits_spec=splits_spec)

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        meta = cast(
            dict[str, JsonValue], json.loads(result.package_path.read_text(encoding="utf-8"))
        )
        assert meta["splits"] is not None
        splits_meta = cast(dict[str, int], meta["splits"])
        assert splits_meta["2024"] == 1
        assert splits_meta["2025"] == 1

    def test_no_splits_directory_when_splits_none(self, tmp_path: Path) -> None:
        package = build_gold_package(_silver(({"id": "1"},)), dataset_name="apt_trade")

        result = persist_gold_package(package, output_root=tmp_path, run_id="run1")

        splits_dir = result.gold_dir / "splits"
        assert not splits_dir.exists()
        assert not result.splits_paths

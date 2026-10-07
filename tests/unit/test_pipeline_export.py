from __future__ import annotations

from pathlib import Path

import polars as pl

from kpubdata_builder.pipeline.export import export_gold_package
from kpubdata_builder.spec import ExportTarget
from kpubdata_builder.stages.gold import ExportPlan, GoldPackage
from tests.support.polars_bridge import handle_from_frame


def test_export_gold_package_executes_registered_targets(tmp_path: Path) -> None:
    package = GoldPackage(
        dataset_name="apt_trade",
        table=handle_from_frame(pl.DataFrame([{"id": "1", "amount": 1000}]), workdir=tmp_path),
        export_plan=ExportPlan(
            targets=(ExportTarget(kind="jsonl", output_path="exports/data.jsonl"),)
        ),
        source_silver="datago.apt_trade",
        metadata={"title": "Apartment Trades"},
    )

    paths = export_gold_package(package, output_dir=tmp_path)

    output_path = tmp_path / "exports" / "data.jsonl"
    assert paths == (output_path,)
    assert output_path.read_text(encoding="utf-8") == '{"amount": 1000, "id": "1"}\n'

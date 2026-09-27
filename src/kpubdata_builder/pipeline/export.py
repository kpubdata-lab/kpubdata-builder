"""Execute GoldPackage export plan."""

from __future__ import annotations

from pathlib import Path

from ..artifact import ArtifactDataset
from ..exporters import get_exporter
from ..spec import JsonValue
from ..stages.gold import GoldPackage


def export_gold_package(package: GoldPackage, *, output_dir: Path) -> tuple[Path, ...]:
    """Record GoldPackage export targets to file/directory outputs."""
    artifact = ArtifactDataset(
        records=tuple(_json_record(row) for row in package.table.to_dicts()),
        schema={name: str(dtype) for name, dtype in package.table.schema.items()},
        metadata=package.metadata,
        provenance=package.source_refs if package.source_refs else (package.source_silver,),
        statistics={"row_count": package.table.height},
    )
    return tuple(
        get_exporter(target.kind).export(artifact, target, output_dir).output_path
        for target in package.export_plan.targets
    )


def _json_record(row: dict[str, JsonValue]) -> dict[str, JsonValue]:
    return dict(row)

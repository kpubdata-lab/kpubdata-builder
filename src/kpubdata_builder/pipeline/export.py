"""Execute GoldPackage export plan."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..artifact import ArtifactDataset
from ..exporters import get_exporter
from ..spec import JsonValue
from ..stages.gold import GoldPackage
from ..tabular.duckdb_load import TableHandle


@dataclass(frozen=True)
class TableSource:
    """A Gold table as an artifact's data source (#873).

    Rows are read from DuckDB in batches, in the table's order, every time they are
    asked for — never collected into one tuple. ``parquet_path`` is the Gold
    ``table.parquet`` already written from this table, which a Parquet exporter copies.
    """

    table: TableHandle
    parquet_path: Path | None = None

    @property
    def row_count(self) -> int:
        return self.table.height

    @property
    def columns(self) -> tuple[str, ...]:
        """Every row has exactly these keys, so exporters need not scan for them."""
        return self.table.columns

    def iter_records(self, *, batch_size: int = 1000) -> Iterator[dict[str, JsonValue]]:
        return cast(Iterator[dict[str, JsonValue]], self.table.iter_rows(batch_size=batch_size))


def export_gold_package(
    package: GoldPackage, *, output_dir: Path, table_path: Path | None = None
) -> tuple[Path, ...]:
    """Record GoldPackage export targets to file/directory outputs.

    ``table_path`` is the Gold ``table.parquet`` persisted from ``package.table``; an
    exporter writing Parquet copies it rather than reading the rows again.
    """
    table = package.table
    artifact = ArtifactDataset(
        data_source=TableSource(
            table, table_path if table_path is not None and table_path.is_file() else None
        ),
        schema=dict(zip(table.columns, table.dtypes, strict=True)),
        metadata=package.metadata,
        provenance=package.source_refs if package.source_refs else (package.source_silver,),
        statistics={"row_count": table.height},
    )
    return tuple(
        get_exporter(target.kind).export(artifact, target, output_dir).output_path
        for target in package.export_plan.targets
    )


__all__ = ["TableSource", "export_gold_package"]

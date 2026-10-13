"""Execute GoldPackage export plan."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..exporters import get_exporter
from ..exporters.base import export_layout_conflicts
from ..spec import ExportTarget, JsonValue
from ..stages._path_safety import _strip_windows_extended_prefix, safe_output_path
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


def _check_export_layout(targets: tuple[ExportTarget, ...], output_dir: Path) -> None:
    """Refuse the whole export plan before writing if a target hits Gold or another (#1320).

    ``validate_spec`` compares the spec's paths; this compares where they resolve on
    disk, so a symlink in the Gold directory cannot lead one export onto
    ``table.parquet`` or onto another export's output.

    Raises:
        PathTraversalError: if a target resolves outside ``output_dir``.
        ExportError: if a target resolves to ``output_dir`` itself, a Gold artifact, or
            an entry another target also writes.
    """
    root = _strip_windows_extended_prefix(output_dir.resolve())
    layouts: list[tuple[int, str, tuple[str, ...]]] = []
    for index, target in enumerate(targets):
        destination = safe_output_path(output_dir, target.output_path)
        parts = _strip_windows_extended_prefix(destination.resolve()).relative_to(root).parts
        if not parts:
            raise ExportError(
                f"exports[{index}].output_path {target.output_path!r} resolves to the Gold "
                "directory itself"
            )
        layouts.append((index, target.kind, parts))
    conflicts = export_layout_conflicts(layouts)
    if conflicts:
        raise ExportError("; ".join(message for _index, _code, message in conflicts))


def export_gold_package(
    package: GoldPackage, *, output_dir: Path, table_path: Path | None = None
) -> tuple[Path, ...]:
    """Record GoldPackage export targets to file/directory outputs.

    ``table_path`` is the Gold ``table.parquet`` persisted from ``package.table``; an
    exporter writing Parquet copies it rather than reading the rows again.
    """
    _check_export_layout(package.export_plan.targets, output_dir)
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

"""Gold stage artifact models (#47)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from ...spec import ExportTarget
from ...tabular.duckdb_load import TableHandle


class SplitTable(Protocol):
    """One split of a Gold table: what persisting it needs.

    Splits are still made on a Polars frame (#871), which satisfies this; the Gold
    table itself is a DuckDB table (#870).
    """

    @property
    def height(self) -> int: ...

    def write_parquet(self, file: Path, /) -> None: ...


@dataclass(frozen=True)
class ExportPlan:
    """export plan for Gold package."""

    targets: tuple[ExportTarget, ...] = ()


@dataclass(frozen=True)
class GoldPackage:
    """final dataset package ready for export."""

    dataset_name: str
    table: TableHandle
    export_plan: ExportPlan
    source_silver: str
    metadata: dict[str, str] = field(default_factory=dict)
    splits: Mapping[str, SplitTable] | None = None
    source_refs: tuple[str, ...] | None = None

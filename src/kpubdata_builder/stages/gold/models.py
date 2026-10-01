"""Gold stage artifact models (#47)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ...spec import ExportTarget
from ...tabular.duckdb_load import TableHandle


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
    splits: Mapping[str, TableHandle] | None = None
    source_refs: tuple[str, ...] | None = None

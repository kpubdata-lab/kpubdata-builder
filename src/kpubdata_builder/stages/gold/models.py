"""Gold stage artifact models (#47)."""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ...spec import ExportTarget


@dataclass(frozen=True)
class ExportPlan:
    """export plan for Gold package."""

    targets: tuple[ExportTarget, ...] = ()


@dataclass(frozen=True)
class GoldPackage:
    """final dataset package ready for export."""

    dataset_name: str
    table: pl.DataFrame
    export_plan: ExportPlan
    source_silver: str
    metadata: dict[str, str] = field(default_factory=dict)
    splits: dict[str, pl.DataFrame] | None = None
    source_refs: tuple[str, ...] | None = None

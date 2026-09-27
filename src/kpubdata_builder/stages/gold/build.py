"""Gold stage orchestration (#47)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...spec import ExportTarget, SplitSpec
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage
from .split import apply_splits_to_frame


def build_gold_package(
    silver: SilverDataset,
    *,
    dataset_name: str,
    exports: Sequence[ExportTarget] = (),
    metadata: Mapping[str, str] | None = None,
    splits_spec: SplitSpec | None = None,
) -> GoldPackage:
    """transforms Silver datasets into export-ready Gold packages."""
    splits = None
    if splits_spec is not None:
        splits = apply_splits_to_frame(silver.table, splits_spec)

    return GoldPackage(
        dataset_name=dataset_name,
        table=silver.table,
        export_plan=ExportPlan(targets=tuple(exports)),
        source_silver=silver.source_bronze,
        metadata=dict(metadata or {}),
        splits=splits,
    )

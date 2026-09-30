"""Gold stage orchestration (#47)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from ...spec import ExportTarget, SplitSpec
from ...tabular.polars_bridge import to_polars
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
    table: pl.DataFrame | None = None,
) -> GoldPackage:
    """transforms Silver datasets into export-ready Gold packages.

    ``table`` is the Silver table after a Gold selection (#659); Silver's own table,
    through the Polars bridge until Gold runs on DuckDB (#870), when it is None.
    """
    frame = table if table is not None else to_polars(silver.table)
    splits = None
    if splits_spec is not None:
        splits = apply_splits_to_frame(frame, splits_spec)

    return GoldPackage(
        dataset_name=dataset_name,
        table=frame,
        export_plan=ExportPlan(targets=tuple(exports)),
        source_silver=silver.source_bronze,
        metadata=dict(metadata or {}),
        splits=splits,
    )

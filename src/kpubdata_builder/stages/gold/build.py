"""Gold stage orchestration (#47)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ...spec import ExportTarget, SplitSpec
from ...tabular.duckdb_load import TableHandle
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage
from .split import apply_splits_to_table


def build_gold_package(
    silver: SilverDataset,
    *,
    dataset_name: str,
    exports: Sequence[ExportTarget] = (),
    metadata: Mapping[str, str] | None = None,
    splits_spec: SplitSpec | None = None,
    table: TableHandle | None = None,
) -> GoldPackage:
    """transforms Silver datasets into export-ready Gold packages.

    ``table`` is the Silver table after a Gold selection and PII masking (#659, #689);
    Silver's own table when None. Splits are tables in the same connection (#871).
    """
    gold_table = table if table is not None else silver.table
    splits = None
    if splits_spec is not None:
        splits = apply_splits_to_table(gold_table, splits_spec)

    return GoldPackage(
        dataset_name=dataset_name,
        table=gold_table,
        export_plan=ExportPlan(targets=tuple(exports)),
        source_silver=silver.source_bronze,
        metadata=dict(metadata or {}),
        splits=splits,
    )

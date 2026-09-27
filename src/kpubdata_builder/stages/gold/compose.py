"""combines two sources' Silver tables into a single Gold dataset (#506)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import polars as pl

from ...spec import ExportTarget, JoinSpec
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage

_JOIN_HOW: dict[str, Literal["inner", "left"]] = {"inner": "inner", "left": "left"}


class CompositionError(RuntimeError):
    """composition (join) execution failure. orchestrator treats it the same as source failure."""


@dataclass(frozen=True)
class CompositionStats:
    """join execution statistics — passed as-is to CompositionProvenance."""

    left_row_count: int
    left_distinct_key_count: int
    right_row_count: int
    right_distinct_key_count: int
    output_row_count: int
    duplicate_key_warning: bool


def _dtypes_compatible(left: pl.DataType, right: pl.DataType) -> bool:
    """judges join key dtype compatibility."""
    return left == right


def _validate_join_keys(
    left_table: pl.DataFrame, right_table: pl.DataFrame, join: JoinSpec
) -> None:
    """checks join key existence and dtype compatibility. runtime validation gate of build."""
    if join.left_key not in left_table.columns:
        raise CompositionError(
            f"composition.join.left_key {join.left_key!r} not found in {join.left!r} columns: "
            f"{sorted(left_table.columns)}"
        )
    if join.right_key not in right_table.columns:
        raise CompositionError(
            f"composition.join.right_key {join.right_key!r} not found in {join.right!r} columns: "
            f"{sorted(right_table.columns)}"
        )
    left_dtype = left_table.schema[join.left_key]
    right_dtype = right_table.schema[join.right_key]
    if not _dtypes_compatible(left_dtype, right_dtype):
        raise CompositionError(
            f"composition join key dtype mismatch: {join.left}.{join.left_key} ({left_dtype}) "
            f"vs {join.right}.{join.right_key} ({right_dtype})"
        )


def _distinct_key_count(table: pl.DataFrame, key: str) -> int:
    """count of distinct join key values excluding null. null never matches in any standard."""
    return int(table[key].drop_nulls().n_unique())


def build_composed_gold_package(
    *,
    left_silver: SilverDataset,
    right_silver: SilverDataset,
    join: JoinSpec,
    dataset_name: str,
    exports: Sequence[ExportTarget] = (),
    metadata: Mapping[str, str] | None = None,
) -> tuple[GoldPackage, CompositionStats]:
    """joins two SilverDatasets to create combined GoldPackage and execution statistics."""
    left_table = left_silver.table
    right_table = right_silver.table
    _validate_join_keys(left_table, right_table, join)

    left_row_count = left_table.height
    right_row_count = right_table.height
    left_distinct = _distinct_key_count(left_table, join.left_key)
    right_distinct = _distinct_key_count(right_table, join.right_key)
    # if both join keys have duplicate values, becomes many-to-many, rows multiply
    # exponentially — this structural signal rather than exact multiple(both non-unique)detected.
    duplicate_key_warning = left_distinct < left_row_count and right_distinct < right_row_count

    if duplicate_key_warning and join.on_duplicate_key == "fail":
        raise CompositionError(
            f"composition {dataset_name!r}: duplicate join keys on both sides "
            f"({join.left}.{join.left_key}: {left_row_count - left_distinct} duplicate rows, "
            f"{join.right}.{join.right_key}: {right_row_count - right_distinct} duplicate rows) "
            "would multiply output rows (on_duplicate_key='fail')"
        )

    combined = left_table.join(
        right_table,
        left_on=join.left_key,
        right_on=join.right_key,
        how=_JOIN_HOW[join.type],
        suffix=f"_{join.right}",
    )

    stats = CompositionStats(
        left_row_count=left_row_count,
        left_distinct_key_count=left_distinct,
        right_row_count=right_row_count,
        right_distinct_key_count=right_distinct,
        output_row_count=combined.height,
        duplicate_key_warning=duplicate_key_warning,
    )
    package = GoldPackage(
        dataset_name=dataset_name,
        table=combined,
        export_plan=ExportPlan(targets=tuple(exports)),
        source_silver=f"{join.left}+{join.right}",
        metadata=dict(metadata or {}),
        source_refs=(join.left, join.right),
    )
    return package, stats


__all__ = ["CompositionError", "CompositionStats", "build_composed_gold_package"]

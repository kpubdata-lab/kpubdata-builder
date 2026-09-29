"""combines two sources' Silver tables into a single Gold dataset (#506).

Join cardinality is judged on the keys that actually intersect (#698): a key that
repeats on one side but never appears on the other cannot multiply any row, so it
neither raises a duplicate-key warning nor violates a declared cardinality.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import polars as pl

from ...spec import ExportTarget, JoinSpec
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage

_JOIN_HOW: dict[str, Literal["inner", "left"]] = {"inner": "inner", "left": "left"}

# Count columns used while comparing the two sides' key frequencies. They live only
# in intermediate frames that hold nothing but key columns and these counts.
_LEFT_COUNT = "__kpubdata_left_n"
_RIGHT_COUNT = "__kpubdata_right_n"

# Which observed cardinalities each declared cardinality allows (#698). A declared
# "many" side permits, but does not require, repeated keys.
_ALLOWED_OBSERVED: dict[str, frozenset[str]] = {
    "one_to_one": frozenset({"one_to_one"}),
    "one_to_many": frozenset({"one_to_one", "one_to_many"}),
    "many_to_one": frozenset({"one_to_one", "many_to_one"}),
    "many_to_many": frozenset({"one_to_one", "one_to_many", "many_to_one", "many_to_many"}),
}


class CompositionError(RuntimeError):
    """composition (join) execution failure. orchestrator treats it the same as source failure."""


@dataclass(frozen=True)
class CompositionStats:
    """join execution statistics — passed as-is to CompositionProvenance.

    Attributes:
        left_row_count: Left Silver table row count.
        left_distinct_key_count: Distinct non-null key tuples on the left.
        right_row_count: Right Silver table row count.
        right_distinct_key_count: Distinct non-null key tuples on the right.
        output_row_count: Join result row count.
        duplicate_key_warning: Some key present on both sides repeats on both sides,
            so its rows multiply many-to-many (#698: intersecting keys only).
        keys: Join key column pairs ``(left_column, right_column)``.
        cardinality: Declared cardinality, or None when not declared.
        observed_cardinality: Cardinality observed over the intersecting keys. A side
            is "many" when any intersecting key repeats on it; with no intersecting
            key at all it is "one_to_one".
        left_unmatched_ratio: Left rows whose key has no match on the right (null-key
            rows included) over left rows; 0.0 for an empty left side.
        right_unmatched_ratio: Same for the right side.
        expansion_ratio: Output rows over left rows; None when the left side is empty.
        left_null_key_rows: Left rows with a null in any key column.
        right_null_key_rows: Right rows with a null in any key column.
    """

    left_row_count: int
    left_distinct_key_count: int
    right_row_count: int
    right_distinct_key_count: int
    output_row_count: int
    duplicate_key_warning: bool
    keys: tuple[tuple[str, str], ...]
    cardinality: str | None
    observed_cardinality: str
    left_unmatched_ratio: float
    right_unmatched_ratio: float
    expansion_ratio: float | None
    left_null_key_rows: int
    right_null_key_rows: int


def _dtypes_compatible(left: pl.DataType, right: pl.DataType) -> bool:
    """judges join key dtype compatibility."""
    return left == right


def _key_label(join: JoinSpec, index: int, side: str) -> str:
    """Spec path naming one key column, in the form the author wrote it."""
    if len(join.keys) == 1:
        return f"composition.join.{side}_key"
    return f"composition.join.keys[{index}].{side}"


def _validate_join_keys(
    left_table: pl.DataFrame, right_table: pl.DataFrame, join: JoinSpec
) -> None:
    """checks join key existence and dtype compatibility. runtime validation gate of build."""
    for index, (left_column, right_column) in enumerate(join.keys):
        if left_column not in left_table.columns:
            raise CompositionError(
                f"{_key_label(join, index, 'left')} {left_column!r} not found in "
                f"{join.left!r} columns: {sorted(left_table.columns)}"
            )
        if right_column not in right_table.columns:
            raise CompositionError(
                f"{_key_label(join, index, 'right')} {right_column!r} not found in "
                f"{join.right!r} columns: {sorted(right_table.columns)}"
            )
        left_dtype = left_table.schema[left_column]
        right_dtype = right_table.schema[right_column]
        if not _dtypes_compatible(left_dtype, right_dtype):
            raise CompositionError(
                f"composition join key dtype mismatch: {join.left}.{left_column} "
                f"({left_dtype}) vs {join.right}.{right_column} ({right_dtype})"
            )


def _null_key_mask(table: pl.DataFrame, columns: Sequence[str]) -> pl.Series:
    """True for rows with a null in any key column — such a row never matches."""
    return table.select(pl.any_horizontal([pl.col(c).is_null() for c in columns])).to_series()


def _key_counts(table: pl.DataFrame, columns: Sequence[str], alias: str) -> pl.DataFrame:
    """Row count per distinct non-null key tuple."""
    return table.select(columns).group_by(columns).agg(pl.len().alias(alias))


def _observed_cardinality(left_many: bool, right_many: bool) -> str:
    left_part = "many" if left_many else "one"
    right_part = "many" if right_many else "one"
    return f"{left_part}_to_{right_part}"


def _describe_key(row: Mapping[str, object], join: JoinSpec) -> str:
    """Render one intersecting key and its per-side counts for an error message."""
    values = ", ".join(f"{lc}={row[lc]!r}" for lc, _ in join.keys)
    return f"({values}): {row[_LEFT_COUNT]} left rows x {row[_RIGHT_COUNT]} right rows"


def _worst_key(frame: pl.DataFrame, join: JoinSpec) -> Mapping[str, object]:
    """The offending key that multiplies the most rows, ties broken by key order."""
    left_columns = [lc for lc, _ in join.keys]
    ordered = frame.with_columns(
        (pl.col(_LEFT_COUNT) * pl.col(_RIGHT_COUNT)).alias("__kpubdata_product")
    ).sort(
        ["__kpubdata_product", *left_columns],
        descending=[True, *([False] * len(left_columns))],
    )
    return ordered.row(0, named=True)


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


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

    left_columns = [lc for lc, _ in join.keys]
    right_columns = [rc for _, rc in join.keys]
    left_row_count = left_table.height
    right_row_count = right_table.height

    # Null keys never match in any standard, so an inner join drops those rows
    # silently. Count them per side so the drop is reported, not hidden (#698).
    left_null_mask = _null_key_mask(left_table, left_columns)
    right_null_mask = _null_key_mask(right_table, right_columns)
    left_null_key_rows = int(left_null_mask.sum())
    right_null_key_rows = int(right_null_mask.sum())
    if join.on_null_key == "fail":
        for side, alias, count in (
            ("left", join.left, left_null_key_rows),
            ("right", join.right, right_null_key_rows),
        ):
            if count:
                raise CompositionError(
                    f"composition {dataset_name!r}: {count} {side} row(s) of {alias!r} have "
                    "a null join key and can never match (on_null_key='fail')"
                )

    left_counts = _key_counts(left_table.filter(~left_null_mask), left_columns, _LEFT_COUNT)
    right_counts = _key_counts(right_table.filter(~right_null_mask), right_columns, _RIGHT_COUNT)
    # Only the keys present on both sides can multiply rows (#698).
    intersecting = left_counts.join(
        right_counts, left_on=left_columns, right_on=right_columns, how="inner"
    )
    left_repeats = pl.col(_LEFT_COUNT) > 1
    right_repeats = pl.col(_RIGHT_COUNT) > 1
    left_many = intersecting.filter(left_repeats).height > 0
    right_many = intersecting.filter(right_repeats).height > 0
    observed = _observed_cardinality(left_many, right_many)

    if join.cardinality is not None and observed not in _ALLOWED_OBSERVED[join.cardinality]:
        conditions: list[pl.Expr] = []
        if join.cardinality.startswith("one_"):
            conditions.append(left_repeats)
        if join.cardinality.endswith("_one"):
            conditions.append(right_repeats)
        offending = intersecting.filter(pl.any_horizontal(conditions))
        raise CompositionError(
            f"composition {dataset_name!r}: declared cardinality {join.cardinality!r} but "
            f"the intersecting keys are {observed!r}; e.g. key "
            f"{_describe_key(_worst_key(offending, join), join)}"
        )

    amplifying = intersecting.filter(left_repeats & right_repeats)
    duplicate_key_warning = amplifying.height > 0
    if duplicate_key_warning and join.on_duplicate_key == "fail":
        raise CompositionError(
            f"composition {dataset_name!r}: {amplifying.height} join key(s) repeat on both "
            f"sides and would multiply output rows, e.g. key "
            f"{_describe_key(_worst_key(amplifying, join), join)} "
            "(on_duplicate_key='fail')"
        )

    left_matched_rows = int(intersecting[_LEFT_COUNT].sum())
    right_matched_rows = int(intersecting[_RIGHT_COUNT].sum())

    combined = left_table.join(
        right_table,
        left_on=left_columns,
        right_on=right_columns,
        how=_JOIN_HOW[join.type],
        suffix=f"_{join.right}",
    )

    stats = CompositionStats(
        left_row_count=left_row_count,
        left_distinct_key_count=left_counts.height,
        right_row_count=right_row_count,
        right_distinct_key_count=right_counts.height,
        output_row_count=combined.height,
        duplicate_key_warning=duplicate_key_warning,
        keys=join.keys,
        cardinality=join.cardinality,
        observed_cardinality=observed,
        left_unmatched_ratio=_ratio(left_row_count - left_matched_rows, left_row_count),
        right_unmatched_ratio=_ratio(right_row_count - right_matched_rows, right_row_count),
        expansion_ratio=combined.height / left_row_count if left_row_count else None,
        left_null_key_rows=left_null_key_rows,
        right_null_key_rows=right_null_key_rows,
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

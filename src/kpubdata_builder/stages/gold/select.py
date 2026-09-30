"""Apply a source's Gold selection: rows by filter, then columns (#659).

Silver keeps every column and row (#611); this is where a published table's shape is
decided (ADR 0018 option C). A filter is data — a column, a named operator and a value —
never an expression to evaluate. A column the selection names that Silver does not have,
or a value that cannot be compared with the column, fails the source with a message
naming it rather than publishing a different table than the spec asked for.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ...spec import JsonValue
from ...spec.models import GoldFilter, GoldSelection


class GoldSelectionError(ValueError):
    """The selection cannot be applied to this Silver table as written."""


@dataclass(frozen=True)
class GoldSelectionResult:
    """What the selection did, for the manifest.

    ``input_rows`` is Silver's row count — the one quality was measured on — and
    ``output_rows`` Gold's, so the two are never confused.
    """

    input_rows: int
    output_rows: int
    select: tuple[str, ...]
    filters: tuple[GoldFilter, ...]

    def body(self) -> dict[str, JsonValue]:
        return {
            "input_rows": self.input_rows,
            "output_rows": self.output_rows,
            "dropped_rows": self.input_rows - self.output_rows,
            "select": list(self.select),
            "filters": [
                {
                    "column": f.column,
                    "op": f.op,
                    **({} if f.op == "not_null" else {"value": f.value}),
                }
                for f in self.filters
            ],
        }


def _predicate(rule: GoldFilter, frame: pl.DataFrame) -> pl.Expr:
    if rule.column not in frame.columns:
        raise GoldSelectionError(f"gold filter names column {rule.column!r}, which Silver lacks")
    column = pl.col(rule.column)
    if rule.op == "not_null":
        return column.is_not_null()
    if rule.op == "in":
        values = rule.value if isinstance(rule.value, list) else [rule.value]
        return column.is_in(values).fill_null(False)
    comparisons = {
        "eq": column == rule.value,
        "ne": column != rule.value,
        "gt": column > rule.value,
        "ge": column >= rule.value,
        "lt": column < rule.value,
        "le": column <= rule.value,
    }
    # A null never passes a comparison — a ``> 0`` filter does not keep unknown values.
    return comparisons[rule.op].fill_null(False)


def apply_gold_selection(
    frame: pl.DataFrame, selection: GoldSelection
) -> tuple[pl.DataFrame, GoldSelectionResult]:
    """Filter rows, then select columns, as the spec declares."""
    missing = [c for c in selection.select if c not in frame.columns]
    if missing:
        raise GoldSelectionError(f"gold select names columns Silver lacks: {missing}")
    result = frame
    for rule in selection.filters:
        try:
            result = result.filter(_predicate(rule, result))
        except (pl.exceptions.ComputeError, pl.exceptions.InvalidOperationError) as exc:
            raise GoldSelectionError(
                f"gold filter {rule.column} {rule.op} {rule.value!r} cannot be applied to "
                f"a {frame.schema[rule.column]} column"
            ) from exc
    if selection.select:
        result = result.select(list(selection.select))
    return result, GoldSelectionResult(
        input_rows=frame.height,
        output_rows=result.height,
        select=selection.select,
        filters=selection.filters,
    )


__all__ = ["GoldSelectionError", "GoldSelectionResult", "apply_gold_selection"]

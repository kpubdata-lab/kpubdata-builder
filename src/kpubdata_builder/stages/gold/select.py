"""Apply a source's Gold selection: rows by filter, then columns (#659).

Silver keeps every column and row (#611); this is where a published table's shape is
decided (ADR 0018 option C). A filter is data — a column, a named operator and a value —
never an expression to evaluate. A column the selection names that Silver does not have,
or a value that cannot be compared with the column, fails the source with a message
naming it rather than publishing a different table than the spec asked for.

The selection runs in DuckDB SQL on the Silver table (#870). Which values a column can
be compared with is decided here, from the column's Builder dtype, and never left to
DuckDB's implicit casts: a number compares with a number column, text with a text
column, a boolean with a boolean column. Every other pairing — a date or duration column
(no filter value is one), a list, a number against text — is refused. A value that is
null, or a column that holds only nulls, keeps no row: a null never passes a comparison.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from ...spec import JsonValue
from ...spec.models import GoldFilter, GoldSelection
from ...tabular.duckdb_load import Node, TableHandle
from ...tabular.duckdb_runtime import ROW_SEQ_COLUMN, TabularRelation
from ...tabular.sql import quote_identifier


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


_COMPARISONS = {"eq": "=", "ne": "<>", "gt": ">", "ge": ">=", "lt": "<", "le": "<="}
_NUMERIC = frozenset({"int", "int128", "float", "decimal"})
_names = itertools.count()


def _relation(handle: TableHandle, step: str) -> TabularRelation:
    return TabularRelation(f"{handle.table.relation.name}_gold_{step}_{next(_names)}")


def _accepts(node: Node, value: JsonValue) -> bool:
    """Whether a non-null filter ``value`` compares with a column of ``node``."""
    kind = node[0]
    if kind in _NUMERIC:
        return isinstance(value, int | float) and not isinstance(value, bool)
    if kind == "str":
        return isinstance(value, str)
    if kind == "bool":
        return isinstance(value, bool)
    return False


def _refuse(rule: GoldFilter, dtype: str) -> GoldSelectionError:
    return GoldSelectionError(
        f"gold filter {rule.column} {rule.op} {rule.value!r} cannot be applied to a {dtype} column"
    )


def _predicate(rule: GoldFilter, handle: TableHandle) -> tuple[str, list[object]]:
    """The SQL condition for one filter and its bound values."""
    table = handle.table
    if rule.column not in table.names:
        raise GoldSelectionError(f"gold filter names column {rule.column!r}, which Silver lacks")
    index = table.names.index(rule.column)
    node, dtype = table.nodes[index], table.dtypes[index]
    column = quote_identifier(table.physical[index])
    if rule.op == "not_null":
        return f"{column} IS NOT NULL", []
    values = rule.value if rule.op == "in" and isinstance(rule.value, list) else [rule.value]
    present = [v for v in values if v is not None]
    if node[0] == "null":
        # A column of nulls: nothing to compare, and nothing passes.
        return "false", []
    if any(not _accepts(node, v) for v in present):
        raise _refuse(rule, dtype)
    if not present:
        return "false", []
    if rule.op == "in":
        # One placeholder per value: each is bound with its own type, never as a list
        # DuckDB would coerce to one element type.
        placeholders = ", ".join("?" for _ in present)
        return f"coalesce({column} IN ({placeholders}), false)", list(present)
    return f"coalesce({column} {_COMPARISONS[rule.op]} ?, false)", [present[0]]


def apply_gold_selection(
    table: TableHandle, selection: GoldSelection
) -> tuple[TableHandle, GoldSelectionResult]:
    """Filter rows, then select columns, as the spec declares.

    The result is a new table in ``table``'s connection, in Silver's row order.
    """
    loaded = table.table
    missing = [c for c in selection.select if c not in loaded.names]
    if missing:
        raise GoldSelectionError(f"gold select names columns Silver lacks: {missing}")
    conditions: list[str] = []
    params: list[object] = []
    for rule in selection.filters:
        condition, bound = _predicate(rule, table)
        conditions.append(condition)
        params.extend(bound)
    keep = list(selection.select) if selection.select else list(loaded.names)
    indices = [loaded.names.index(name) for name in keep]
    seq = quote_identifier(ROW_SEQ_COLUMN)
    columns = ", ".join(
        f"{quote_identifier(loaded.physical[i])} AS {quote_identifier(f'c{n}')}"
        for n, i in enumerate(indices)
    )
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    result = table.derive(
        f"SELECT row_number() OVER ({loaded.order_by}) - 1 AS {seq}"
        f"{', ' + columns if columns else ''} FROM {loaded.relation.sql}{where}",
        params,
        into=_relation(table, "selection"),
        columns=[(loaded.names[i], loaded.nodes[i]) for i in indices],
    )
    return result, GoldSelectionResult(
        input_rows=table.height,
        output_rows=result.height,
        select=selection.select,
        filters=selection.filters,
    )


__all__ = ["GoldSelectionError", "GoldSelectionResult", "apply_gold_selection"]

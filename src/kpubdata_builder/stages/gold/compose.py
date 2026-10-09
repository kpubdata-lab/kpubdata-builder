"""combines two sources' Silver tables into a single Gold dataset (#506).

Join cardinality is judged on the keys that actually intersect (#698): a key that
repeats on one side but never appears on the other cannot multiply any row, so it
neither raises a duplicate-key warning nor violates a declared cardinality.

The join and its statistics run in DuckDB SQL (#870), on two tables in one connection.
The rules are the ones the Polars join followed: key dtypes must be equal; a NaN key
is a null key (#793); a null key never matches; the right side's key columns are not
in the output, and a right column whose name the left side has is suffixed with
``_<right alias>``. Rows come out in the left side's order, and for one left row, in the
right side's.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ...spec import ExportTarget, JoinSpec
from ...tabular.duckdb_load import Node, TableHandle
from ...tabular.duckdb_runtime import ROW_SEQ_COLUMN, TabularRelation
from ...tabular.sql import quote_identifier
from ..silver.models import SilverDataset
from .models import ExportPlan, GoldPackage

_JOIN_SQL: dict[str, str] = {"inner": "INNER JOIN", "left": "LEFT JOIN"}

# Which observed cardinalities each declared cardinality allows (#698). A declared
# "many" side permits, but does not require, repeated keys.
_ALLOWED_OBSERVED: dict[str, frozenset[str]] = {
    "one_to_one": frozenset({"one_to_one"}),
    "one_to_many": frozenset({"one_to_one", "one_to_many"}),
    "many_to_one": frozenset({"one_to_one", "many_to_one"}),
    "many_to_many": frozenset({"one_to_one", "one_to_many", "many_to_one", "many_to_many"}),
}

_names = itertools.count()


#: What a failed composition is called where its message may not be shown (#1120):
#: a code and a fixed sentence. A ``CompositionError`` message is written for the run's
#: owner and names the tables' columns and an offending join key's value; a list that
#: serves other owners' runs (the build index, ``/admin/runs``) gets only these.
COMPOSITION_FAILURE_SUMMARIES: dict[str, str] = {
    "join_key_missing": "a join key column is missing from the source it is declared for",
    "join_key_type_mismatch": "the join key columns have different types on the two sides",
    "join_null_key": "rows with a null join key were refused (on_null_key='fail')",
    "join_cardinality_mismatch": "the join keys do not have the declared cardinality",
    "join_duplicate_key": (
        "join keys repeat on both sides and were refused (on_duplicate_key='fail')"
    ),
    "join_output_column_conflict": "the join would produce two columns with the same name",
    "composition_failed": "the composition failed",
}


class CompositionError(RuntimeError):
    """composition (join) execution failure. orchestrator treats it the same as source failure.

    ``code`` is a key of ``COMPOSITION_FAILURE_SUMMARIES``: which rule the join broke,
    without the columns and key values the message carries (#1120).
    """

    def __init__(self, message: str, *, code: str = "composition_failed") -> None:
        super().__init__(message)
        self.code = code


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


def _key_label(join: JoinSpec, index: int, side: str) -> str:
    """Spec path naming one key column, in the form the author wrote it."""
    if len(join.keys) == 1:
        return f"composition.join.{side}_key"
    return f"composition.join.keys[{index}].{side}"


def _validate_join_keys(left: TableHandle, right: TableHandle, join: JoinSpec) -> None:
    """checks join key existence and dtype compatibility. runtime validation gate of build."""
    for index, (left_column, right_column) in enumerate(join.keys):
        if left_column not in left.columns:
            raise CompositionError(
                f"{_key_label(join, index, 'left')} {left_column!r} not found in "
                f"{join.left!r} columns: {sorted(left.columns)}",
                code="join_key_missing",
            )
        if right_column not in right.columns:
            raise CompositionError(
                f"{_key_label(join, index, 'right')} {right_column!r} not found in "
                f"{join.right!r} columns: {sorted(right.columns)}",
                code="join_key_missing",
            )
        left_dtype = left.dtypes[left.columns.index(left_column)]
        right_dtype = right.dtypes[right.columns.index(right_column)]
        if left_dtype != right_dtype:
            raise CompositionError(
                f"composition join key dtype mismatch: {join.left}.{left_column} "
                f"({left_dtype}) vs {join.right}.{right_column} ({right_dtype})",
                code="join_key_type_mismatch",
            )


def _nan_keys_to_null(table: TableHandle, columns: Sequence[str]) -> TableHandle:
    """Float key columns with NaN turned into null (#793).

    NaN is not a value that equals anything, so it is treated as the missing key it
    stands for — counted as a null key, never matched — and the statistics and the
    join follow the same rule. (DuckDB, like Polars, would otherwise match NaN keys to
    each other.)
    """
    loaded = table.table
    floats = {c for c in columns if loaded.nodes[loaded.names.index(c)] == ("float",)}
    if not floats:
        return table
    parts = [quote_identifier(ROW_SEQ_COLUMN)]
    for index, (name, physical) in enumerate(zip(loaded.names, loaded.physical, strict=True)):
        column = quote_identifier(physical)
        expression = (
            f"CASE WHEN isnan({column}) THEN NULL ELSE {column} END" if name in floats else column
        )
        parts.append(f"{expression} AS {quote_identifier(f'c{index}')}")
    return table.derive(
        f"SELECT {', '.join(parts)} FROM {loaded.relation.sql}",
        into=TabularRelation(f"{loaded.relation.name}_keys_{next(_names)}"),
        columns=list(zip(loaded.names, loaded.nodes, strict=True)),
    )


def _observed_cardinality(left_many: bool, right_many: bool) -> str:
    left_part = "many" if left_many else "one"
    right_part = "many" if right_many else "one"
    return f"{left_part}_to_{right_part}"


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True)
class _KeySql:
    """The SQL pieces for one join's keys over its two sides."""

    left: TableHandle
    right: TableHandle
    join: JoinSpec

    def columns(self, side: str) -> list[str]:
        table, names = (
            (self.left, [lc for lc, _ in self.join.keys])
            if side == "left"
            else (self.right, [rc for _, rc in self.join.keys])
        )
        return [table.table.column(name) for name in names]

    def null_rows(self, side: str) -> str:
        table = self.left if side == "left" else self.right
        condition = " OR ".join(f"{c} IS NULL" for c in self.columns(side))
        return f"SELECT count(*) FROM {table.table.relation.sql} WHERE {condition}"

    def counts(self, side: str) -> str:
        """Row count per distinct non-null key tuple, keys as ``k0…`` and count ``n``."""
        table = self.left if side == "left" else self.right
        keys = self.columns(side)
        select = ", ".join(f"{c} AS k{i}" for i, c in enumerate(keys))
        where = " AND ".join(f"{c} IS NOT NULL" for c in keys)
        group = ", ".join(keys)
        return (
            f"SELECT {select}, count(*) AS n FROM {table.table.relation.sql} "
            f"WHERE {where} GROUP BY {group}"
        )

    def intersecting(self) -> str:
        """Keys present on both sides with their per-side counts ``ln`` and ``rn``."""
        on = " AND ".join(f"lc.k{i} = rc.k{i}" for i in range(len(self.join.keys)))
        keys = ", ".join(f"lc.k{i}" for i in range(len(self.join.keys)))
        return (
            f"WITH lc AS ({self.counts('left')}), rc AS ({self.counts('right')}) "
            f"SELECT {keys}, lc.n AS ln, rc.n AS rn FROM lc JOIN rc ON {on}"
        )


def _scalar(table: TableHandle, sql: str) -> int:
    rows = table.fetch(sql)
    return int(rows[0][0]) if rows and rows[0][0] is not None else 0


def _worst_key(keys: _KeySql, condition: str) -> str:
    """The offending key that multiplies the most rows, ties broken by key order,
    rendered for an error message."""
    count = len(keys.join.keys)
    order = ", ".join(f"k{i}" for i in range(count))
    (row,) = keys.left.fetch(
        f"SELECT * FROM ({keys.intersecting()}) WHERE {condition} "
        f"ORDER BY ln * rn DESC, {order} LIMIT 1"
    )
    left_names = [lc for lc, _ in keys.join.keys]
    values = keys.left.decode_row(left_names, row[:count])
    rendered = ", ".join(f"{name}={values[name]!r}" for name in left_names)
    return f"({rendered}): {row[count]} left rows x {row[count + 1]} right rows"


def _output_columns(
    left: TableHandle, right: TableHandle, join: JoinSpec
) -> list[tuple[str, str, str, Node]]:
    """``(name, side, physical, node)`` for each output column, in output order."""
    out: list[tuple[str, str, str, Node]] = [
        (name, "l", physical, node)
        for name, physical, node in zip(
            left.table.names, left.table.physical, left.table.nodes, strict=True
        )
    ]
    right_keys = {rc for _, rc in join.keys}
    taken = set(left.table.names)
    for name, physical, node in zip(
        right.table.names, right.table.physical, right.table.nodes, strict=True
    ):
        if name in right_keys:
            continue
        output = f"{name}_{join.right}" if name in taken else name
        if output in taken:
            raise CompositionError(
                f"composition join output would have two columns named {output!r}",
                code="join_output_column_conflict",
            )
        taken.add(output)
        out.append((output, "r", physical, node))
    return out


def build_composed_gold_package(
    *,
    left_silver: SilverDataset,
    right_silver: SilverDataset,
    join: JoinSpec,
    dataset_name: str,
    exports: Sequence[ExportTarget] = (),
    metadata: Mapping[str, str] | None = None,
    left_table: TableHandle | None = None,
    right_table: TableHandle | None = None,
) -> tuple[GoldPackage, CompositionStats]:
    """joins two SilverDatasets to create combined GoldPackage and execution statistics.

    ``left_table``/``right_table`` are the sides as they are joined, when they differ
    from Silver's (declared PII masked, #689); Silver's own tables otherwise. The joined
    table is created in the left side's connection; a right side from another
    connection is copied there first.
    """
    left = left_table if left_table is not None else left_silver.table
    right = right_table if right_table is not None else right_silver.table
    if not left.shares_connection(right):
        right = right.copied_to(left, table=f"composed_right_{next(_names)}")
    _validate_join_keys(left, right, join)

    left = _nan_keys_to_null(left, [lc for lc, _ in join.keys])
    right = _nan_keys_to_null(right, [rc for _, rc in join.keys])
    keys = _KeySql(left, right, join)
    left_row_count = left.height
    right_row_count = right.height

    # Null keys never match in any standard, so an inner join drops those rows
    # silently. Count them per side so the drop is reported, not hidden (#698).
    left_null_key_rows = _scalar(left, keys.null_rows("left"))
    right_null_key_rows = _scalar(left, keys.null_rows("right"))
    if join.on_null_key == "fail":
        for side, alias, count in (
            ("left", join.left, left_null_key_rows),
            ("right", join.right, right_null_key_rows),
        ):
            if count:
                raise CompositionError(
                    f"composition {dataset_name!r}: {count} {side} row(s) of {alias!r} have "
                    "a null join key and can never match (on_null_key='fail')",
                    code="join_null_key",
                )

    # Only the keys present on both sides can multiply rows (#698).
    (
        (
            left_distinct,
            right_distinct,
            left_many,
            right_many,
            amplifying,
            left_matched,
            right_matched,
        ),
    ) = left.fetch(
        f"WITH lc AS ({keys.counts('left')}), rc AS ({keys.counts('right')}), "
        f"i AS ({keys.intersecting()}) "
        "SELECT (SELECT count(*) FROM lc), (SELECT count(*) FROM rc), "
        "coalesce(bool_or(ln > 1), false), coalesce(bool_or(rn > 1), false), "
        "count(*) FILTER (WHERE ln > 1 AND rn > 1), coalesce(sum(ln), 0), "
        "coalesce(sum(rn), 0) FROM i"
    )
    observed = _observed_cardinality(bool(left_many), bool(right_many))

    if join.cardinality is not None and observed not in _ALLOWED_OBSERVED[join.cardinality]:
        conditions: list[str] = []
        if join.cardinality.startswith("one_"):
            conditions.append("ln > 1")
        if join.cardinality.endswith("_one"):
            conditions.append("rn > 1")
        raise CompositionError(
            f"composition {dataset_name!r}: declared cardinality {join.cardinality!r} but "
            f"the intersecting keys are {observed!r}; e.g. key "
            f"{_worst_key(keys, ' OR '.join(conditions))}",
            code="join_cardinality_mismatch",
        )

    duplicate_key_warning = int(amplifying) > 0
    if duplicate_key_warning and join.on_duplicate_key == "fail":
        raise CompositionError(
            f"composition {dataset_name!r}: {int(amplifying)} join key(s) repeat on both "
            f"sides and would multiply output rows, e.g. key "
            f"{_worst_key(keys, 'ln > 1 AND rn > 1')} "
            "(on_duplicate_key='fail')",
            code="join_duplicate_key",
        )

    output = _output_columns(left, right, join)
    seq = quote_identifier(ROW_SEQ_COLUMN)
    on = " AND ".join(
        f"l.{lc} = r.{rc}"
        for lc, rc in zip(keys.columns("left"), keys.columns("right"), strict=True)
    )
    select = ", ".join(
        f"{side}.{quote_identifier(physical)} AS {quote_identifier(f'c{i}')}"
        for i, (_, side, physical, _) in enumerate(output)
    )
    combined = left.derive(
        f"SELECT row_number() OVER (ORDER BY l.{seq}, r.{seq}) - 1 AS {seq}, {select} "
        f"FROM {left.table.relation.sql} l {_JOIN_SQL[join.type]} {right.table.relation.sql} r "
        f"ON {on}",
        into=TabularRelation(f"composed_{next(_names)}"),
        columns=[(name, node) for name, _, _, node in output],
    )

    stats = CompositionStats(
        left_row_count=left_row_count,
        left_distinct_key_count=int(left_distinct),
        right_row_count=right_row_count,
        right_distinct_key_count=int(right_distinct),
        output_row_count=combined.height,
        duplicate_key_warning=duplicate_key_warning,
        keys=join.keys,
        cardinality=join.cardinality,
        observed_cardinality=observed,
        left_unmatched_ratio=_ratio(left_row_count - int(left_matched), left_row_count),
        right_unmatched_ratio=_ratio(right_row_count - int(right_matched), right_row_count),
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

"""Validated aggregates over one table snapshot (#818).

A chart or a summary needs an aggregate, and until now the only way to get one was
user SQL. SQL cannot be told which columns may be added up, so it happily sums rows
counted in different units, aggregates the first N rows instead of all of them, and
lets a client confuse ``count(*)`` with ``count(column)``. This module is the plan and
the child-process worker for an aggregate that is checked before it runs:

- **Named functions only.** ``count_rows`` counts rows; ``count`` counts a column's
  non-null values; ``count_null`` its nulls; ``count_distinct`` its distinct non-null
  values. ``sum``, ``avg``, ``min`` and ``max`` need a column of a type they make sense
  for. Nothing is aggregated by default.
- **Sum is never assumed.** A numeric column is not necessarily additive — a rate, an
  index or a stock level added up is a wrong number that looks right. ``sum`` needs the
  request to say ``additive: true``; Builder has no column metadata that could say so
  (ADR 0019 hints are not stored with snapshots yet), so the caller's assertion is
  echoed back rather than silently trusted. A group whose values are all null sums to
  null, not 0.
- **Units are checked, not assumed.** When the rows carry their unit in a column
  (``unit_column``), a group that mixes units is refused (``unit_policy: reject``, the
  default) or split by unit (``split``). It is never added up across units, and nothing
  is converted.
- **Aggregate first, then top N.** Every row that passes the filters is aggregated;
  ``limit`` only cuts the sorted groups afterwards, and the response says how many
  groups there were in total, so a chart can tell a full result from a top-N one.
- **Nothing propagates.** Output column metadata is read from the aggregate's own types.
  An aggregate is a new value, so no semantic, display or unit hint of its input is
  carried over to it.
- **Same limits as a query.** The worker runs through ``QueryEngine`` — child process,
  timeout, memory cap — and takes a slot from the same concurrency limit. Too many
  groups, or a result too large to send, is refused with its own code instead of being
  cut short.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import TYPE_CHECKING, Literal, cast

from ..spec import JsonValue
from ..tabular.builder_parquet import scan_builder_parquet
from .rows import RowFilter, filter_predicate, parse_filters, typed_literal

if TYPE_CHECKING:
    import polars as pl

AggregateFn = Literal[
    "count_rows", "count", "count_null", "count_distinct", "sum", "avg", "min", "max"
]
UnitPolicy = Literal["reject", "split"]

MAX_GROUP_BY = 4
MAX_MEASURES = 16
MAX_ORDER_KEYS = 8
MAX_AGGREGATE_LIMIT = 1000
DEFAULT_AGGREGATE_LIMIT = 100
#: Groups an aggregate may produce before the top N is taken. Above it the request is
#: refused (``too_many_groups``): a chart of a hundred thousand bars is a wrong request.
MAX_GROUPS = 100_000
#: How many mixed-unit groups a refusal names.
MIXED_UNIT_SAMPLES = 5

_FNS: frozenset[str] = frozenset(
    {"count_rows", "count", "count_null", "count_distinct", "sum", "avg", "min", "max"}
)
_NUMERIC_FNS = frozenset({"sum", "avg"})
_ORDERED_FNS = frozenset({"min", "max"})
#: Internal columns used while a group's units are checked. Refused as table columns.
_UNITS_N = "__kpubdata_units_n"
_UNITS = "__kpubdata_units"


@dataclass(frozen=True)
class Measure:
    fn: AggregateFn
    alias: str
    column: str | None = None
    additive: bool = False


@dataclass(frozen=True)
class OrderKey:
    key: str
    descending: bool = False


@dataclass(frozen=True)
class AggregatePlan:
    measures: tuple[Measure, ...]
    group_by: tuple[str, ...] = ()
    filters: tuple[RowFilter, ...] = ()
    order: tuple[OrderKey, ...] = ()
    limit: int = DEFAULT_AGGREGATE_LIMIT
    unit_column: str | None = None
    unit_policy: UnitPolicy = "reject"

    @property
    def key_columns(self) -> tuple[str, ...]:
        """The columns rows are grouped by: ``group_by``, plus the unit when split."""
        if (
            self.unit_column is not None
            and self.unit_policy == "split"
            and self.unit_column not in self.group_by
        ):
            return (*self.group_by, self.unit_column)
        return self.group_by

    @property
    def output_columns(self) -> tuple[str, ...]:
        """Group keys, then the checked unit (when not already a key), then measures."""
        keys = self.key_columns
        unit = (
            (self.unit_column,)
            if self.unit_column is not None and self.unit_column not in keys
            else ()
        )
        return (*keys, *unit, *(m.alias for m in self.measures))

    def to_json(self) -> str:
        return json.dumps(
            {
                "measures": [
                    {"fn": m.fn, "alias": m.alias, "column": m.column, "additive": m.additive}
                    for m in self.measures
                ],
                "group_by": list(self.group_by),
                "filters": [
                    {"column": f.column, "op": f.op, "value": f.value, "values": list(f.values)}
                    for f in self.filters
                ],
                "order": [{"key": k.key, "descending": k.descending} for k in self.order],
                "limit": self.limit,
                "unit_column": self.unit_column,
                "unit_policy": self.unit_policy,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> AggregatePlan:
        data = json.loads(raw)
        return AggregatePlan(
            measures=tuple(
                Measure(m["fn"], m["alias"], m["column"], bool(m["additive"]))
                for m in data["measures"]
            ),
            group_by=tuple(data["group_by"]),
            filters=tuple(
                RowFilter(f["column"], f["op"], f["value"], tuple(f["values"]))
                for f in data["filters"]
            ),
            order=tuple(OrderKey(k["key"], bool(k["descending"])) for k in data["order"]),
            limit=int(data["limit"]),
            unit_column=data["unit_column"],
            unit_policy=data["unit_policy"],
        )


def _name(value: JsonValue, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty column name")
    return value


def _parse_measure(item: JsonValue) -> Measure:
    if not isinstance(item, dict) or not set(item) <= {"fn", "column", "as", "additive"}:
        raise ValueError("each measure is {fn, column, as, additive}")
    fn = item.get("fn")
    if fn not in _FNS:
        raise ValueError(f"measure fn must be one of {sorted(_FNS)}")
    column: str | None
    if fn == "count_rows":
        if "column" in item:
            raise ValueError("count_rows counts rows and takes no column; use count for a column")
        column = None
    else:
        column = _name(item.get("column"), f"the column of {fn}")
    additive = item.get("additive", False)
    if not isinstance(additive, bool):
        raise ValueError("additive must be a boolean")
    if fn == "sum" and not additive:
        raise ValueError(
            f"sum of {column!r} needs additive: true — a numeric column is not additive by "
            "default (rates, indices and stock levels are not)"
        )
    if fn != "sum" and "additive" in item:
        raise ValueError("additive applies to sum only")
    alias = item.get("as", "count_rows" if column is None else f"{fn}_{column}")
    if not isinstance(alias, str) or not alias:
        raise ValueError("a measure's as must be a non-empty name")
    return Measure(cast(AggregateFn, fn), alias, column, additive)


def parse_aggregate_plan(body: Mapping[str, JsonValue]) -> AggregatePlan:
    """Read the grouping, measure, filter, order, limit and unit fields of a request.

    Shape only: whether the columns exist and have fitting types is checked against the
    snapshot's schema by ``check_aggregate_plan``.
    """
    raw_group = body.get("group_by", [])
    if not isinstance(raw_group, list) or len(raw_group) > MAX_GROUP_BY:
        raise ValueError(f"group_by must be a list of at most {MAX_GROUP_BY} columns")
    group_by = tuple(_name(c, "each group_by entry") for c in raw_group)
    if len(set(group_by)) != len(group_by):
        raise ValueError("group_by must not repeat a column")

    raw_measures = body.get("measures")
    if not isinstance(raw_measures, list) or not 1 <= len(raw_measures) <= MAX_MEASURES:
        raise ValueError(f"measures must be a list of 1 to {MAX_MEASURES} measures")
    measures = tuple(_parse_measure(m) for m in raw_measures)

    unit_column = body.get("unit_column")
    if unit_column is not None:
        unit_column = _name(unit_column, "unit_column")
    unit_policy = body.get("unit_policy", "reject")
    if unit_policy not in ("reject", "split"):
        raise ValueError("unit_policy must be reject or split")
    if unit_column is None and "unit_policy" in body:
        raise ValueError("unit_policy needs a unit_column")

    aliases = [m.alias for m in measures]
    taken = {*group_by, *((unit_column,) if unit_column else ())}
    if len(set(aliases)) != len(aliases) or taken & set(aliases):
        raise ValueError("measure names (as) must be unique and differ from the group columns")

    raw_order = body.get("order_by", [])
    if not isinstance(raw_order, list) or len(raw_order) > MAX_ORDER_KEYS:
        raise ValueError(f"order_by must be a list of at most {MAX_ORDER_KEYS} keys")
    order: list[OrderKey] = []
    for item in raw_order:
        if not isinstance(item, dict) or not set(item) <= {"key", "direction"}:
            raise ValueError("each order_by entry is {key, direction}")
        direction = item.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise ValueError("order_by direction must be asc or desc")
        order.append(OrderKey(_name(item.get("key"), "order_by key"), direction == "desc"))
    if len({k.key for k in order}) != len(order):
        raise ValueError("a key may appear in order_by only once")

    limit = body.get("limit", DEFAULT_AGGREGATE_LIMIT)
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or not 1 <= limit <= MAX_AGGREGATE_LIMIT
    ):
        raise ValueError(f"limit must be an integer from 1 to {MAX_AGGREGATE_LIMIT}")

    plan = AggregatePlan(
        measures=measures,
        group_by=group_by,
        filters=parse_filters(body.get("filters", [])),
        order=tuple(order),
        limit=limit,
        unit_column=unit_column,
        unit_policy=cast(UnitPolicy, unit_policy),
    )
    orderable = set(plan.output_columns)
    unknown = [k.key for k in order if k.key not in orderable]
    if unknown:
        raise ValueError(f"order_by keys must be group columns or measure names: {unknown}")
    return plan


def _is_nested(dtype: pl.DataType) -> bool:
    return bool(dtype.is_nested()) or str(dtype).startswith("Object")


def check_aggregate_plan(plan: AggregatePlan, schema: Mapping[str, pl.DataType]) -> None:
    """Refuse a plan that names a missing column or aggregates a column it cannot."""
    reserved = sorted({_UNITS_N, _UNITS} & set(schema))
    if reserved:
        raise ValueError(f"the table has reserved column names: {reserved}")
    named = [
        *plan.group_by,
        *((plan.unit_column,) if plan.unit_column else ()),
        *(m.column for m in plan.measures if m.column is not None),
        *(f.column for f in plan.filters),
    ]
    missing = sorted({name for name in named if name not in schema})
    if missing:
        raise ValueError(f"no such columns: {missing}")
    for column in (*plan.group_by, *((plan.unit_column,) if plan.unit_column else ())):
        if _is_nested(schema[column]):
            raise ValueError(f"cannot group by {column!r}, a {schema[column]} column")
    for m in plan.measures:
        if m.column is None:
            continue
        dtype = schema[m.column]
        if _is_nested(dtype):
            raise ValueError(f"cannot aggregate {m.column!r}, a {dtype} column")
        if m.fn in _NUMERIC_FNS and not dtype.is_numeric():
            raise ValueError(f"{m.fn} needs a numeric column; {m.column!r} is {dtype}")
        if m.fn in _ORDERED_FNS and not (
            dtype.is_numeric() or dtype.is_temporal() or str(dtype) in ("String", "Utf8")
        ):
            raise ValueError(f"{m.fn} needs an ordered column; {m.column!r} is {dtype}")
    for f in plan.filters:
        for value in (f.value,) if f.op not in ("in", "is_null", "is_not_null") else f.values:
            typed_literal(value, schema[f.column])


def _measure_expr(measure: Measure) -> pl.Expr:
    import polars as pl

    if measure.fn == "count_rows":
        return pl.len().cast(pl.Int64).alias(measure.alias)
    column = pl.col(cast(str, measure.column))
    if measure.fn == "count":
        expr = column.count()
    elif measure.fn == "count_null":
        expr = column.null_count()
    elif measure.fn == "count_distinct":
        expr = column.drop_nulls().n_unique()
    elif measure.fn == "sum":
        # Polars sums an all-null group to 0; a sum of nothing is unknown, not zero.
        expr = pl.when(column.count() > 0).then(column.sum()).otherwise(None)
    elif measure.fn == "avg":
        expr = column.mean()
    elif measure.fn == "min":
        expr = column.min()
    else:
        expr = column.max()
    if measure.fn.startswith("count"):
        expr = expr.cast(pl.Int64)
    return expr.alias(measure.alias)


@dataclass(frozen=True)
class AggregateOutcome:
    """What the worker computed. ``refusal`` set means ``frame`` is empty."""

    frame: pl.DataFrame
    input_row_count: int
    group_count: int
    refusal: dict[str, JsonValue] | None = None


def run_aggregate(table_path: str, plan: AggregatePlan) -> AggregateOutcome:
    """Filter, aggregate every group, check units, then sort and take the top N."""
    import polars as pl

    frame = scan_builder_parquet(table_path)
    schema = dict(frame.collect_schema())
    check_aggregate_plan(plan, schema)
    predicate = filter_predicate(plan.filters, schema)
    if predicate is not None:
        frame = frame.filter(predicate)
    input_row_count = int(frame.select(pl.len()).collect().item())

    keys = list(plan.key_columns)
    expressions = [_measure_expr(m) for m in plan.measures]
    unit = plan.unit_column
    checks_unit = unit is not None and unit not in keys
    if checks_unit:
        unit_col = pl.col(cast(str, unit))
        units = unit_col.unique().sort(nulls_last=True).head(10)
        # n_unique counts null as a value: a group with one named unit and a missing one is mixed.
        expressions += [
            unit_col.n_unique().alias(_UNITS_N),
            (units if keys else units.implode()).alias(_UNITS),
            unit_col.first().alias(cast(str, unit)),
        ]
    if keys:
        result = frame.group_by(keys).agg(expressions).collect()
    else:
        # One group over every filtered row, also when there are none.
        result = frame.select(expressions).collect()
    group_count = result.height

    if group_count > MAX_GROUPS:
        return AggregateOutcome(
            result.clear(),
            input_row_count,
            group_count,
            {
                "code": "too_many_groups",
                "error": f"the aggregate has {group_count} groups; at most {MAX_GROUPS} "
                "are allowed before the top N is taken",
                "group_count": group_count,
                "max_groups": MAX_GROUPS,
            },
        )

    if checks_unit:
        mixed = result.filter(pl.col(_UNITS_N) > 1)
        if mixed.height:
            samples: list[JsonValue] = []
            for row in mixed.sort(keys, nulls_last=True).head(MIXED_UNIT_SAMPLES).to_dicts():
                samples.append(
                    {
                        "group": {k: _plain(row[k]) for k in keys},
                        "units": [_plain(u) for u in row[_UNITS]],
                    }
                )
            return AggregateOutcome(
                result.clear(),
                input_row_count,
                group_count,
                {
                    "code": "mixed_units",
                    "error": f"{mixed.height} of {group_count} groups mix values counted in "
                    f"different units of {unit!r}; aggregate them split by unit "
                    "(unit_policy: split) or filter to one unit",
                    "unit_column": unit,
                    "mixed_group_count": mixed.height,
                    "samples": samples,
                },
            )
        result = result.drop([_UNITS_N, _UNITS])

    # Sort every group, then cut: the top N of the whole aggregate, never an aggregate of
    # the first N rows. Ties are broken by the group keys so a re-run orders them alike.
    ordered = [k.key for k in plan.order]
    sort_keys = ordered + [k for k in keys if k not in ordered]
    descending = [k.descending for k in plan.order] + [False] * (len(sort_keys) - len(plan.order))
    if sort_keys:
        result = result.sort(sort_keys, descending=descending, nulls_last=True, maintain_order=True)
    result = result.select(list(plan.output_columns)).head(plan.limit)
    return AggregateOutcome(result, input_row_count, group_count)


def _plain(value: object) -> JsonValue:
    """A group key or unit as JSON: text for anything JSON cannot hold exactly."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and abs(value) <= 2**53 - 1:
        return value
    if isinstance(value, float):
        return value
    return str(value)


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)


def aggregate_worker(
    connection: Connection,
    table_path: str,
    plan_json: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    """``QueryEngine`` worker for an aggregate. Sends the groups and how many there were."""
    del limit  # the plan carries it
    try:
        startup_ms = _elapsed_ms(parent_started_ns)
        engine_started_ns = time.monotonic_ns()
        outcome = run_aggregate(table_path, AggregatePlan.from_json(plan_json))
        engine_execution_ms = _elapsed_ms(engine_started_ns)

        from ..tabular.polars_engine import infer_schema
        from ..tabular.wire import column_meta, encode_rows
        from .engine import MAX_QUERY_RESPONSE_BYTES

        page = outcome.frame
        columns = infer_schema(page).columns
        meta: dict[str, JsonValue] = {
            "input_row_count": outcome.input_row_count,
            "group_count": outcome.group_count,
            "refusal": outcome.refusal,
        }
        payload = {
            "ok": True,
            "columns": list(page.columns),
            "column_meta": column_meta(columns),
            "rows": list(encode_rows(page.to_dicts(), columns)),
            "truncated": page.height < outcome.group_count,
            "startup_ms": startup_ms,
            "engine_execution_ms": engine_execution_ms,
            "meta": meta,
        }
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        if size > MAX_QUERY_RESPONSE_BYTES:
            # Refused with its own code: a result cut to fit would look complete.
            payload.update(columns=[], column_meta=[], rows=[], truncated=False)
            meta["refusal"] = {
                "code": "result_too_large",
                "error": f"the aggregate result is {size} bytes; at most "
                f"{MAX_QUERY_RESPONSE_BYTES} can be sent. Lower limit or measures.",
                "max_bytes": MAX_QUERY_RESPONSE_BYTES,
            }
        connection.send(payload)
    except BaseException:
        # Engine messages can contain absolute parquet paths; never send them across.
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send({"ok": False})
    finally:
        connection.close()


__all__ = [
    "MAX_AGGREGATE_LIMIT",
    "MAX_GROUPS",
    "MAX_GROUP_BY",
    "MAX_MEASURES",
    "AggregateOutcome",
    "AggregatePlan",
    "Measure",
    "OrderKey",
    "aggregate_worker",
    "check_aggregate_plan",
    "parse_aggregate_plan",
    "run_aggregate",
]

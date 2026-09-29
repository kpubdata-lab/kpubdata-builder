"""Paged row reads over one table snapshot (#815).

A table screen needs pages, a sort, filters, a column selection and a row count, and it
needs them to agree from one page to the next. A client that assembles SQL to fake this
gets duplicated or missing rows where sort keys tie, and a count it cannot trust. This
module is the plan and the child-process worker that reads one page:

- **Stable order.** Every read is ordered by the requested sort keys and then by the
  row's position in the snapshot's table file, so rows that tie on every key keep one
  order and a page boundary never splits them differently on two requests. Nulls sort
  last in either direction. The position is an internal tie-breaker, valid inside one
  snapshot only; it is not sent, and it is not a key for editing a cell across snapshots.
- **Count with a status.** A count is either exact or not computed; a count that was not
  computed is null, never 0.
- **Same limits as a query.** The worker runs through ``QueryEngine`` — child process,
  timeout, memory cap, response size cap — and takes a slot from the same concurrency
  limit.

Values are sent with the same wire encoding as a query (#735): codes stay text with
their leading zeros, Decimals and large integers are exact decimal text, dates ISO 8601.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import TYPE_CHECKING, Literal, cast

from ..spec import JsonValue

if TYPE_CHECKING:
    import polars as pl

#: The column the tie-breaker is kept in while a page is read. Refused as a table column
#: name so it can never shadow real data.
ROW_ORDER_COLUMN = "__kpubdata_row_order"
MAX_PAGE_SIZE = 500
MAX_SORT_KEYS = 8
MAX_FILTERS = 16
MAX_IN_VALUES = 100

FilterOp = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "is_null", "is_not_null"]
_VALUE_OPS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
_NO_VALUE_OPS = frozenset({"is_null", "is_not_null"})
_ALL_OPS = _VALUE_OPS | _NO_VALUE_OPS | {"in"}
CountMode = Literal["exact", "none"]


@dataclass(frozen=True)
class SortKey:
    column: str
    descending: bool = False


@dataclass(frozen=True)
class RowFilter:
    column: str
    op: FilterOp
    value: JsonValue = None
    values: tuple[JsonValue, ...] = ()


@dataclass(frozen=True)
class RowsPlan:
    """One page to read. ``columns`` None means every column, in table order."""

    offset: int
    page_size: int
    columns: tuple[str, ...] | None = None
    sort: tuple[SortKey, ...] = ()
    filters: tuple[RowFilter, ...] = ()
    count: CountMode = "exact"

    def to_json(self) -> str:
        return json.dumps(
            {
                "offset": self.offset,
                "page_size": self.page_size,
                "columns": list(self.columns) if self.columns is not None else None,
                "sort": [{"column": k.column, "descending": k.descending} for k in self.sort],
                "filters": [
                    {"column": f.column, "op": f.op, "value": f.value, "values": list(f.values)}
                    for f in self.filters
                ],
                "count": self.count,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> RowsPlan:
        data = json.loads(raw)
        return RowsPlan(
            offset=int(data["offset"]),
            page_size=int(data["page_size"]),
            columns=tuple(data["columns"]) if data["columns"] is not None else None,
            sort=tuple(SortKey(k["column"], bool(k["descending"])) for k in data["sort"]),
            filters=tuple(
                RowFilter(f["column"], f["op"], f["value"], tuple(f["values"]))
                for f in data["filters"]
            ),
            count=data["count"],
        )


def _int(body: Mapping[str, JsonValue], key: str, default: int, low: int, high: int) -> int:
    value = body.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{key} must be an integer from {low} to {high}")
    return value


def _column_name(value: JsonValue, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty column name")
    return value


def parse_rows_plan(body: Mapping[str, JsonValue]) -> RowsPlan:
    """Read the paging, sort, filter, column and count fields of a request body.

    Shape only: whether the columns exist and the values fit their types is checked
    against the snapshot's schema by ``check_plan``.
    """
    offset = _int(body, "offset", 0, 0, 2**53 - 1)
    page_size = _int(body, "page_size", 100, 1, MAX_PAGE_SIZE)

    raw_columns = body.get("columns")
    columns: tuple[str, ...] | None = None
    if raw_columns is not None:
        if not isinstance(raw_columns, list) or not raw_columns:
            raise ValueError("columns must be a non-empty list of column names")
        columns = tuple(_column_name(c, "each column") for c in raw_columns)
        if len(set(columns)) != len(columns):
            raise ValueError("columns must not repeat")

    raw_sort = body.get("sort", [])
    if not isinstance(raw_sort, list) or len(raw_sort) > MAX_SORT_KEYS:
        raise ValueError(f"sort must be a list of at most {MAX_SORT_KEYS} keys")
    sort: list[SortKey] = []
    for item in raw_sort:
        if not isinstance(item, dict) or not set(item) <= {"column", "direction"}:
            raise ValueError("each sort key is {column, direction}")
        direction = item.get("direction", "asc")
        if direction not in ("asc", "desc"):
            raise ValueError("sort direction must be asc or desc")
        sort.append(SortKey(_column_name(item.get("column"), "sort column"), direction == "desc"))
    if len({k.column for k in sort}) != len(sort):
        raise ValueError("a column may appear in sort only once")

    raw_filters = body.get("filters", [])
    if not isinstance(raw_filters, list) or len(raw_filters) > MAX_FILTERS:
        raise ValueError(f"filters must be a list of at most {MAX_FILTERS} conditions")
    filters: list[RowFilter] = []
    for item in raw_filters:
        if not isinstance(item, dict) or not set(item) <= {"column", "op", "value", "values"}:
            raise ValueError("each filter is {column, op, value | values}")
        op = item.get("op")
        if op not in _ALL_OPS:
            raise ValueError(f"filter op must be one of {sorted(_ALL_OPS)}")
        column = _column_name(item.get("column"), "filter column")
        if op in _VALUE_OPS:
            if "value" not in item or item["value"] is None or "values" in item:
                raise ValueError(f"filter {op} needs one non-null value")
            filters.append(RowFilter(column, cast(FilterOp, op), value=item["value"]))
        elif op == "in":
            values = item.get("values")
            if (
                "value" in item
                or not isinstance(values, list)
                or not 1 <= len(values) <= MAX_IN_VALUES
                or any(v is None for v in values)
            ):
                raise ValueError(f"filter in needs 1 to {MAX_IN_VALUES} non-null values")
            filters.append(RowFilter(column, "in", values=tuple(values)))
        else:
            if "value" in item or "values" in item:
                raise ValueError(f"filter {op} takes no value")
            filters.append(RowFilter(column, cast(FilterOp, op)))

    count = body.get("count", "none" if filters else "exact")
    if count not in ("exact", "none"):
        raise ValueError("count must be exact or none")
    return RowsPlan(
        offset=offset,
        page_size=page_size,
        columns=columns,
        sort=tuple(sort),
        filters=tuple(filters),
        count=cast(CountMode, count),
    )


def typed_literal(value: JsonValue, dtype: pl.DataType) -> object:
    """``value`` as a value of the column's type, or ValueError when it does not fit.

    A client sends a Decimal or a large integer as its exact text and a date as ISO 8601
    — the way it received them — so text is cast to the column's type rather than
    compared as text.
    """
    import polars as pl

    if isinstance(value, (dict, list)):
        raise ValueError("a filter value must be a scalar")
    try:
        series = pl.Series([value]).cast(dtype, strict=True)
    except Exception as exc:  # polars raises several unrelated error types here
        raise ValueError(f"{value!r} is not a valid {dtype}") from exc
    if series.null_count():
        raise ValueError(f"{value!r} is not a valid {dtype}")
    return series.item()


def check_plan(plan: RowsPlan, schema: Mapping[str, pl.DataType]) -> None:
    """Refuse a plan that names a column the snapshot lacks or a value that cannot fit."""
    if ROW_ORDER_COLUMN in schema:
        raise ValueError(f"the table has a column named {ROW_ORDER_COLUMN}, which is reserved")
    named = [
        *(plan.columns or ()),
        *(k.column for k in plan.sort),
        *(f.column for f in plan.filters),
    ]
    missing = sorted({name for name in named if name not in schema})
    if missing:
        raise ValueError(f"no such columns: {missing}")
    for f in plan.filters:
        for value in (f.value,) if f.op in _VALUE_OPS else f.values:
            typed_literal(value, schema[f.column])


def _predicate(filters: Sequence[RowFilter], schema: Mapping[str, pl.DataType]) -> pl.Expr | None:
    import polars as pl

    expressions: list[pl.Expr] = []
    for f in filters:
        column = pl.col(f.column)
        dtype = schema[f.column]
        if f.op == "is_null":
            expressions.append(column.is_null())
        elif f.op == "is_not_null":
            expressions.append(column.is_not_null())
        elif f.op == "in":
            # Equalities rather than `is_in`, whose collection semantics changed across
            # the Polars 1.x range this package allows. At most MAX_IN_VALUES terms.
            expressions.append(
                pl.any_horizontal(
                    [column == pl.lit(typed_literal(v, dtype), dtype=dtype) for v in f.values]
                )
            )
        else:
            literal = pl.lit(typed_literal(f.value, dtype), dtype=dtype)
            expressions.append(
                {
                    "eq": column == literal,
                    "ne": column != literal,
                    "lt": column < literal,
                    "lte": column <= literal,
                    "gt": column > literal,
                    "gte": column >= literal,
                }[f.op]
            )
    if not expressions:
        return None
    combined = expressions[0]
    for expression in expressions[1:]:
        combined = combined & expression
    return combined


def read_page(table_path: str, plan: RowsPlan) -> tuple[pl.DataFrame, int | None, bool]:
    """Read one page: the rows, the filtered count (None when not asked for), more rows?"""
    import polars as pl

    frame = pl.scan_parquet(table_path)
    schema = dict(frame.collect_schema())
    check_plan(plan, schema)
    frame = frame.with_row_index(ROW_ORDER_COLUMN)
    predicate = _predicate(plan.filters, schema)
    if predicate is not None:
        frame = frame.filter(predicate)

    count: int | None = None
    if plan.count == "exact":
        count = int(frame.select(pl.len()).collect().item())

    if plan.sort:
        frame = frame.sort(
            [k.column for k in plan.sort] + [ROW_ORDER_COLUMN],
            descending=[k.descending for k in plan.sort] + [False],
            nulls_last=True,
            maintain_order=True,
        )
    # Without a sort the scan keeps file order, which is the row order itself.
    frame = frame.slice(plan.offset, plan.page_size + 1)
    columns = list(plan.columns) if plan.columns is not None else list(schema)
    page = frame.select(columns).collect()
    has_more = page.height > plan.page_size
    return page.head(plan.page_size), count, has_more


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)


def rows_worker(
    connection: Connection,
    table_path: str,
    plan_json: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    """``QueryEngine`` worker for a paged read. Sends the page, its count and ``has_more``."""
    del limit  # the plan carries the page size
    try:
        startup_ms = _elapsed_ms(parent_started_ns)
        engine_started_ns = time.monotonic_ns()
        page, count, has_more = read_page(table_path, RowsPlan.from_json(plan_json))
        engine_execution_ms = _elapsed_ms(engine_started_ns)

        from ..tabular.polars_engine import infer_schema
        from ..tabular.wire import column_meta, encode_rows
        from .engine import MAX_QUERY_RESPONSE_BYTES

        columns = infer_schema(page).columns
        payload = {
            "ok": True,
            "columns": list(page.columns),
            "column_meta": column_meta(columns),
            "rows": list(encode_rows(page.to_dicts(), columns)),
            "truncated": has_more,
            "startup_ms": startup_ms,
            "engine_execution_ms": engine_execution_ms,
            "meta": {"count": count, "has_more": has_more},
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_QUERY_RESPONSE_BYTES:
            connection.send({"ok": False})
        else:
            connection.send(payload)
    except BaseException:
        # Engine messages can contain absolute parquet paths; never send them across.
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send({"ok": False})
    finally:
        connection.close()


__all__ = [
    "MAX_PAGE_SIZE",
    "ROW_ORDER_COLUMN",
    "RowFilter",
    "RowsPlan",
    "SortKey",
    "check_plan",
    "parse_rows_plan",
    "read_page",
    "rows_worker",
    "typed_literal",
]

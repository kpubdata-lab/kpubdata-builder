"""Loading Bronze records into DuckDB with the types Builder has always given them (#869).

Silver's table used to be ``pl.DataFrame(records, infer_schema_length=None)``. What
Polars infers from the records *is* the contract — the dtypes in every schema.json,
manifest and API response. DuckDB's own JSON inference is different (it reads
``"2024-01-01"`` as a DATE, for one), so it is not used. Instead:

1. **Scan** (one pass): the raw-JSON checks of :class:`~.convert.RecordTypeScan`, and a
   type inference that reproduces Polars' — integers are Int64, or Int128 past its
   range, and Float64 when mixed with floats; a Decimal column takes the largest scale
   at precision 38; a fixed UTC offset becomes UTC; a column that is only null is Null;
   list and struct types are unified element by element, struct fields in first-seen
   order.
2. **Write a load file** (second pass): each record as JSON DuckDB can read into the
   declared types without guessing, under internal column names ``c0``, ``c1``, …
3. **Read** it with ``read_json`` and every column type declared, then convert what
   JSON cannot carry directly (binary arrives as hex).

The result is a :class:`LoadedTable`: the physical table, and per column its name and
its **logical** dtype — the Builder dtype (ADR 0021 D3). The two differ only where DuckDB
cannot store the type Polars inferred: a Null column is stored as INTEGER, a Duration as
microseconds, a zoned datetime as UTC wall time, an empty struct as a flag. Reading rows
back (:func:`fetch_rows`) undoes that, so values come out as Polars gave them.

Internal column names keep source names that SQL cannot hold side by side (``Name`` and
``name``, #868) apart until the declared renames have run.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import duckdb

from ..errors import TabularError
from ..spec import JsonValue
from .convert import RecordTypeScan, apply_read_as
from .duckdb_runtime import ROW_SEQ_COLUMN, TabularRelation, reserve_row_seq
from .sql import quote_identifier, quote_literal

#: A type as inferred: ``("int",)``, ``("decimal", 2)``, ``("list", node)``,
#: ``("struct", {name: node})``, ``("datetime", zone)`` …
Node = tuple[Any, ...]

_NULL: Node = ("null",)
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1


class _TypeConflict(TabularError):
    """Two values of one column have types that do not unify."""


def _infer(value: object) -> Node:
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return ("bool",)
    if isinstance(value, int):
        return ("int",) if _INT64_MIN <= value <= _INT64_MAX else ("int128",)
    if isinstance(value, float):
        return ("float",)
    if isinstance(value, str):
        return ("str",)
    if isinstance(value, Decimal):
        exponent = value.as_tuple().exponent
        return ("decimal", -exponent if isinstance(exponent, int) and exponent < 0 else 0)
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return ("datetime", None)
        return ("datetime", value.tzinfo.key if isinstance(value.tzinfo, ZoneInfo) else "UTC")
    if isinstance(value, dt.date):
        return ("date",)
    if isinstance(value, dt.time):
        return ("time",)
    if isinstance(value, dt.timedelta):
        return ("duration",)
    if isinstance(value, bytes):
        return ("binary",)
    if isinstance(value, (list, tuple)):
        inner = _NULL
        for item in value:
            inner = _unify(inner, _infer(item))
        return ("list", inner)
    if isinstance(value, Mapping):
        return ("struct", {str(k): _infer(v) for k, v in value.items()})
    raise TabularError(f"unsupported value type in a record: {type(value).__name__}")


def _unify(a: Node, b: Node) -> Node:
    if a == _NULL:
        return b
    if b == _NULL or a == b:
        return a
    kinds = {a[0], b[0]}
    if kinds <= {"int", "int128"}:
        return ("int128",)
    if kinds <= {"int", "int128", "float"}:
        return ("float",)
    if a[0] == b[0] == "decimal":
        return ("decimal", max(a[1], b[1]))
    if a[0] == b[0] == "list":
        return ("list", _unify(a[1], b[1]))
    if a[0] == b[0] == "struct":
        merged = dict(a[1])
        for name, node in b[1].items():
            merged[name] = _unify(merged.get(name, _NULL), node)
        return ("struct", merged)
    if a[0] == b[0] == "datetime":
        raise TabularError(
            f"a column mixes datetimes in different time zones: {a[1]!r} and {b[1]!r}"
        )
    raise _TypeConflict(
        f"heterogeneous column types detected (refusing to silently coerce): {a} {b}"
    )


def canonical(node: Node) -> str:
    """The Builder dtype a node is — spelt as ``dtypes.canonical_dtype`` spells it."""
    kind = node[0]
    simple = {
        "null": "Null",
        "bool": "Boolean",
        "int": "Int64",
        "int128": "Int128",
        "float": "Float64",
        "str": "String",
        "date": "Date",
        "time": "Time",
        "binary": "Binary",
        "duration": "Duration(time_unit='us')",
    }
    if kind in simple:
        return simple[kind]
    if kind == "decimal":
        return f"Decimal(precision=38, scale={node[1]})"
    if kind == "datetime":
        return f"Datetime(time_unit='us', time_zone={node[1]!r})"
    if kind == "list":
        return f"List({canonical(node[1])})"
    fields = ", ".join(f"{name!r}: {canonical(child)}" for name, child in node[1].items())
    return f"Struct({{{fields}}})"


def _physical(node: Node, *, top: bool) -> str:
    """The DuckDB type a node is stored as."""
    kind = node[0]
    simple = {
        "null": "INTEGER",
        "bool": "BOOLEAN",
        "int": "BIGINT",
        "int128": "HUGEINT" if top else "",
        "float": "DOUBLE",
        "str": "VARCHAR",
        "date": "DATE",
        "time": "TIME",
        "duration": "BIGINT",
        "datetime": "TIMESTAMP",
    }
    if kind in simple:
        return simple[kind]
    if kind == "decimal":
        return f"DECIMAL(38,{node[1]})"
    if kind == "int128" and not top:
        raise TabularError(
            "integers beyond 64 bits inside a list or struct are not supported: "
            "they cannot be written without rounding"
        )
    if kind == "binary":
        if not top:
            raise TabularError("binary values inside a list or struct are not supported")
        return "BLOB"
    if kind == "list":
        return f"{_physical(node[1], top=False)}[]"
    if not node[1]:
        if not top:
            raise TabularError("an empty struct inside a list or struct is not supported")
        return "BOOLEAN"
    folded: dict[str, list[str]] = {}
    for name in node[1]:
        folded.setdefault(name.casefold(), []).append(name)
    clashes = sorted(sorted(names) for names in folded.values() if len(names) > 1)
    if clashes:
        raise TabularError(
            f"struct fields differ only in letter case, which SQL reads as one field: {clashes}"
        )
    fields = ", ".join(
        f"{quote_identifier(name)} {_physical(child, top=False)}" for name, child in node[1].items()
    )
    return f"STRUCT({fields})"


def _load_type(node: Node) -> str:
    """The type ``read_json`` reads the load file's column as (binary comes as hex)."""
    return "VARCHAR" if node[0] == "binary" else _physical(node, top=True)


def _encode(node: Node, value: object) -> object:
    """A value as the load file holds it for its column's type."""
    if value is None:
        return None
    kind = node[0]
    if kind == "float" or (kind in ("int", "int128") and isinstance(value, float)):
        number = float(cast(float, value))
        if math.isnan(number):
            return "nan"
        if math.isinf(number):
            return "inf" if number > 0 else "-inf"
        return number
    if kind == "int128" or kind == "decimal":
        return str(value)
    if kind == "datetime":
        moment = cast(dt.datetime, value)
        if moment.tzinfo is not None:
            moment = moment.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return moment.isoformat(sep=" ")
    if kind in ("date", "time"):
        return cast(dt.date, value).isoformat()
    if kind == "duration":
        return cast(dt.timedelta, value) // dt.timedelta(microseconds=1)
    if kind == "binary":
        return cast(bytes, value).hex()
    if kind == "list":
        return [_encode(node[1], item) for item in cast(Sequence[object], value)]
    if kind == "struct":
        mapping = cast(Mapping[str, object], value)
        if not node[1]:
            return True
        return {name: _encode(child, mapping.get(name)) for name, child in node[1].items()}
    return value


def _decode(node: Node, value: object) -> object:
    """A value read back from DuckDB, as Polars would have given it."""
    if value is None:
        return None
    kind = node[0]
    if kind == "null":
        return None
    if kind == "datetime" and node[1] is not None:
        return (
            cast(dt.datetime, value).replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo(node[1]))
        )
    if kind == "duration":
        return dt.timedelta(microseconds=cast(int, value))
    if kind == "list":
        return [_decode(node[1], item) for item in cast(Sequence[object], value)]
    if kind == "struct":
        if not node[1]:
            return {}
        mapping = cast(Mapping[str, object], value)
        return {name: _decode(child, mapping.get(name)) for name, child in node[1].items()}
    return value


@dataclass(frozen=True)
class LoadedTable:
    """A table of records in DuckDB: physical columns ``c0…`` and what each one is.

    Every loaded table also carries the row ordinal ``ROW_SEQ_COLUMN`` (ADR 0021 D8):
    source order, numbered at load. It is not one of the columns — not in ``names``,
    ``physical`` or ``dtypes``, and not in any schema, statistic or distinct count — and
    every read whose order matters sorts by it (:data:`order_by`).
    """

    relation: TabularRelation
    names: tuple[str, ...]
    physical: tuple[str, ...]
    dtypes: tuple[str, ...]
    row_count: int
    nodes: tuple[Node, ...]

    def column(self, name: str) -> str:
        """The physical (quoted) column holding ``name``."""
        return quote_identifier(self.physical[self.names.index(name)])

    @property
    def order_by(self) -> str:
        """``ORDER BY`` clause restoring source order."""
        return f"ORDER BY {quote_identifier(ROW_SEQ_COLUMN)}"


def derive_table(
    connection: duckdb.DuckDBPyConnection,
    source: LoadedTable,
    *,
    into: str,
    columns: Sequence[tuple[str, str, Node]],
) -> LoadedTable:
    """A new table ``into`` from ``source``: one column per ``(name, sql, node)``.

    ``sql`` is an expression over ``source``'s physical columns (see
    :meth:`LoadedTable.column`); ``node`` is the new column's type. Rows keep their order.
    """
    physical = tuple(f"c{i}" for i in range(len(columns)))
    relation = TabularRelation(into)
    # The row ordinal travels with every derived table (ADR 0021 D8).
    select = ", ".join(
        [quote_identifier(ROW_SEQ_COLUMN)]
        + [
            f"{sql} AS {quote_identifier(name)}"
            for name, (_, sql, _) in zip(physical, columns, strict=True)
        ]
    )
    connection.execute(
        f"CREATE OR REPLACE TABLE {relation.sql} AS SELECT {select} FROM {source.relation.sql}"
    )
    nodes = tuple(node for _, _, node in columns)
    return LoadedTable(
        relation=relation,
        names=tuple(name for name, _, _ in columns),
        physical=physical,
        dtypes=tuple(canonical(n) for n in nodes),
        row_count=source.row_count,
        nodes=nodes,
    )


def node_of(target: str) -> Node:
    """The node of a column after a declared cast to ``target`` (see ``duckdb_casts``)."""
    name = target.strip().lower()
    return {
        "int": ("int",),
        "int64": ("int",),
        "int_comma": ("int",),
        "float": ("float",),
        "float64": ("float",),
        "float_comma": ("float",),
        "str": ("str",),
        "string": ("str",),
        "utf8": ("str",),
        "year_month": ("str",),
        "date": ("date",),
        "datetime": ("datetime", None),
        "bool": ("bool",),
        "boolean": ("bool",),
    }[name]


@dataclass(eq=False)
class TableHandle:
    """A table in an open DuckDB connection, for the stages that read it (#869).

    The connection belongs to whoever opened it (the build's source pipeline, a preview);
    the handle never closes it. ``workdir`` is where exports of the table are spilled.
    ``cache`` holds what a caller derived from the table and may need after the
    connection closes (the Polars bridge keeps its frame there).
    """

    connection: duckdb.DuckDBPyConnection
    table: LoadedTable
    workdir: Path
    cache: dict[str, object] = field(default_factory=dict)

    @property
    def columns(self) -> tuple[str, ...]:
        return self.table.names

    @property
    def dtypes(self) -> tuple[str, ...]:
        return self.table.dtypes

    @property
    def height(self) -> int:
        return self.table.row_count

    def rows(self, *, limit: int) -> tuple[dict[str, object], ...]:
        """Up to ``limit`` rows in source order."""
        return fetch_rows(self.connection, self.table, limit=limit)

    def rows_at(self, indices: Sequence[int]) -> tuple[dict[str, object], ...]:
        """The rows at ``indices``, in the order asked for."""
        out: list[dict[str, object]] = []
        for index in indices:
            out.extend(fetch_rows(self.connection, self.table, limit=1, offset=index))
        return tuple(out)

    def write_parquet(self, path: Path, *, physical_names: bool = False) -> None:
        """The table as Parquet, rows in order.

        Under its real column names by default. ``physical_names`` keeps the internal
        ``c0…`` names instead, for readers that rename themselves (the Polars bridge),
        and writes Int128 as exact text for them to read back.

        DuckDB writes a HUGEINT to Parquet as a double, so an Int128 column is written as
        ``DECIMAL(38,0)`` — or as text when a value has more than 38 digits — never
        rounded. A zoned datetime is written as an instant (``TIMESTAMPTZ``).

        Raises:
            ValueError: The table has no columns.
        """
        if not self.table.physical:
            raise ValueError("a table without columns cannot be written as Parquet")
        parts = []
        for physical, name, node in zip(
            self.table.physical, self.table.names, self.table.nodes, strict=True
        ):
            column = quote_identifier(physical)
            if node[0] == "int128":
                column = (
                    f"CAST({column} AS VARCHAR)"
                    if physical_names or not self._fits_decimal(physical)
                    else f"CAST({column} AS DECIMAL(38,0))"
                )
            elif node[0] == "datetime" and node[1] is not None and not physical_names:
                column = f"CAST({column} AS TIMESTAMPTZ)"
            alias = physical if physical_names else name
            parts.append(f"{column} AS {quote_identifier(alias)}")
        # DuckDB 1.2 takes no bound parameter as a COPY target: the path — Builder's
        # own, under the run — goes in as a quoted literal.
        self.connection.execute(
            f"COPY (SELECT {', '.join(parts)} FROM {self.table.relation.sql} "
            f"{self.table.order_by}) TO {quote_literal(str(path))} (FORMAT PARQUET)"
        )

    def _fits_decimal(self, physical: str) -> bool:
        column = quote_identifier(physical)
        row = self.connection.execute(
            f"SELECT coalesce(max(abs({column})) < CAST('1e38' AS DOUBLE), true) "
            f"FROM {self.table.relation.sql}"
        ).fetchone()
        return bool(row and row[0])


def load_records(
    connection: duckdb.DuckDBPyConnection,
    records: Callable[[], Iterable[dict[str, JsonValue]]],
    *,
    table: str,
    workdir: Path,
    read_as: Mapping[str, str] | None = None,
) -> LoadedTable:
    """Load ``records`` — a callable giving a fresh iterator each time; it is read twice —
    into ``table``.

    ``connection`` is expected to come from ``duckdb_runtime`` (``build_connection``:
    memory limit, threads, UTC, the run's temp directory), and ``workdir`` to be under the
    run's own directory; the load file lives there only while loading.

    Raises:
        TabularError: The records mix types, or risk integer precision, exactly as
            ``records_to_dataframe`` would refuse them; or hold a type DuckDB cannot store.
        ReservedColumnError: A source column is named ``_kpubdata_row_seq`` (any case).
    """
    scan = RecordTypeScan(read_as=read_as)
    nodes: dict[str, Node] = {}
    count = 0
    conflicted: set[str] = set()
    for record in records():
        checked = scan.add(record)
        for key, value in checked.items():
            if key in conflicted:
                continue
            try:
                nodes[key] = _unify(nodes.get(key, _NULL), _infer(value))
            except _TypeConflict:
                # The scan words this refusal as Silver always has; let it raise it.
                conflicted.add(key)
                nodes.setdefault(key, _NULL)
        count += 1
    scan.check()
    if conflicted:
        raise TabularError(
            "heterogeneous column types detected (refusing to silently coerce): "
            f"{sorted(conflicted)}"
        )
    reserve_row_seq(nodes)
    names = tuple(nodes)
    physical = tuple(f"c{i}" for i in range(len(names)))
    node_list = tuple(nodes[n] for n in names)
    relation = TabularRelation(table)
    load_path = workdir / f".{table}.load.jsonl"
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        with load_path.open("w", encoding="utf-8") as handle:
            for ordinal, record in enumerate(records()):
                checked = apply_read_as(record, read_as) if read_as else record
                row: dict[str, object] = {ROW_SEQ_COLUMN: ordinal}
                for index, name in enumerate(names):
                    value = checked.get(name)
                    if value is not None:
                        row[physical[index]] = _encode(node_list[index], value)
                handle.write(json.dumps(row, ensure_ascii=False))
                handle.write("\n")
        _create_table(connection, relation, load_path, physical, node_list)
    finally:
        load_path.unlink(missing_ok=True)
    return LoadedTable(
        relation=relation,
        names=names,
        physical=physical,
        dtypes=tuple(canonical(n) for n in node_list),
        row_count=count,
        nodes=node_list,
    )


def _create_table(
    connection: duckdb.DuckDBPyConnection,
    relation: TabularRelation,
    load_path: Path,
    physical: Sequence[str],
    nodes: Sequence[Node],
) -> None:
    # The column types are a bound parameter, not SQL text: struct field names are
    # source data (#869 review), and ``sql.py``'s rule is values as parameters.
    declared: dict[str, str] = {ROW_SEQ_COLUMN: "BIGINT"}
    declared.update((name, _load_type(node)) for name, node in zip(physical, nodes, strict=True))
    select = ", ".join(
        [quote_identifier(ROW_SEQ_COLUMN)]
        + [
            f"unhex({quote_identifier(name)}) AS {quote_identifier(name)}"
            if node[0] == "binary"
            else quote_identifier(name)
            for name, node in zip(physical, nodes, strict=True)
        ]
    )
    connection.execute(
        f"CREATE OR REPLACE TABLE {relation.sql} AS SELECT {select} FROM read_json(?, "
        "format = 'newline_delimited', records = 'true', columns = ?)",
        [str(load_path), declared],
    )


def fetch_rows(
    connection: duckdb.DuckDBPyConnection,
    loaded: LoadedTable,
    *,
    limit: int,
    offset: int = 0,
) -> tuple[dict[str, object], ...]:
    """Up to ``limit`` rows from ``offset``, in source order, as Polars' ``to_dicts``
    would give them. The limit is required: rows come into Python memory."""
    if not loaded.physical:
        return tuple({} for _ in range(max(0, min(limit, loaded.row_count - offset))))
    columns = ", ".join(quote_identifier(p) for p in loaded.physical)
    rows = connection.execute(
        f"SELECT {columns} FROM {loaded.relation.sql} {loaded.order_by} LIMIT ? OFFSET ?",
        [limit, offset],
    ).fetchall()
    return tuple(
        {
            name: _decode(node, value)
            for name, node, value in zip(loaded.names, loaded.nodes, row, strict=True)
        }
        for row in rows
    )


__all__ = [
    "LoadedTable",
    "Node",
    "TableHandle",
    "canonical",
    "derive_table",
    "fetch_rows",
    "load_records",
    "node_of",
]

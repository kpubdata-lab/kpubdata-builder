"""A DuckDB table as the Polars frame the not-yet-migrated stages read (#869).

Silver runs on DuckDB; Gold, quality and composition still read a ``pl.DataFrame`` until
their own steps (#870, #872) move them. This bridge gives them the frame Silver used to
hold, **exactly**: the same column names, order, dtypes and values. The table is written
as Parquet (which Polars reads without pyarrow) and the columns DuckDB stores
differently from their Builder dtype — a Null column stored as INTEGER, a zoned datetime
as UTC wall time, a Duration as microseconds, an empty struct as a flag — are turned
back into that dtype.

The frame is cached on the handle, so a caller that needs it after the source's
connection has closed (composition) reads it from there. The bridge goes away with the
last Polars stage (#876).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from kpubdata_builder.tabular.duckdb_load import Node, TableHandle
from kpubdata_builder.tabular.duckdb_runtime import ROW_SEQ_COLUMN, TabularRelation
from kpubdata_builder.tabular.sql import quote_identifier

_FRAME = "polars_frame"


def polars_dtype(node: Node) -> pl.DataType:
    """The Polars dtype a loader node stands for."""
    kind = node[0]
    simple: dict[str, pl.DataType] = {
        "null": pl.Null(),
        "bool": pl.Boolean(),
        "int": pl.Int64(),
        "int128": pl.Int128(),
        "float": pl.Float64(),
        "str": pl.String(),
        "date": pl.Date(),
        "time": pl.Time(),
        "binary": pl.Binary(),
        "duration": pl.Duration("us"),
    }
    if kind in simple:
        return simple[kind]
    if kind == "decimal":
        return pl.Decimal(38, node[1])
    if kind == "datetime":
        return pl.Datetime("us", node[1])
    if kind == "list":
        return pl.List(polars_dtype(node[1]))
    return pl.Struct({name: polars_dtype(child) for name, child in node[1].items()})


def _restore(series: pl.Series, node: Node) -> pl.Series:
    target = polars_dtype(node)
    if series.dtype == target:
        return series
    if node[0] == "struct" and not node[1]:
        # Stored as a flag: true for {} and null for null.
        return pl.Series(series.name, [{} if v else None for v in series.to_list()])
    if node[0] == "int128":
        return pl.Series(
            series.name, [None if v is None else int(v) for v in series.to_list()], dtype=target
        )
    if node[0] in ("null", "duration", "datetime"):
        return series.cast(target)
    # Nested types holding Null parts (List(Null), Struct({'x': Null})): rebuilt from
    # their values, which Polars will not cast into a Null part.
    return pl.Series(series.name, series.to_list(), dtype=target)


def to_polars(handle: TableHandle, *, keep: bool = False) -> pl.DataFrame:
    """The table as a Polars frame with its Builder dtypes.

    ``keep`` holds the frame on the handle, for a caller that reads it after the table's
    connection has closed (composition); otherwise nothing is held beyond the call. A
    kept frame is returned by later calls.
    """
    cached = handle.cache.get(_FRAME)
    if isinstance(cached, pl.DataFrame):
        return cached
    table = handle.table
    if not table.physical:
        frame = pl.DataFrame()
    else:
        handle.workdir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=handle.workdir, prefix=".frame-") as spill:
            path = Path(spill) / "table.parquet"
            handle.write_parquet(path, physical_names=True)
            read = pl.read_parquet(path)
        # Renamed here, not in SQL: names SQL cannot hold side by side (``Name`` and
        # ``name``, or an empty name) are fine in a frame.
        frame = read.rename(dict(zip(table.physical, table.names, strict=True)))
        frame = frame.with_columns(
            _restore(frame.get_column(name), node)
            for name, node in zip(table.names, table.nodes, strict=True)
        )
    if keep:
        handle.cache[_FRAME] = frame
    return frame


def alongside(handle: TableHandle, other: TableHandle) -> TableHandle:
    """``handle``'s table copied into ``other``'s connection, so SQL can read both (#870).

    For library callers that bring tables from separate connections; a build loads both
    sides of a composition into one connection to begin with.
    """
    return handle_from_frame(to_polars(handle), connection=other._open(), workdir=other.workdir)


def write_table_parquet(handle: TableHandle, path: Path) -> None:
    """``handle``'s table as Parquet, written by DuckDB (#869, #870).

    A table with no columns (a source that returned nothing) is written through the
    Polars frame: DuckDB cannot write a Parquet file without columns.
    """
    if handle.table.physical:
        handle.write_parquet(path)
    else:
        to_polars(handle).write_parquet(path)


def node_of_polars(dtype: Any) -> Node:
    """The loader node for a Polars dtype — the inverse of :func:`polars_dtype`.

    Raises:
        TypeError: Builder has no dtype for it.
    """
    if isinstance(dtype, type):
        dtype = dtype()
    simple: dict[type[pl.DataType], Node] = {
        pl.Null: ("null",),
        pl.Boolean: ("bool",),
        pl.Int64: ("int",),
        pl.Int128: ("int128",),
        pl.Float64: ("float",),
        pl.String: ("str",),
        pl.Date: ("date",),
        pl.Time: ("time",),
        pl.Binary: ("binary",),
    }
    for kind, node in simple.items():
        if isinstance(dtype, kind):
            return node
    if isinstance(dtype, pl.Duration):
        return ("duration",)
    if isinstance(dtype, pl.Decimal):
        return ("decimal", dtype.scale)
    if isinstance(dtype, pl.Datetime):
        return ("datetime", dtype.time_zone)
    if isinstance(dtype, pl.List):
        return ("list", node_of_polars(dtype.inner))
    if isinstance(dtype, pl.Struct):
        return ("struct", {field.name: node_of_polars(field.dtype) for field in dtype.fields})
    raise TypeError(f"no Builder dtype for Polars dtype {dtype}")


def _as_stored(column: pl.Expr, node: Node) -> pl.Expr:
    """A column as the loader stores it (see ``duckdb_load``)."""
    if node[0] == "null":
        return column.cast(pl.Int32)
    if node[0] == "duration":
        return column.dt.total_microseconds()
    if node[0] == "int128":
        # Parquet has no 128-bit integer Polars writes and DuckDB reads as one: carried
        # as exact text and cast to HUGEINT on load.
        return column.cast(pl.String)
    return column.dt.convert_time_zone("UTC").dt.replace_time_zone(None)


def _loaded(physical: str, node: Node) -> str:
    """A spilled column as the table holds it (the reverse of :func:`_as_stored`'s text)."""
    column = quote_identifier(physical)
    if node[0] == "int128":
        return f"CAST({column} AS HUGEINT) AS {column}"
    return column


def handle_from_frame(
    frame: pl.DataFrame, *, connection: duckdb.DuckDBPyConnection | None = None, workdir: Path
) -> TableHandle:
    """A DuckDB table holding ``frame`` — for callers that already hold a frame
    (library use, tests). The frame itself is kept as the handle's Polars view. Without
    ``connection`` a private one is opened through the runtime, and the handle closes it."""
    from kpubdata_builder.tabular.duckdb_load import LoadedTable, canonical

    owns_connection = connection is None
    if connection is None:
        from kpubdata_builder.tabular.duckdb_runtime import BuildProfile, connect

        temp = workdir / ".duckdb_tmp"
        temp.mkdir(parents=True, exist_ok=True)
        connection = connect(BuildProfile.from_env(), temp)
    nodes = tuple(node_of_polars(dtype) for dtype in frame.dtypes)
    physical = tuple(f"c{i}" for i in range(frame.width))
    workdir.mkdir(parents=True, exist_ok=True)
    relation = TabularRelation(f"frame_{id(frame)}")
    seq = quote_identifier(ROW_SEQ_COLUMN)
    if frame.width:
        with tempfile.TemporaryDirectory(dir=workdir, prefix=".frame-") as spill:
            path = Path(spill) / "frame.parquet"
            renamed = frame.rename(dict(zip(frame.columns, physical, strict=True)))
            # Types DuckDB stores differently are stored the way the loader stores them.
            renamed = renamed.with_columns(
                _as_stored(pl.col(p), node)
                for p, node in zip(physical, nodes, strict=True)
                if node[0] in ("null", "duration", "int128")
                or (node[0] == "datetime" and node[1] is not None)
            )
            renamed.write_parquet(path)
            # The frame's row order is its source order: the file row number is the
            # ordinal (ADR 0021 D8).
            connection.execute(
                f"CREATE OR REPLACE TABLE {relation.sql} AS SELECT file_row_number AS {seq}, "
                f"{', '.join(_loaded(p, node) for p, node in zip(physical, nodes, strict=True))} "
                "FROM read_parquet(?, file_row_number = true)",
                [str(path)],
            )
    else:
        connection.execute(
            f"CREATE OR REPLACE TABLE {relation.sql} AS SELECT i AS {seq} FROM range(?) t(i)",
            [frame.height],
        )
    loaded = LoadedTable(
        relation=relation,
        names=tuple(frame.columns),
        physical=physical,
        dtypes=tuple(canonical(n) for n in nodes),
        row_count=frame.height,
        nodes=nodes,
    )
    return TableHandle(
        connection, loaded, workdir, owns_connection=owns_connection, cache={_FRAME: frame}
    )


__all__ = [
    "alongside",
    "handle_from_frame",
    "node_of_polars",
    "polars_dtype",
    "to_polars",
    "write_table_parquet",
]

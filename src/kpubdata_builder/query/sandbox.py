"""A locked DuckDB connection over one snapshot's table file (#874, ADR 0021 D6).

The query child opens one of these and nothing else. It is the second of three
layers — the SQL validator (``security``) before it, the child process (``engine``)
around it — and does not rely on the other two:

- **One file at most, one directory.** An unmasked connection may read the pinned
  ``table.parquet`` (``allowed_paths``); a masked one may read no file at all (below).
  Either may spill into its own temporary directory (``allowed_directories``); every
  other file system, network or extension access is refused
  (``enable_external_access = false``), no extension is installed or loaded, and no
  Python variable is a table (``python_enable_replacements = false``).
- **Limits.** Threads, buffer memory and the spill quota come from the deployment
  (``BuildProfile.from_env``, #701); the time zone is UTC.
- **Locked.** ``lock_configuration`` is set last, so no statement can loosen any of it.
- **One relation.** ``dataset`` is a view of the file with the columns given back their
  Builder dtypes and names from the file's key-value metadata (``builder_kv``): an
  Int128 is a HUGEINT, a Duration an INTERVAL, a Null column null (DuckDB types it
  INTEGER once projected). A table with a column SQL cannot name — an empty name, or two
  names one letter case apart — gets no ``dataset``, and user SQL over it fails.
  ``_kpubdata_dataset_ordered`` names every column ``_c<i>`` (:meth:`Sandbox.alias`)
  and adds the row's position in the file, for the reads Builder writes itself (rows,
  aggregates, profiles) and for reads whose order is a contract (ADR 0021 D8). User SQL
  may name only ``dataset`` (the validator).
- **Masked columns.** A Silver read names the declared PII columns it must not show
  (#900). Their original values never enter the connection (#1331): the file is read
  by a pyarrow scanner that applies Gold's mask — text becomes the mask token with its
  nulls kept, any other dtype null — and DuckDB scans that masked Arrow stream, read
  afresh for every scan, in place of the file. Both views read it and cast each column
  back to the type DuckDB gives it in the file, so a masked column keeps its type and
  every other column reads as it would unmasked. The file's path is then taken out of
  ``allowed_paths``, so no SQL — ``read_parquet``, a quoted path, ``parquet_metadata``
  and its statistics — reaches the raw file, whether or not the validator ran. Nothing
  table-sized is written: the mask is applied to the rows a query reads, inside the
  child's timeout and address-space cap (pyarrow's buffers sit outside DuckDB's
  ``memory_limit``, inside the cap).

Paths are written into SQL only through ``quote_literal`` and come from the server,
never from a request.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import duckdb

from ..stages.gold.pii import PII_MASK_TOKEN
from ..tabular.builder_kv import KV_KEY, KV_NAMES_KEY, NO_COLUMNS, NO_COLUMNS_KEY
from ..tabular.duckdb_runtime import BuildProfile
from ..tabular.sql import quote_identifier, quote_literal

if TYPE_CHECKING:
    import pyarrow.dataset as pads

DATASET = "dataset"
#: ``dataset`` with :data:`ROW_ORDER` — the row's position in the file — added.
ORDERED_DATASET = "_kpubdata_dataset_ordered"
#: The row position column of :data:`ORDERED_DATASET`; refused as a table column name.
ROW_ORDER = "__kpubdata_row_order"
#: The column DuckDB adds for a row's position in the file, compared case-folded.
_FILE_ROW_NUMBER = "file_row_number"
#: Set by the query engine in the child: the temporary directory it made for this query.
TEMP_DIR_ENV = "KPUBDATA_QUERY_TEMP_DIR"
#: Set by the query engine in the child: a JSON list of the columns to mask (#1331).
MASKED_COLUMNS_ENV = "KPUBDATA_QUERY_MASKED_COLUMNS"
#: The masked Arrow stream a masked sandbox's views read in place of the file (#1331).
_MASKED_SOURCE = "_kpubdata_masked_source"
#: Rows per batch of the masked stream: a page reads a few batches, not a row group.
_MASKED_BATCH_ROWS = 16_384
#: Batches the masked stream reads ahead of DuckDB.
_MASKED_READAHEAD = 4


@dataclass(frozen=True)
class Sandbox:
    """An open locked connection and what the file says about its columns."""

    connection: duckdb.DuckDBPyConnection
    #: Column name → Builder dtype, as the file records them; empty when it records none.
    dtypes: dict[str, str] = field(default_factory=dict)
    #: The columns' real names, in file order.
    columns: tuple[str, ...] = ()
    #: Why ``dataset`` could not be created (a column SQL cannot name), or None.
    dataset_refused: str | None = None

    def alias(self, name: str) -> str:
        """The quoted column of :data:`ORDERED_DATASET` holding ``name``.

        The internal view names every column ``_c<i>``, so a column whose real name SQL
        cannot hold — empty, or one letter case away from another — is still read.
        """
        return quote_identifier(f"_c{self.columns.index(name)}")


def _kv(connection: duckdb.DuckDBPyConnection, path: str) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    for key, value in connection.execute(
        "SELECT key, value FROM parquet_kv_metadata(?)", [path]
    ).fetchall():
        name = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        raw = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        if name == NO_COLUMNS_KEY:
            # A table without columns is written with a placeholder the file marks as none.
            found[name] = {"": raw}
            continue
        if name not in (KV_KEY, KV_NAMES_KEY):
            continue
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            found[name] = {str(k): str(v) for k, v in decoded.items()}
    return found


def _is_text(storage: str, dtype: str | None) -> bool:
    """Whether Gold masks the column as text (#902): its dtype, or VARCHAR without one."""
    return (dtype if dtype is not None else ("String" if storage == "VARCHAR" else "")) == "String"


class _MaskedArrowSource:
    """One Parquet file's columns with Gold's mask applied, as an Arrow stream (#1331).

    DuckDB asks for a fresh stream (``__arrow_c_stream__``) for every scan, so a query
    that reads ``dataset`` twice — a self-join, a reused CTE, the planning read before
    the streamed one (#1332) — reads the file twice rather than an exhausted stream.
    DuckDB also asks while it binds a query, only for the schema, so a stream reads
    ahead little: a page costs a few batches, not row groups.
    """

    def __init__(self, dataset: pads.Dataset, columns: Mapping[str, pads.Expression]) -> None:
        self._dataset = dataset
        self._columns = dict(columns)

    def __arrow_c_stream__(self, requested_schema: object | None = None) -> object:
        import pyarrow as pa

        scanner = self._dataset.scanner(
            columns=self._columns,
            batch_size=_MASKED_BATCH_ROWS,
            batch_readahead=_MASKED_READAHEAD,
            fragment_readahead=1,
            memory_pool=pa.system_memory_pool(),
        )
        return scanner.to_reader().__arrow_c_stream__(requested_schema)


def _masked_source(
    path: str, physical: Sequence[str], text: Collection[str], nulled: Collection[str]
) -> _MaskedArrowSource:
    """The file's ``physical`` columns, ``text`` given the mask token and ``nulled`` null.

    A masked column's original values are not handed to DuckDB: a text column becomes the
    token wherever it is not null, and any other masked column is a null constant the
    scanner never reads from the file. A file pyarrow cannot read (a 128-bit integer
    Polars wrote) raises, and the query is refused rather than read unmasked.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    import pyarrow.dataset as pads

    dataset = pads.dataset(path, format="parquet")
    # Parquet has no duration type: DuckDB reads the stored INT64, while pyarrow gives
    # back the duration a Polars-written file's Arrow schema records. Read it as stored.
    schema = pa.schema(
        [f.with_type(pa.int64()) if pa.types.is_duration(f.type) else f for f in dataset.schema]
    )
    if schema != dataset.schema:
        dataset = pads.dataset(path, format="parquet", schema=schema)
    columns: dict[str, pads.Expression] = {}
    for name in physical:
        column = pads.field(name)
        if name in text:
            columns[name] = pc.if_else(
                pc.is_null(column),
                pc.scalar(pa.scalar(None, pa.string())),
                pc.scalar(pa.scalar(PII_MASK_TOKEN, pa.string())),
            )
        elif name in nulled:
            columns[name] = pc.scalar(pa.scalar(None, schema.field(name).type))
        else:
            columns[name] = column
    return _MaskedArrowSource(dataset, columns)


def masked_columns_from_env() -> frozenset[str]:
    """The columns the engine asked this child to mask; an empty list masks none.

    Raises:
        ValueError: The variable is unset, or is not a JSON list of names — refused
            rather than read as "mask nothing". The engine sets it on every query.
    """
    raw = os.environ.get(MASKED_COLUMNS_ENV)
    if raw is None:
        raise ValueError(f"{MASKED_COLUMNS_ENV} is not set")
    decoded = json.loads(raw)
    if not isinstance(decoded, list) or not all(isinstance(name, str) for name in decoded):
        raise ValueError(f"{MASKED_COLUMNS_ENV} must be a JSON list of column names")
    return frozenset(decoded)


def _restored(column: str, dtype: str | None) -> str:
    """The SQL giving a stored column (``column`` is its SQL) its Builder dtype back."""
    if dtype == "Null":
        return "NULL"
    if dtype == "Int128":
        return f"CAST({column} AS HUGEINT)"
    if dtype is not None and dtype.startswith("Duration("):
        return f"to_microseconds({column})"
    return column


@contextmanager
def open_sandbox(
    table_path: str,
    *,
    profile: BuildProfile | None = None,
    masked: Collection[str] = (),
) -> Iterator[Sandbox]:
    """A locked connection whose ``dataset`` is ``table_path``; closed on exit.

    Args:
        masked: Columns, by real name, that both views show masked (#1331). When any is
            named — present in the table or not — the connection reads the masked Arrow
            stream and may open no file. The query child passes what
            :func:`masked_columns_from_env` says.
    """
    withheld = frozenset(masked)
    limits = profile or BuildProfile.from_env()
    given = os.environ.get(TEMP_DIR_ENV)
    temp_dir = given or tempfile.mkdtemp(prefix="kpubdata-query-")
    connection = duckdb.connect(
        ":memory:",
        config={
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_community_extensions": False,
            "threads": limits.threads,
            "memory_limit": limits.memory_limit,
        },
    )
    try:
        path = os.fspath(table_path)
        # The spill quota is set after open: DuckDB does not enforce it from the
        # connect config (#701).
        connection.execute(f"SET temp_directory = {quote_literal(temp_dir)}")
        connection.execute(
            f"SET max_temp_directory_size = {quote_literal(limits.max_temp_directory_size)}"
        )
        connection.execute("SET TimeZone = 'UTC'")

        kv = _kv(connection, path)
        dtypes = dict(kv.get(KV_KEY, {}))
        # A file Polars wrote (an older snapshot, a masked copy) records no Builder
        # dtypes, but a Null column is still Parquet's null logical type.
        for name, logical in connection.execute(
            "SELECT name, logical_type FROM parquet_schema(?)", [path]
        ).fetchall():
            if logical == "NullType()":
                dtypes.setdefault(str(name), "Null")
        renamed = kv.get(KV_NAMES_KEY, {})
        plain = f"read_parquet({quote_literal(path)})"
        placeholder = kv.get(NO_COLUMNS_KEY, {}).get("") == "true"
        storage = {
            str(row[0]): str(row[1])
            for row in connection.execute(f"DESCRIBE SELECT * FROM {plain}").fetchall()
        }
        described = list(storage)
        stored = [name for name in described if not (placeholder and name == NO_COLUMNS)]
        names = [renamed.get(physical, physical) for physical in stored]
        recorded = kv.get(KV_KEY, {})
        allowed = [path]
        if withheld:
            # The views read the masked stream, each column cast back to its stored
            # type, and the file leaves allowed_paths below. The stream keeps the file's
            # order, as a scan of the file does, so counting rows gives each one's place.
            hidden = {p: n for p, n in zip(stored, names, strict=True) if n in withheld}
            text = {p for p, n in hidden.items() if _is_text(storage[p], recorded.get(n))}
            projected = stored or [NO_COLUMNS]
            connection.register(
                _MASKED_SOURCE, _masked_source(path, projected, text, hidden.keys() - text)
            )
            source = quote_identifier(_MASKED_SOURCE)
            row_order = "(row_number() OVER ()) - 1"
            read = [
                f"CAST({quote_identifier(physical)} AS {storage[physical]})" for physical in stored
            ]
            allowed = []
        elif any(name.casefold() == _FILE_ROW_NUMBER for name in described):
            # DuckDB will not add its row-number column to a file that has a column of
            # that name, in any letter case: the read fails outright (#1149). The
            # position is counted instead. A window with no ordering runs as a streaming
            # operator over the scan, and the scan keeps the file's order
            # (``preserve_insertion_order``, on by default and not changeable here once
            # the configuration is locked), so the count is the row's place in the file.
            source = plain
            row_order = "(row_number() OVER ()) - 1"
            read = [quote_identifier(physical) for physical in stored]
        else:
            source = f"read_parquet({quote_literal(path)}, file_row_number = true)"
            row_order = "file_row_number"
            read = [quote_identifier(physical) for physical in stored]
        restored = [_restored(sql, dtypes.get(name)) for sql, name in zip(read, names, strict=True)]
        internal = ", ".join(f"{sql} AS _c{i}" for i, sql in enumerate(restored))
        connection.execute(
            f"CREATE VIEW {ORDERED_DATASET} AS SELECT {internal}"
            f"{', ' if internal else ''}{row_order} AS {quote_identifier(ROW_ORDER)} "
            f"FROM {source}"
        )
        folded = [name.casefold() for name in names]
        refused: str | None = None
        if not names:
            refused = "the table has no columns"
        elif any(not name for name in names):
            refused = "the table has a column with an empty name, which SQL cannot name"
        elif len(set(folded)) != len(folded):
            refused = "the table has columns whose names differ only in letter case"
        elif ROW_ORDER.casefold() in folded:
            refused = f"the table has a column named {ROW_ORDER}, which is reserved"
        else:
            select = ", ".join(
                f"{sql} AS {quote_identifier(name)}"
                for sql, name in zip(restored, names, strict=True)
            )
            connection.execute(f"CREATE VIEW {DATASET} AS SELECT {select} FROM {source}")

        connection.execute(f"SET allowed_paths = [{', '.join(quote_literal(p) for p in allowed)}]")
        connection.execute("SET python_enable_replacements = false")
        connection.execute(f"SET allowed_directories = [{quote_literal(temp_dir)}]")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET lock_configuration = true")
        yield Sandbox(connection, dtypes, tuple(names), refused)
    finally:
        connection.close()
        if not given:
            shutil.rmtree(temp_dir, ignore_errors=True)


__all__ = [
    "DATASET",
    "MASKED_COLUMNS_ENV",
    "ORDERED_DATASET",
    "ROW_ORDER",
    "TEMP_DIR_ENV",
    "Sandbox",
    "masked_columns_from_env",
    "open_sandbox",
]

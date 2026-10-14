"""A locked DuckDB connection over one snapshot's table file (#874, ADR 0021 D6).

The query child opens one of these and nothing else. It is the second of three
layers — the SQL validator (``security``) before it, the child process (``engine``)
around it. It holds its limits without the other two; what it does not do on its own
is keep a Silver table's original values away from SQL (see *Masked columns*):

- **One file, one directory.** The connection may read the pinned ``table.parquet``
  (``allowed_paths``) and spill into its own temporary directory
  (``allowed_directories``); every other file system, network or extension access is
  refused (``enable_external_access = false``). No extension is installed or loaded.
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
  (#900); both views give each of them Gold's mask — text becomes the mask token with
  its nulls kept, any other dtype null of its own type — so no expression over the
  view sees an original value. The mask is part of the view rather than a copy of the
  table (#1331): DuckDB applies it to the rows a query reads, and its cost is the
  query's, inside the child's timeout, memory cap and spill quota.
- **The raw Silver file is reachable; the validator keeps SQL off it.** The views read
  the file, so its path stays on ``allowed_paths`` even when columns are masked: on
  this connection ``read_parquet('<path>')``, ``FROM '<path>'`` or
  ``parquet_metadata('<path>')`` (whose statistics are values) would return original
  values. For Silver, the validator — which lets user SQL name only ``dataset`` and no
  table function or quoted path — is the only layer that keeps the raw file out of a
  query (#1331). Keeping it out here too costs pyarrow's memory in the child, which the
  768 MB default cap does not leave room for (#1363, not taken).

Paths are written into SQL only through ``quote_literal`` and come from the server,
never from a request.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import duckdb

from ..stages.gold.pii import PII_MASK_TOKEN
from ..tabular.builder_kv import KV_KEY, KV_NAMES_KEY, NO_COLUMNS, NO_COLUMNS_KEY
from ..tabular.duckdb_runtime import BuildProfile
from ..tabular.sql import quote_identifier, quote_literal

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


def _masked(physical: str, storage: str, dtype: str | None) -> str:
    """The stored column with Gold's mask applied, still of its stored type (#900).

    Text keeps its null pattern and gets the token; any other column becomes null of
    its own type (``stages/gold/pii``, #902). A column the file records no dtype for is
    text when it is stored as VARCHAR.
    """
    column = quote_identifier(physical)
    if (dtype if dtype is not None else ("String" if storage == "VARCHAR" else "")) == "String":
        return f"CASE WHEN {column} IS NULL THEN NULL ELSE {quote_literal(PII_MASK_TOKEN)} END"
    return f"CAST(NULL AS {storage})"


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
        masked: Columns, by real name, that both views show masked (#1331). A name the
            table does not have is ignored. The query child passes what
            :func:`masked_columns_from_env` says; nothing else here reads it.
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
        if any(name.casefold() == _FILE_ROW_NUMBER for name in described):
            # DuckDB will not add its row-number column to a file that has a column of
            # that name, in any letter case: the read fails outright (#1149). The
            # position is counted instead. A window with no ordering runs as a streaming
            # operator over the scan, and the scan keeps the file's order
            # (``preserve_insertion_order``, on by default and not changeable here once
            # the configuration is locked), so the count is the row's place in the file.
            source = plain
            row_order = "(row_number() OVER ()) - 1"
        else:
            source = f"read_parquet({quote_literal(path)}, file_row_number = true)"
            row_order = "file_row_number"
        names = [renamed.get(physical, physical) for physical in stored]
        recorded = kv.get(KV_KEY, {})
        restored = [
            _restored(
                _masked(physical, storage[physical], recorded.get(name))
                if name in withheld
                else quote_identifier(physical),
                dtypes.get(name),
            )
            for physical, name in zip(stored, names, strict=True)
        ]
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

        connection.execute(f"SET allowed_paths = [{quote_literal(path)}]")
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

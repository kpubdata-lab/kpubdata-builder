"""A locked DuckDB connection over one snapshot's table file (#874, ADR 0021 D6).

The query child opens one of these and nothing else. It is the second of three
layers — the SQL validator (``security``) before it, the child process (``engine``)
around it — and does not rely on the other two:

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

Paths are written into SQL only through ``quote_literal`` and come from the server,
never from a request.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import duckdb

from ..tabular.builder_kv import KV_KEY, KV_NAMES_KEY, NO_COLUMNS, NO_COLUMNS_KEY
from ..tabular.duckdb_runtime import BuildProfile
from ..tabular.sql import quote_identifier, quote_literal

DATASET = "dataset"
#: ``dataset`` with :data:`ROW_ORDER` — the row's position in the file — added.
ORDERED_DATASET = "_kpubdata_dataset_ordered"
#: The row position column of :data:`ORDERED_DATASET`; refused as a table column name.
ROW_ORDER = "__kpubdata_row_order"
#: Set by the query engine in the child: the temporary directory it made for this query.
TEMP_DIR_ENV = "KPUBDATA_QUERY_TEMP_DIR"


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


def _restored(physical: str, dtype: str | None) -> str:
    """The SQL giving a stored column its Builder dtype back."""
    column = quote_identifier(physical)
    if dtype == "Null":
        return "NULL"
    if dtype == "Int128":
        return f"CAST({column} AS HUGEINT)"
    if dtype is not None and dtype.startswith("Duration("):
        return f"to_microseconds({column})"
    return column


@contextmanager
def open_sandbox(table_path: str, *, profile: BuildProfile | None = None) -> Iterator[Sandbox]:
    """A locked connection whose ``dataset`` is ``table_path``; closed on exit."""
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
        source = f"read_parquet({quote_literal(path)}, file_row_number = true)"
        placeholder = kv.get(NO_COLUMNS_KEY, {}).get("") == "true"
        stored = [
            row[0]
            for row in connection.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
            if row[0] != "file_row_number" and not (placeholder and row[0] == NO_COLUMNS)
        ]
        names = [renamed.get(physical, physical) for physical in stored]
        restored = [
            _restored(physical, dtypes.get(name))
            for physical, name in zip(stored, names, strict=True)
        ]
        internal = ", ".join(f"{sql} AS _c{i}" for i, sql in enumerate(restored))
        connection.execute(
            f"CREATE VIEW {ORDERED_DATASET} AS SELECT {internal}"
            f"{', ' if internal else ''}file_row_number AS {quote_identifier(ROW_ORDER)} "
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
    "ORDERED_DATASET",
    "ROW_ORDER",
    "TEMP_DIR_ENV",
    "Sandbox",
    "open_sandbox",
]

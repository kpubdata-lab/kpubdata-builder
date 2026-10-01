"""Creating and closing DuckDB connections for Builder (ADR 0021, #866).

Nothing in the build or query path uses DuckDB yet: this is the foundation the later
steps (#868 Silver, #869 Gold, #874 query sandbox) build on, so that every connection
Builder opens is made the same way.

- **One connection per unit of work.** A build opens one per source worker; nothing is
  shared, so one worker's tables, settings and temp files cannot reach another's.
- **Settings fixed at open.** ``memory_limit``, ``threads``, ``temp_directory`` and
  ``max_temp_directory_size`` come from a :class:`BuildProfile`; ``TimeZone`` is always
  UTC, whatever the host says (DuckDB otherwise follows the host, which on a Korean
  server is ``Asia/Seoul``). Extensions are never installed or loaded on demand: a
  query must not make the server download code.
- **A temp directory of its own.** Spill files go under
  ``<run>/_duckdb_tmp/<source>-<worker>/``, created at open and removed at close. A
  connection removes only its own directory, so a worker that finishes early does not
  pull the floor out from under one still spilling.

The connection object stays inside the tabular layer. Callers outside it name tables
with :class:`TabularRelation`, which is deliberately not exported from the package root.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import duckdb

from .sql import quote_identifier, quote_literal

#: The oldest DuckDB with every setting Builder's sandbox needs (see REQUIRED_SETTINGS).
#: Checked against the release wheels: 1.1.3 lacks allowed_paths and
#: allowed_directories; 1.2.0 has all five and enforces them. pyproject.toml's floor
#: must say the same, which a test checks.
MINIMUM_DUCKDB_VERSION = (1, 2, 0)

#: Settings the query sandbox (#874) relies on. Their absence is a startup error, not a
#: sandbox that silently lets everything through.
REQUIRED_SETTINGS = (
    "allowed_paths",
    "allowed_directories",
    "lock_configuration",
    "enable_external_access",
    "max_temp_directory_size",
)

#: The internal row ordinal (ADR 0021 D8). Builder adds it to keep source order through
#: operations that do not preserve it, and removes it before anything is written.
ROW_SEQ_COLUMN = "_kpubdata_row_seq"

#: Where a run's DuckDB spill files go, under the run directory.
TEMP_DIRECTORY_NAME = "_duckdb_tmp"

_SAFE_PART = re.compile(r"[^A-Za-z0-9._-]+")


class ReservedColumnError(ValueError):
    """A source has a column with a name Builder reserves for itself."""


#: Environment variables for every Builder DuckDB connection's limits (#701, ADR 0021
#: D9). Each applies per connection: a build opens one per running source.
THREADS_ENV = "KPUBDATA_DUCKDB_THREADS"
MEMORY_LIMIT_ENV = "KPUBDATA_DUCKDB_MEMORY_LIMIT"
MAX_TEMP_SIZE_ENV = "KPUBDATA_DUCKDB_MAX_TEMP_SIZE"

_SIZE = re.compile(r"^\s*\d+(\.\d+)?\s*(B|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\s*$", re.IGNORECASE)


def _size(value: str, *, name: str) -> str:
    if not _SIZE.match(value):
        raise ValueError(f"{name} must be a size such as 512MB or 2GiB, got {value!r}")
    return value.strip()


@dataclass(frozen=True)
class BuildProfile:
    """Resource settings for a build connection (ADR 0021 D9, #701).

    Per connection: ``threads`` DuckDB threads, ``memory_limit`` of buffer memory before
    it spills, and at most ``max_temp_directory_size`` of spill files — past that a
    query fails instead of filling the disk. A deployment sets them with
    :data:`THREADS_ENV`, :data:`MEMORY_LIMIT_ENV` and :data:`MAX_TEMP_SIZE_ENV`
    (:meth:`from_env`); ``docs/deploy.md`` adds them up for a host.
    """

    memory_limit: str = "1GB"
    threads: int = 2
    max_temp_directory_size: str = "10GB"

    def __post_init__(self) -> None:
        if self.threads < 1:
            raise ValueError("threads must be at least 1")
        _size(self.memory_limit, name="memory_limit")
        _size(self.max_temp_directory_size, name="max_temp_directory_size")

    @classmethod
    def from_env(cls) -> BuildProfile:
        """The defaults, overridden by whichever of the three variables are set.

        Raises:
            ValueError: A variable holds something other than a positive integer
                (threads) or a size; its name is in the message.
        """
        defaults = cls()
        raw_threads = os.environ.get(THREADS_ENV, "").strip()
        try:
            threads = int(raw_threads) if raw_threads else defaults.threads
        except ValueError:
            raise ValueError(f"{THREADS_ENV} must be a positive integer") from None
        if threads < 1:
            raise ValueError(f"{THREADS_ENV} must be a positive integer")
        memory = os.environ.get(MEMORY_LIMIT_ENV, "").strip() or defaults.memory_limit
        temp = os.environ.get(MAX_TEMP_SIZE_ENV, "").strip() or defaults.max_temp_directory_size
        return cls(
            memory_limit=_size(memory, name=MEMORY_LIMIT_ENV),
            threads=threads,
            max_temp_directory_size=_size(temp, name=MAX_TEMP_SIZE_ENV),
        )


@dataclass(frozen=True)
class TabularRelation:
    """A table inside a Builder DuckDB connection, named without exposing the connection."""

    name: str

    @property
    def sql(self) -> str:
        """The name as it goes into generated SQL."""
        return quote_identifier(self.name)


def duckdb_version() -> tuple[int, int, int]:
    """The installed DuckDB's version as ``(major, minor, patch)``."""
    parts = re.findall(r"\d+", duckdb.__version__)[:3]
    major, minor, patch = (int(p) for p in (parts + ["0", "0", "0"])[:3])
    return major, minor, patch


def check_duckdb() -> None:
    """Refuse a DuckDB older than the floor, or one missing a sandbox setting.

    Raises:
        RuntimeError: The installed DuckDB cannot run Builder safely.
    """
    version = duckdb_version()
    if version < MINIMUM_DUCKDB_VERSION:
        floor = ".".join(str(p) for p in MINIMUM_DUCKDB_VERSION)
        raise RuntimeError(f"DuckDB {duckdb.__version__} is older than {floor}")
    with duckdb.connect(":memory:") as connection:
        rows = connection.execute("SELECT name FROM duckdb_settings()").fetchall()
        available = {row[0] for row in rows}
    missing = [name for name in REQUIRED_SETTINGS if name not in available]
    if missing:
        raise RuntimeError(f"DuckDB {duckdb.__version__} lacks settings: {', '.join(missing)}")


def reserve_row_seq(columns: Iterable[str]) -> None:
    """Refuse a source that already has a column DuckDB would read as the row ordinal.

    DuckDB matches names case-insensitively, quoted or not, so ``_KPUBDATA_ROW_SEQ``
    collides too. A user's column is never silently overwritten.

    Raises:
        ReservedColumnError: A column has the reserved name.
    """
    for column in columns:
        if column.casefold() == ROW_SEQ_COLUMN:
            raise ReservedColumnError(
                f"column {column!r} uses a name Builder reserves ({ROW_SEQ_COLUMN}); "
                "rename it in the source's schema"
            )


def worker_temp_directory(run_dir: Path, source_key: str, worker_id: int | str) -> Path:
    """``<run>/_duckdb_tmp/<source>-<hash>-<worker>/``, path-safe and unique per source.

    Making a name path-safe folds different names together (any two Hangul aliases
    both become ``_``), so a short hash of the exact source key and worker id is part of the
    name: two sources running side by side never share a spill directory.
    """
    exact = f"{source_key}\x00{worker_id}"
    digest = hashlib.sha256(exact.encode("utf-8")).hexdigest()[:10]
    readable = _SAFE_PART.sub("_", f"{source_key}").strip(".")[:40] or "source"
    return run_dir / TEMP_DIRECTORY_NAME / f"{readable}-{digest}-{worker_id}"


def clear_temp_root(run_dir: Path) -> None:
    """Remove what earlier attempts at this run left in ``<run>/_duckdb_tmp`` — called
    once when a run starts, before any connection opens."""
    shutil.rmtree(run_dir / TEMP_DIRECTORY_NAME, ignore_errors=True)


def connect(profile: BuildProfile, temp_directory: Path) -> duckdb.DuckDBPyConnection:
    """A new in-memory connection with ``profile``'s limits, UTC and no extension loading."""
    connection = duckdb.connect(
        ":memory:",
        config={
            "memory_limit": profile.memory_limit,
            "threads": profile.threads,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        },
    )
    try:
        # The spill directory and its quota are set after open, in this order: given in
        # the connect config, DuckDB reports the quota but does not enforce it (#701) —
        # a spilling query filled hundreds of MB past a 4 MB quota.
        connection.execute(f"SET temp_directory = {quote_literal(os.fspath(temp_directory))}")
        connection.execute(
            f"SET max_temp_directory_size = {quote_literal(profile.max_temp_directory_size)}"
        )
        # TimeZone belongs to the ICU extension, so it is set after open, not in config.
        connection.execute("SET TimeZone = 'UTC'")
    except BaseException:
        connection.close()
        raise
    return connection


@contextmanager
def build_connection(
    run_dir: Path,
    source_key: str,
    worker_id: int | str = 0,
    profile: BuildProfile | None = None,
) -> Iterator[duckdb.DuckDBPyConnection]:
    """A connection for one build worker, closed and its temp directory removed on exit."""
    temp_directory = worker_temp_directory(run_dir, source_key, worker_id)
    # Unique per source (worker_temp_directory): an existing directory is another live
    # connection's, or a crashed attempt's that clear_temp_root should have removed —
    # never silently taken over.
    temp_directory.mkdir(parents=True, exist_ok=False)
    try:
        connection = connect(profile or BuildProfile.from_env(), temp_directory)
        try:
            yield connection
        finally:
            connection.close()
    finally:
        shutil.rmtree(temp_directory, ignore_errors=True)
        # Another worker's directory may still be there; the last one out removes it.
        with suppress(OSError):
            temp_directory.parent.rmdir()


__all__ = [
    "clear_temp_root",
    "MAX_TEMP_SIZE_ENV",
    "MEMORY_LIMIT_ENV",
    "MINIMUM_DUCKDB_VERSION",
    "REQUIRED_SETTINGS",
    "ROW_SEQ_COLUMN",
    "TEMP_DIRECTORY_NAME",
    "THREADS_ENV",
    "BuildProfile",
    "ReservedColumnError",
    "TabularRelation",
    "build_connection",
    "check_duckdb",
    "connect",
    "duckdb_version",
    "reserve_row_seq",
    "worker_temp_directory",
]

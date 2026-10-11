"""Refusing a state store written by a release this code does not know (#1096).

Each SQLite store records the version of its schema. A store whose version is newer
than the running code was written by a later release: this code does not know what the
later one added or what it relies on, so it must not read it as its own, and must not
recreate it to make the difference go away. It refuses to open, and the server does not
start.

That is what an operator meets after rolling a deployment back. The message says which
store, which versions, and what can be done.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS


class UnsupportedSchemaVersionError(RuntimeError):
    """A state store has a schema version this code cannot use."""

    def __init__(self, *, store: str, location: str, found: int, supported: int, remedy: str):
        self.store = store
        self.found = found
        self.supported = supported
        super().__init__(
            f"{store} at {location} has schema version {found}; this release supports "
            f"up to {supported}. It was written by a newer release and is left "
            f"untouched. {remedy}"
        )


#: Seconds a read-only look at a store waits for a lock: as long as the stores themselves
#: do (``sqlite_settings``). SQLite's default of five gave up on a store that an
#: ordinary connection would have waited for.
PROBE_TIMEOUT_SECONDS = BUSY_TIMEOUT_SECONDS


def open_read_only(path: Path) -> sqlite3.Connection:
    """A read-only connection to ``path`` that sets nothing on it."""
    return sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=PROBE_TIMEOUT_SECONDS
    )


#: What SQLite says of a file that is not a usable database of ours: not one at all,
#: damaged, or without the table asked for. Anything else it says — locked, cannot be
#: opened, an I/O error — is about the moment and not about the file.
_DAMAGE = ("not a database", "malformed", "no such table")


def says_damaged(error: sqlite3.Error) -> bool:
    """Whether ``error`` says the file itself is not usable, rather than busy or unreachable."""
    message = str(error).lower()
    return any(sign in message for sign in _DAMAGE)


#: What SQLite says when a store could not be reached at all: held by another process,
#: not openable, on a disk that failed, is full or cannot be written. Nothing here is
#: about what the store holds. The message has to start with one of these: a word of one
#: could as well be the name of a column in a message about something else
#: (``no such column: locked_at``), and SQLite adds a detail after some
#: (``database table is locked: builds``).
_UNREACHABLE = (
    "database is locked",
    "database table is locked",
    "database schema is locked",
    "unable to open database file",
    "disk i/o error",
    "attempt to write a readonly database",
    "database or disk is full",
)


def says_unreachable(error: sqlite3.Error) -> bool:
    """Whether ``error`` says the store could not be reached, rather than what is in it."""
    message = str(error).lower()
    return not says_damaged(error) and any(message.startswith(sign) for sign in _UNREACHABLE)


def stored_version(path: Path) -> int | None:
    """The schema version a SQLite store records, read without changing the file.

    The connection is read-only and sets nothing, so a store this code then refuses
    keeps its journal mode: opening it the ordinary way switches it to WAL before any
    version is looked at (#1096). A store that is not in WAL mode gains no file beside
    it either. One that is already in WAL mode does — SQLite creates its ``-shm`` and
    ``-wal`` to read it at all — which changes nothing about the store.

    None when there is no file, or the file holds no version: it is not a database, is
    damaged, or has no version table.

    Raises:
        sqlite3.Error: The version could not be read for another reason — the store
            is locked, cannot be opened, the disk failed. "No version" would be a
            guess, and a caller that replaces a store with no version would replace
            one that was only busy (#1157).
    """
    if not path.is_file():
        return None
    try:
        with closing(open_read_only(path)) as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.Error as exc:
        if says_damaged(exc):
            return None
        raise
    return int(row[0]) if row is not None and row[0] is not None else None


#: One step of a store's schema: what turns version ``n`` into ``n + 1``. It is handed
#: a connection inside the transaction that also records the new version, and must not
#: commit, roll back, drop a table or rewrite a row.
Migration = Callable[[sqlite3.Connection], None]

_CREATE_VERSION_TABLE = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    " version INTEGER PRIMARY KEY,"
    " applied_at TEXT DEFAULT (datetime('now')))"
)


@dataclass(frozen=True)
class StoreSchema:
    """The versions of one SQLite store's schema, and how a file is brought to the last.

    The version a store supports is the number of its ``migrations``: step ``n`` (from
    zero) takes a file at version ``n`` to ``n + 1``, and a change to the schema is one
    more step at the end. A file with no recorded version is at version zero, whether it
    is new or was written by a release from before the store recorded one (#1096) — so
    the first step only creates what is absent and adds what is missing, and such a file
    is adopted with every row it had.

    The version is kept as the build index, the event store and the catalog keep theirs:
    a ``schema_version`` table, read by ``stored_version``.
    """

    #: What the store is, as an error message names it.
    store: str
    migrations: tuple[Migration, ...]
    #: What an operator can do about a file a newer release wrote.
    remedy: str

    @property
    def version(self) -> int:
        """The schema version this release writes and reads."""
        return len(self.migrations)

    def _refuse_newer(self, path: Path, found: int | None) -> None:
        if found is not None and found > self.version:
            raise UnsupportedSchemaVersionError(
                store=self.store,
                location=str(path),
                found=found,
                supported=self.version,
                remedy=self.remedy,
            )

    def refuse_newer(self, path: Path) -> None:
        """Refuse a file a newer release wrote, without changing or creating it.

        Raises:
            UnsupportedSchemaVersionError: The file records a version above this one.
            sqlite3.Error: The file could not be read (``stored_version``).
        """
        self._refuse_newer(path, stored_version(path))

    def bring_up_to_date(self, path: Path, connect: Callable[[], sqlite3.Connection]) -> None:
        """Make the store at ``path`` this release's, or refuse it.

        The version is first read on a read-only connection: a file a newer release
        wrote is refused before ``connect`` is called, so nothing is set on it, and a
        file already at this version is not written to at all — it can be opened on a
        disk that cannot be written.

        Anything older is migrated on a connection from ``connect`` in one
        ``BEGIN IMMEDIATE`` transaction that also records the version: a step that fails
        leaves the file at the version it had, and a second process opening the same
        file waits for the first and then finds nothing left to do.

        Raises:
            UnsupportedSchemaVersionError: The file records a version above this one.
            sqlite3.Error: The file could not be read or migrated.
        """
        found = stored_version(path)
        self._refuse_newer(path, found)
        if found == self.version:
            return
        with closing(connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(_CREATE_VERSION_TABLE)
                row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                # Read again under the write lock: another process may have migrated the
                # file, or a newer release created it, since the look above.
                current = int(row[0]) if row is not None and row[0] is not None else 0
                self._refuse_newer(path, current)
                for step in self.migrations[current:]:
                    step(conn)
                if current != self.version:
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (self.version,))
            except BaseException:
                conn.rollback()
                raise
            conn.commit()


def add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    """Add each of ``columns`` (name, definition) that ``table`` does not have yet.

    For the step that adopts a file from before a store recorded a version: such a file
    may predate a column, and no version says which. Rows are not rewritten; an existing
    row reads the column's default.
    """
    present = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns:
        if name not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


__all__ = [
    "PROBE_TIMEOUT_SECONDS",
    "Migration",
    "StoreSchema",
    "UnsupportedSchemaVersionError",
    "add_missing_columns",
    "open_read_only",
    "says_damaged",
    "says_unreachable",
    "stored_version",
]

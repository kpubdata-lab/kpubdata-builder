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
from contextlib import closing
from pathlib import Path


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


#: Seconds a read-only look at a store waits for a lock, as the stores themselves do
#: (``store/inventory.py``). SQLite's default of five gave up on a store that an
#: ordinary connection would have waited for.
PROBE_TIMEOUT_SECONDS = 30.0


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


__all__ = [
    "PROBE_TIMEOUT_SECONDS",
    "UnsupportedSchemaVersionError",
    "open_read_only",
    "says_damaged",
    "says_unreachable",
    "stored_version",
]

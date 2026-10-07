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


def stored_version(path: Path) -> int | None:
    """The schema version a SQLite store records, read without changing the file.

    The connection is read-only and sets nothing, so a store this code then refuses
    keeps its journal mode and gains no ``-wal`` or ``-shm`` file beside it: opening it
    the ordinary way switches it to WAL before any version is looked at (#1096).

    None when there is no file, no version in it, or it cannot be read this way — the
    ordinary open then decides, and fails as it would have.
    """
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.Error:
        return None
    return int(row[0]) if row is not None and row[0] is not None else None


__all__ = ["UnsupportedSchemaVersionError", "stored_version"]

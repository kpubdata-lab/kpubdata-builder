"""What every SQLite state store connects with (#1096).

Ten modules open a store (``store/inventory.py``), and each chose how long a connection
waits for a lock: two waited five seconds and the rest thirty. Nothing said why the two
differed, and a locked store then failed some requests after five seconds while others,
behind the same lock, were still waiting.

One wait for all of them is written here: thirty seconds, what eight of the ten already
used. It is not derived from anything else — a request has no time limit of its own to
match (the thirty seconds in ``service/http.py`` are how long a socket waits to be
read) — so it is a number to change here if another serves better.

This module imports nothing of the package, so any store can import it.
"""

from __future__ import annotations

import sqlite3
import time

#: Seconds a connection waits for a lock before the statement fails.
BUSY_TIMEOUT_SECONDS = 30.0

#: The same wait in milliseconds, for ``PRAGMA busy_timeout``.
BUSY_TIMEOUT_MS = int(BUSY_TIMEOUT_SECONDS * 1000)

#: How long ``enable_wal`` sleeps between two tries.
_WAL_RETRY_SECONDS = 0.02


def enable_wal(connection: sqlite3.Connection) -> None:
    """Put ``connection``'s database in WAL mode, waiting for another that is doing the same.

    A new database file is in rollback mode until one connection changes it. Two that
    open it at once both try, and SQLite answers the second ``database is locked`` at
    once rather than after the busy timeout: it will not have two connections wait on
    each other for the exclusive lock the change needs (#1210). The change is tried
    again for as long as any other lock is waited for. A database already in WAL mode
    is not changed and takes no lock.

    Raises:
        sqlite3.OperationalError: The database stayed locked for the whole wait, or the
            mode could not be changed for another reason.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_SECONDS
    while True:
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as error:
            if "locked" not in str(error).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(_WAL_RETRY_SECONDS)


__all__ = ["BUSY_TIMEOUT_MS", "BUSY_TIMEOUT_SECONDS", "enable_wal"]

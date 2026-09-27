"""Table catalog — immutable snapshots with a transactional pointer (#699).

One difference from ``BuildIndex`` (ADR 0003) decides the whole design. The build
index is **derived**: it may swallow write failures and can be rebuilt from
manifest.json. This catalog is **canonical** — which snapshot is current exists
nowhere else. So it does not swallow write failures, and it moves the pointer
with a compare-and-swap.

Schema::

    tables
      id, workspace_id, logical_name, current_snapshot_id, revision

    table_snapshots
      id, table_id, run_id, schema_version, coverage_hash,
      artifact_digest, row_count, state, created_at, committed_at

    snapshot_leases
      lease_id, snapshot_id, acquired_at, expires_at

``state``: ``staging`` -> ``validated`` -> ``committed``, or ``quarantined``.

The pointer update is a compare-and-swap::

    BEGIN IMMEDIATE;
    UPDATE tables
       SET current_snapshot_id = :new, revision = revision + 1
     WHERE id = :table AND revision = :expected;

An affected row count other than 1 is a concurrent-update conflict. It raises
``SnapshotConflict`` and is **not retried** — retrying would overwrite the commit
that won.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

from .errors import (
    ImmutableSnapshot,
    SnapshotConflict,
    SnapshotInUse,
    SnapshotNotFound,
    SnapshotStateError,
    TableNotFound,
)

# Schema version. Being canonical, this catalog cannot be dropped and recreated
# when the version changes — it needs a migration. There is no migration path yet,
# so a mismatched version refuses to open. That beats discarding data silently.
SCHEMA_VERSION = 1

CATALOG_FILENAME = "_warehouse.sqlite"

SnapshotState = Literal["staging", "validated", "committed", "quarantined"]

# Default lease lifetime, so a crashed query cannot pin a snapshot forever.
DEFAULT_LEASE_SECONDS = 3600


@dataclass(frozen=True)
class TableRow:
    """One table row in the catalog.

    Attributes:
        id: Table identifier.
        workspace_id: Owning workspace. The first version has one personal
            workspace.
        logical_name: Human-readable name.
        current_snapshot_id: The snapshot readers should use, or None when
            nothing has been committed yet.
        revision: Monotonic counter for the compare-and-swap. It increments on
            every pointer move.
    """

    id: str
    workspace_id: str
    logical_name: str
    current_snapshot_id: str | None
    revision: int


@dataclass(frozen=True)
class SnapshotRow:
    """One snapshot row in the catalog."""

    id: str
    table_id: str
    run_id: str
    schema_version: int
    coverage_hash: str
    artifact_digest: str
    row_count: int | None
    state: SnapshotState
    created_at: str
    committed_at: str | None


@dataclass(frozen=True)
class PinnedSnapshot:
    """A snapshot a query has pinned.

    ``resolve_current`` reads the current pointer and takes the lease **in one
    transaction** and returns this. That is what keeps a running query unaffected
    by commits, and keeps garbage collection off the snapshot it is reading.

    Attributes:
        snapshot_id: The pinned snapshot.
        lease_id: Identifier to pass to ``release``.
        revision: The table revision at resolution time.
    """

    snapshot_id: str
    lease_id: str
    revision: int


def _now() -> str:
    """Current time as a UTC ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


class TableCatalog:
    """Single-file SQLite table catalog.

    The CUBRID backend (ADR 0016) is an **additional** backend under the same
    protocol, not a replacement. SQLite is enough for the first catalog: one node,
    short transactions, ``BEGIN IMMEDIATE``, a busy timeout and a CAS revision.
    """

    def __init__(self, root: Path, *, catalog_path: Path | None = None) -> None:
        """Open the catalog.

        Args:
            root: Warehouse root. The catalog lives at ``root/_warehouse.sqlite``.
            catalog_path: Path override, for tests.
        """
        self._root = root
        self._path = catalog_path if catalog_path is not None else root / CATALOG_FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    @property
    def root(self) -> Path:
        """Warehouse root."""
        return self._root

    @property
    def _conn(self) -> sqlite3.Connection:
        """Thread-local connection, created on first use."""
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return cast(sqlite3.Connection, self._local.conn)

    def _connect(self) -> sqlite3.Connection:
        """Create and configure a connection.

        ``isolation_level=None`` matters. Python's sqlite3 default opens a
        **deferred** transaction before DML, and a deferred transaction that
        starts as a reader can fail with ``SQLITE_BUSY`` when it upgrades to a
        writer — which is exactly the gap the compare-and-swap exists to close.
        Running in autocommit and issuing ``BEGIN IMMEDIATE`` explicitly avoids it.
        """
        conn = sqlite3.connect(str(self._path), timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _immediate(self) -> Iterator[sqlite3.Connection]:
        """A ``BEGIN IMMEDIATE`` transaction.

        It takes the write lock up front, so no other writer can slip between a
        read and the write that depends on it.
        """
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _init_db(self) -> None:
        """Create the schema, or verify the version of an existing one."""
        with self._immediate() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                " version INTEGER PRIMARY KEY,"
                " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is not None and row[0] != SCHEMA_VERSION:
                raise SnapshotStateError(
                    f"catalog schema version is {row[0]} but this code expects "
                    f"{SCHEMA_VERSION}. Being canonical, this catalog is never "
                    "recreated automatically — it needs a migration."
                )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tables ("
                " id TEXT PRIMARY KEY,"
                " workspace_id TEXT NOT NULL,"
                " logical_name TEXT NOT NULL,"
                " current_snapshot_id TEXT,"
                " revision INTEGER NOT NULL DEFAULT 0,"
                " UNIQUE (workspace_id, logical_name))"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS table_snapshots ("
                " id TEXT PRIMARY KEY,"
                " table_id TEXT NOT NULL REFERENCES tables(id),"
                " run_id TEXT NOT NULL,"
                " schema_version INTEGER NOT NULL,"
                " coverage_hash TEXT NOT NULL,"
                " artifact_digest TEXT NOT NULL,"
                " row_count INTEGER,"
                " state TEXT NOT NULL CHECK (state IN"
                "   ('staging','validated','committed','quarantined')),"
                " created_at TEXT NOT NULL,"
                " committed_at TEXT)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_table"
                " ON table_snapshots(table_id, created_at DESC)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS snapshot_leases ("
                " lease_id TEXT PRIMARY KEY,"
                " snapshot_id TEXT NOT NULL REFERENCES table_snapshots(id),"
                " acquired_at TEXT NOT NULL,"
                " expires_at TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_leases_snapshot"
                " ON snapshot_leases(snapshot_id, expires_at)"
            )
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    # ------------------------------------------------------------------ tables

    def create_table(
        self, workspace_id: str, logical_name: str, *, table_id: str | None = None
    ) -> TableRow:
        """Create a table, returning the existing one when the name is taken."""
        new_id = table_id or f"tbl_{uuid.uuid4().hex[:16]}"
        with self._immediate() as conn:
            existing = conn.execute(
                "SELECT id, workspace_id, logical_name, current_snapshot_id, revision"
                " FROM tables WHERE workspace_id = ? AND logical_name = ?",
                (workspace_id, logical_name),
            ).fetchone()
            if existing is not None:
                return TableRow(*existing)
            conn.execute(
                "INSERT INTO tables (id, workspace_id, logical_name, current_snapshot_id,"
                " revision) VALUES (?, ?, ?, NULL, 0)",
                (new_id, workspace_id, logical_name),
            )
        return TableRow(new_id, workspace_id, logical_name, None, 0)

    def get_table(self, table_id: str) -> TableRow:
        """Read a table row.

        Raises:
            TableNotFound: No such table.
        """
        row = self._conn.execute(
            "SELECT id, workspace_id, logical_name, current_snapshot_id, revision"
            " FROM tables WHERE id = ?",
            (table_id,),
        ).fetchone()
        if row is None:
            raise TableNotFound(f"no such table: {table_id!r}")
        return TableRow(*row)

    def list_tables(self, workspace_id: str | None = None) -> list[TableRow]:
        """List tables by name."""
        if workspace_id is None:
            rows = self._conn.execute(
                "SELECT id, workspace_id, logical_name, current_snapshot_id, revision"
                " FROM tables ORDER BY workspace_id, logical_name"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, workspace_id, logical_name, current_snapshot_id, revision"
                " FROM tables WHERE workspace_id = ? ORDER BY logical_name",
                (workspace_id,),
            ).fetchall()
        return [TableRow(*row) for row in rows]

    # --------------------------------------------------------------- snapshots

    def begin_snapshot(
        self,
        table_id: str,
        *,
        run_id: str,
        schema_version: int,
        coverage_hash: str,
        artifact_digest: str,
        row_count: int | None = None,
        snapshot_id: str | None = None,
    ) -> SnapshotRow:
        """Register a snapshot in ``staging`` state.

        Called before anything is written to disk, so that garbage collection does
        not mistake an in-progress staging directory for an orphan.
        """
        new_id = snapshot_id or f"snap_{uuid.uuid4().hex[:16]}"
        created = _now()
        with self._immediate() as conn:
            if conn.execute("SELECT 1 FROM tables WHERE id = ?", (table_id,)).fetchone() is None:
                raise TableNotFound(f"no such table: {table_id!r}")
            conn.execute(
                "INSERT INTO table_snapshots (id, table_id, run_id, schema_version,"
                " coverage_hash, artifact_digest, row_count, state, created_at, committed_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'staging', ?, NULL)",
                (
                    new_id,
                    table_id,
                    run_id,
                    schema_version,
                    coverage_hash,
                    artifact_digest,
                    row_count,
                    created,
                ),
            )
        return SnapshotRow(
            new_id,
            table_id,
            run_id,
            schema_version,
            coverage_hash,
            artifact_digest,
            row_count,
            "staging",
            created,
            None,
        )

    def get_snapshot(self, snapshot_id: str) -> SnapshotRow:
        """Read a snapshot row.

        Raises:
            SnapshotNotFound: No such snapshot.
        """
        row = self._conn.execute(
            "SELECT id, table_id, run_id, schema_version, coverage_hash, artifact_digest,"
            " row_count, state, created_at, committed_at FROM table_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
        return SnapshotRow(*row)

    def list_snapshots(self, table_id: str) -> list[SnapshotRow]:
        """List a table's snapshots, newest first."""
        rows = self._conn.execute(
            "SELECT id, table_id, run_id, schema_version, coverage_hash, artifact_digest,"
            " row_count, state, created_at, committed_at FROM table_snapshots"
            " WHERE table_id = ? ORDER BY created_at DESC, id DESC",
            (table_id,),
        ).fetchall()
        return [SnapshotRow(*row) for row in rows]

    def known_snapshot_ids(self, table_id: str) -> frozenset[str]:
        """Snapshot identifiers the catalog knows about.

        Passed to ``SnapshotLayout.orphan_staging_ids`` to tell orphans apart from
        work in progress.
        """
        rows = self._conn.execute(
            "SELECT id FROM table_snapshots WHERE table_id = ?", (table_id,)
        ).fetchall()
        return frozenset(row[0] for row in rows)

    def mark_validated(self, snapshot_id: str) -> None:
        """Move ``staging`` -> ``validated``.

        Records that pre-commit validation passed. ``commit_snapshot`` accepts only
        ``validated``, which closes the path by which an unvalidated snapshot could
        become current.
        """
        self._transition(snapshot_id, expected=("staging",), new="validated")

    def quarantine(self, snapshot_id: str) -> None:
        """Move a snapshot to ``quarantined``.

        The current snapshot cannot be quarantined — there would be nothing to read.
        """
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT table_id FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            current = conn.execute(
                "SELECT current_snapshot_id FROM tables WHERE id = ?", (row[0],)
            ).fetchone()
            if current is not None and current[0] == snapshot_id:
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is current and cannot be quarantined; "
                    "commit another snapshot to move the pointer first"
                )
            conn.execute(
                "UPDATE table_snapshots SET state = 'quarantined' WHERE id = ?", (snapshot_id,)
            )

    def _transition(self, snapshot_id: str, *, expected: tuple[str, ...], new: str) -> None:
        """Change state, refusing when the current state is not in ``expected``."""
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT state FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            if row[0] not in expected:
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is in state {row[0]!r}, expected one of {expected}"
                )
            conn.execute("UPDATE table_snapshots SET state = ? WHERE id = ?", (new, snapshot_id))

    # ------------------------------------------------------- compare-and-swap

    def commit_snapshot(self, snapshot_id: str, *, expected_revision: int) -> TableRow:
        """Commit a snapshot and move the table pointer to it.

        **One transaction**: marking the snapshot ``committed`` and moving the
        pointer either both happen or neither does. A crash in between leaves the
        pointer where it was, and the previous current snapshot stays readable.

        The files must already be at their final path
        (``SnapshotLayout.promote``). The order matters — moving the pointer before
        the files are in place makes it point at nothing.

        Args:
            snapshot_id: The snapshot to commit. Must be ``validated``.
            expected_revision: The table revision that was read. A mismatch is a
                conflict.

        Returns:
            The updated table row.

        Raises:
            SnapshotConflict: Another commit moved the pointer first. Not a retry.
            SnapshotStateError: The snapshot is not ``validated``.
            SnapshotNotFound: No such snapshot.
        """
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT table_id, state FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            table_id, state = row
            if state != "validated":
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is in state {state!r}; commit accepts only "
                    "'validated' so that an unvalidated snapshot cannot become current"
                )
            cursor = conn.execute(
                "UPDATE tables SET current_snapshot_id = ?, revision = revision + 1"
                " WHERE id = ? AND revision = ?",
                (snapshot_id, table_id, expected_revision),
            )
            if cursor.rowcount != 1:
                actual = conn.execute(
                    "SELECT revision FROM tables WHERE id = ?", (table_id,)
                ).fetchone()
                raise SnapshotConflict(
                    f"table {table_id!r} was expected at revision {expected_revision} but is "
                    f"at {actual[0] if actual else 'missing'}. Another refresh committed "
                    "first — do not retry; re-read the current state and decide whether "
                    "this refresh is still needed."
                )
            conn.execute(
                "UPDATE table_snapshots SET state = 'committed', committed_at = ? WHERE id = ?",
                (_now(), snapshot_id),
            )
            updated = conn.execute(
                "SELECT id, workspace_id, logical_name, current_snapshot_id, revision"
                " FROM tables WHERE id = ?",
                (table_id,),
            ).fetchone()
        return TableRow(*updated)

    # ------------------------------------------------------------------ leases

    def resolve_current(
        self, table_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> PinnedSnapshot:
        """Resolve the current snapshot once and take a lease on it.

        Resolution and the lease happen in the **same transaction**, so a commit
        cannot slip in between and let garbage collection remove a snapshot this
        query never got to pin.

        A query carries the result to completion: a snapshot committed while it
        runs does not change what it reads.

        Raises:
            TableNotFound: No such table.
            SnapshotNotFound: The table has no committed snapshot.
        """
        lease_id = f"lease_{secrets.token_hex(12)}"
        acquired = datetime.now(timezone.utc)
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT current_snapshot_id, revision FROM tables WHERE id = ?", (table_id,)
            ).fetchone()
            if row is None:
                raise TableNotFound(f"no such table: {table_id!r}")
            snapshot_id, revision = row
            if snapshot_id is None:
                raise SnapshotNotFound(f"table {table_id!r} has no committed snapshot")
            conn.execute(
                "INSERT INTO snapshot_leases (lease_id, snapshot_id, acquired_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    lease_id,
                    snapshot_id,
                    acquired.isoformat(),
                    (acquired + timedelta(seconds=lease_seconds)).isoformat(),
                ),
            )
        return PinnedSnapshot(snapshot_id, lease_id, revision)

    def release(self, lease_id: str) -> None:
        """Release a lease; do nothing when it is already gone."""
        with self._immediate() as conn:
            conn.execute("DELETE FROM snapshot_leases WHERE lease_id = ?", (lease_id,))

    @contextmanager
    def pinned(
        self, table_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> Iterator[PinnedSnapshot]:
        """``resolve_current`` that releases the lease on exit."""
        pin = self.resolve_current(table_id, lease_seconds=lease_seconds)
        try:
            yield pin
        finally:
            self.release(pin.lease_id)

    def live_lease_count(self, snapshot_id: str, *, now: str | None = None) -> int:
        """Count leases that have not expired.

        Expired leases do not count, so a crashed query cannot pin a snapshot
        forever.
        """
        moment = now or _now()
        row = self._conn.execute(
            "SELECT COUNT(*) FROM snapshot_leases WHERE snapshot_id = ? AND expires_at > ?",
            (snapshot_id, moment),
        ).fetchone()
        return int(row[0])

    def purge_expired_leases(self, *, now: str | None = None) -> int:
        """Delete expired leases and return how many went."""
        moment = now or _now()
        with self._immediate() as conn:
            cursor = conn.execute("DELETE FROM snapshot_leases WHERE expires_at <= ?", (moment,))
        return cursor.rowcount

    # -------------------------------------------------------------- deletion

    def assert_deletable(self, snapshot_id: str, *, now: str | None = None) -> SnapshotRow:
        """Check that a snapshot may be deleted.

        Refuses the current snapshot and any snapshot under a live lease. This is
        the gate garbage collection goes through.

        Raises:
            ImmutableSnapshot: It is the current snapshot.
            SnapshotInUse: A query is reading it.
            SnapshotNotFound: No such snapshot.
        """
        snapshot = self.get_snapshot(snapshot_id)
        table = self.get_table(snapshot.table_id)
        if table.current_snapshot_id == snapshot_id:
            raise ImmutableSnapshot(
                f"snapshot {snapshot_id!r} is the current snapshot of table "
                f"{snapshot.table_id!r}; deleting it would leave nothing to read"
            )
        live = self.live_lease_count(snapshot_id, now=now)
        if live:
            raise SnapshotInUse(
                f"snapshot {snapshot_id!r} has {live} live lease(s); a query is reading it"
            )
        return snapshot

    def forget_snapshot(self, snapshot_id: str) -> None:
        """Remove a snapshot row from the catalog.

        Call this **after** the files are gone. The other order leaves files the
        catalog no longer knows about, which is the definition of an orphan.
        """
        with self._immediate() as conn:
            conn.execute("DELETE FROM snapshot_leases WHERE snapshot_id = ?", (snapshot_id,))
            conn.execute("DELETE FROM table_snapshots WHERE id = ?", (snapshot_id,))

    def close(self) -> None:
        """Close this thread's connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            del self._local.conn


__all__ = [
    "CATALOG_FILENAME",
    "DEFAULT_LEASE_SECONDS",
    "SCHEMA_VERSION",
    "PinnedSnapshot",
    "SnapshotRow",
    "SnapshotState",
    "TableCatalog",
    "TableRow",
]

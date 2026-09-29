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

    snapshot_holds
      hold_id, snapshot_id, kind, reason, created_at, expires_at

``state``: ``staging`` -> ``validated`` -> ``committed``, or ``quarantined``.
A snapshot that lost a compare-and-swap, or that a crash left behind, becomes
``abandoned`` so garbage collection can reclaim it — a ``validated`` row that
nobody will ever commit would otherwise keep its files forever.
``retiring`` marks a snapshot being deleted: no new lease is issued for one.

A lease protects a snapshot while a query reads it. A **hold** protects it for
longer than any query: a saved analysis that must re-run on the same input, a
retention period, an audit (#705). Garbage collection refuses both, in the same
transaction that would retire the snapshot.

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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

from .errors import (
    ImmutableSnapshot,
    SnapshotConflict,
    SnapshotHeld,
    SnapshotInUse,
    SnapshotNotFound,
    SnapshotStateError,
    TableNotFound,
)

# Schema version. Being canonical, this catalog cannot be dropped and recreated
# when the version changes — it needs a migration. Every step lives in _MIGRATIONS,
# and a version with no path to the current one refuses to open rather than
# discarding data silently.
#
# 2: baseline scoping columns on table_snapshots (#700) — owner_id,
#    coverage_fingerprint, source_params_fingerprint, schema_contract_version.
# 3: 'abandoned' and 'retiring' states (#699 N-01, N-03). SQLite cannot alter a
#    CHECK constraint, so the table is rebuilt and copied row for row.
# 4: snapshot_holds (#705) — saved analyses, retention and audit keep a snapshot
#    past garbage collection.
SCHEMA_VERSION = 4

CATALOG_FILENAME = "_warehouse.sqlite"

# Column order for every snapshot read. One list rather than four copies of the
# same SELECT, because SnapshotRow is positional and a drifting order would
# silently put a fingerprint in the wrong field.
_SNAPSHOT_COLUMNS = (
    "id, table_id, run_id, schema_version, coverage_hash, artifact_digest,"
    " row_count, state, created_at, committed_at, owner_id, coverage_fingerprint,"
    " source_params_fingerprint, schema_contract_version"
)

SnapshotState = Literal[
    "staging",
    "validated",
    "committed",
    "quarantined",
    "abandoned",
    "retiring",
]

# Default lease lifetime, so a crashed query cannot pin a snapshot forever.
DEFAULT_LEASE_SECONDS = 3600

HoldKind = Literal["saved_analysis", "retention", "audit"]
"""Why a snapshot is kept past garbage collection (#705).

``saved_analysis`` — a stored query run pinned this snapshot and must be able to
re-run against it. ``retention`` — a policy keeps it for a period. ``audit`` — it was
named as evidence. The kinds behave the same; they exist so a report can say *why*
something could not be reclaimed.
"""

HOLD_KINDS: tuple[HoldKind, ...] = ("saved_analysis", "retention", "audit")

_HOLDS_TABLE = (
    "CREATE TABLE IF NOT EXISTS snapshot_holds ("
    " hold_id TEXT PRIMARY KEY,"
    " snapshot_id TEXT NOT NULL REFERENCES table_snapshots(id),"
    " kind TEXT NOT NULL CHECK (kind IN ('saved_analysis','retention','audit')),"
    " reason TEXT NOT NULL,"
    " created_at TEXT NOT NULL,"
    " expires_at TEXT)"
)
_HOLDS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_holds_snapshot ON snapshot_holds(snapshot_id, expires_at)"
)

# A hold with no expiry lasts until it is released. Leases always expire; holds need
# not, because an audit does not end on a timer.
# Compared as instants, not as text (#790): an ISO string with a -10:00 offset sorts
# before a UTC one that is hours earlier, so a text comparison called a live hold
# expired and let collection take a snapshot it was protecting. julianday() reads the
# offset; rows written before this fix are compared correctly too.
_LIVE_HOLD = "snapshot_id = ? AND (expires_at IS NULL OR julianday(expires_at) > julianday(?))"
_LIVE_LEASE = "snapshot_id = ? AND julianday(expires_at) > julianday(?)"


# Each entry upgrades from the version that is its key to the next one. A column is
# added with SQL rather than by rebuilding: this catalog is canonical, so a
# migration preserves every row it touches.
_MIGRATIONS: Mapping[int, tuple[str, ...]] = {
    1: (
        "ALTER TABLE table_snapshots ADD COLUMN owner_id TEXT",
        "ALTER TABLE table_snapshots ADD COLUMN coverage_fingerprint TEXT",
        "ALTER TABLE table_snapshots ADD COLUMN source_params_fingerprint TEXT",
        "ALTER TABLE table_snapshots ADD COLUMN schema_contract_version TEXT",
    ),
    # A CHECK constraint cannot be altered in SQLite, so widening the state vocabulary
    # means rebuilding the table. Every row is copied, which is the point: this catalog
    # is canonical and a migration that loses rows is not a migration.
    2: (
        "ALTER TABLE table_snapshots RENAME TO table_snapshots_v2",
        "CREATE TABLE table_snapshots ("
        " id TEXT PRIMARY KEY,"
        " table_id TEXT NOT NULL REFERENCES tables(id),"
        " run_id TEXT NOT NULL,"
        " schema_version INTEGER NOT NULL,"
        " coverage_hash TEXT NOT NULL,"
        " artifact_digest TEXT NOT NULL,"
        " row_count INTEGER,"
        " state TEXT NOT NULL CHECK (state IN"
        "   ('staging','validated','committed','quarantined','abandoned','retiring')),"
        " created_at TEXT NOT NULL,"
        " committed_at TEXT,"
        " owner_id TEXT,"
        " coverage_fingerprint TEXT,"
        " source_params_fingerprint TEXT,"
        " schema_contract_version TEXT)",
        "INSERT INTO table_snapshots SELECT id, table_id, run_id, schema_version,"
        " coverage_hash, artifact_digest, row_count, state, created_at, committed_at,"
        " owner_id, coverage_fingerprint, source_params_fingerprint,"
        " schema_contract_version FROM table_snapshots_v2",
        "DROP TABLE table_snapshots_v2",
        "CREATE INDEX IF NOT EXISTS idx_snapshots_table"
        " ON table_snapshots(table_id, created_at DESC)",
    ),
    3: (_HOLDS_TABLE, _HOLDS_INDEX),
}


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
    """One snapshot row in the catalog.

    The last four fields exist so that drift can pick a baseline that is actually
    comparable (#700). Each is optional, because a snapshot recorded before those
    columns existed has none, and a missing value must read as "cannot compare"
    rather than as a match.

    Attributes:
        owner_id: Who produced this snapshot. A baseline never crosses owners —
            row counts and schema are metadata about someone else's data.
        coverage_fingerprint: What population was collected (region, period,
            parameter set). Two snapshots with different fingerprints are not
            comparable by volume.
        source_params_fingerprint: The request parameters behind the collection.
        schema_contract_version: The schema contract in force. A volume baseline
            across a contract change is compared silently otherwise.
    """

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
    owner_id: str | None = None
    coverage_fingerprint: str | None = None
    source_params_fingerprint: str | None = None
    schema_contract_version: str | None = None


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


@dataclass(frozen=True)
class SnapshotHold:
    """A reason a snapshot must survive garbage collection (#705).

    Attributes:
        hold_id: Identifier to pass to ``release_hold``.
        snapshot_id: The held snapshot.
        kind: Why it is held.
        reason: Free text a person can act on — which analysis, which audit.
        created_at: When the hold was placed.
        expires_at: When it lapses, or None for "until released".
    """

    hold_id: str
    snapshot_id: str
    kind: HoldKind
    reason: str
    created_at: str
    expires_at: str | None


def _utc_instant(value: str, *, field: str) -> str:
    """``value`` as a UTC ISO 8601 string; an instant without an offset is refused (#790).

    A naive time means whatever the writing machine's clock zone was, which the next
    reader cannot know — the same ambiguity that made hold expiry wrong.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SnapshotStateError(f"{field} is not an ISO 8601 time: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotStateError(f"{field} needs a UTC offset: {value!r}")
    return parsed.astimezone(timezone.utc).isoformat()


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
    def path(self) -> Path:
        """The catalog database file. Backup copies it through SQLite, not the filesystem."""
        return self._path

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
                self._migrate(conn, int(row[0]))
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
                "   ('staging','validated','committed','quarantined',"
                "    'abandoned','retiring')),"
                " created_at TEXT NOT NULL,"
                " committed_at TEXT,"
                " owner_id TEXT,"
                " coverage_fingerprint TEXT,"
                " source_params_fingerprint TEXT,"
                " schema_contract_version TEXT)"
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
            conn.execute(_HOLDS_TABLE)
            conn.execute(_HOLDS_INDEX)
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def _migrate(self, conn: sqlite3.Connection, found: int) -> None:
        """Walk an older catalog up to ``SCHEMA_VERSION``.

        Runs inside the caller's ``BEGIN IMMEDIATE``, so a failure part way leaves
        the catalog on the version it started from rather than somewhere between two.

        A version this code cannot reach — newer than it knows, or older with a gap
        in the chain — refuses to open. Being canonical, the catalog is never
        recreated to make a mismatch go away.

        Raises:
            SnapshotStateError: There is no path from ``found`` to the current
                version.
        """
        version = found
        while version != SCHEMA_VERSION:
            steps = _MIGRATIONS.get(version)
            if steps is None:
                raise SnapshotStateError(
                    f"catalog schema version is {found} and this code expects "
                    f"{SCHEMA_VERSION}, with no migration from {version}. Being "
                    "canonical, this catalog is never recreated automatically."
                )
            for statement in steps:
                conn.execute(statement)
            version += 1
        conn.execute("DELETE FROM schema_version")
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

    def table_revision(self, workspace_id: str, logical_name: str) -> int:
        """The table's current revision, or 0 when it does not exist yet (#787).

        A build reads this when it starts and commits against it, so a refresh that
        another build finished in the meantime is a conflict rather than overwritten.
        0 matches a table created later at revision 0, so the first build of a table
        still commits.
        """
        row = self._conn.execute(
            "SELECT revision FROM tables WHERE workspace_id = ? AND logical_name = ?",
            (workspace_id, logical_name),
        ).fetchone()
        return int(row[0]) if row is not None else 0

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
        owner_id: str | None = None,
        coverage_fingerprint: str | None = None,
        source_params_fingerprint: str | None = None,
        schema_contract_version: str | None = None,
    ) -> SnapshotRow:
        """Register a snapshot in ``staging`` state.

        Called before anything is written to disk, so that garbage collection does
        not mistake an in-progress staging directory for an orphan.

        The last four arguments are what makes this snapshot eligible as a drift
        baseline (#700). Leaving one out is allowed and means "cannot compare on
        that axis" — baseline selection treats a missing value as a mismatch rather
        than as a match, so an unlabelled snapshot is never silently compared.
        """
        new_id = snapshot_id or f"snap_{uuid.uuid4().hex[:16]}"
        created = _now()
        with self._immediate() as conn:
            if conn.execute("SELECT 1 FROM tables WHERE id = ?", (table_id,)).fetchone() is None:
                raise TableNotFound(f"no such table: {table_id!r}")
            conn.execute(
                "INSERT INTO table_snapshots (id, table_id, run_id, schema_version,"
                " coverage_hash, artifact_digest, row_count, state, created_at,"
                " committed_at, owner_id, coverage_fingerprint,"
                " source_params_fingerprint, schema_contract_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'staging', ?, NULL, ?, ?, ?, ?)",
                (
                    new_id,
                    table_id,
                    run_id,
                    schema_version,
                    coverage_hash,
                    artifact_digest,
                    row_count,
                    created,
                    owner_id,
                    coverage_fingerprint,
                    source_params_fingerprint,
                    schema_contract_version,
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
            owner_id,
            coverage_fingerprint,
            source_params_fingerprint,
            schema_contract_version,
        )

    def get_snapshot(self, snapshot_id: str) -> SnapshotRow:
        """Read a snapshot row.

        Raises:
            SnapshotNotFound: No such snapshot.
        """
        row = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM table_snapshots WHERE id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
        return SnapshotRow(*row)

    def list_snapshots(self, table_id: str) -> list[SnapshotRow]:
        """List a table's snapshots, newest first."""
        rows = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM table_snapshots"
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

    def set_artifact_digest(self, snapshot_id: str, digest: str) -> None:
        """Record the digest of what was actually written.

        The digest can only be known once the bytes are in staging, so
        ``begin_snapshot`` cannot take it — a digest recorded before the write
        describes something that may not have happened. This is the legitimate path
        for filling it in, and it is refused once the snapshot is committed: a
        committed snapshot's digest is what ``verify_before_commit`` checks against,
        and letting it be rewritten would make the check circular.

        Raises:
            SnapshotStateError: The snapshot is already committed or beyond.
            SnapshotNotFound: No such snapshot.
        """
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT state FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            if row[0] not in ("staging", "validated"):
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is in state {row[0]!r}; its digest is what "
                    "verification compares against and cannot be rewritten"
                )
            conn.execute(
                "UPDATE table_snapshots SET artifact_digest = ? WHERE id = ?",
                (digest, snapshot_id),
            )

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

    def abandon(self, snapshot_id: str) -> None:
        """Mark a snapshot nobody will commit, so its files can be reclaimed.

        Two things produce one. A compare-and-swap loser stays ``validated`` for ever
        — the caller is told not to retry, so nothing moves it again. And a crash
        between ``begin_snapshot`` and a commit leaves ``staging`` or ``validated``
        behind. Neither is an orphan the layout can spot, because the catalog knows
        about them, so garbage collection skipped them and their bytes stayed on disk.

        Committed snapshots cannot be abandoned — use ``quarantine``, which refuses the
        current one. A snapshot already ``abandoned`` is accepted so that a retried
        cleanup is not an error.

        Raises:
            SnapshotStateError: The snapshot is committed, quarantined or retiring.
            SnapshotNotFound: No such snapshot.
        """
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT state FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            if row[0] == "abandoned":
                return
            if row[0] not in ("staging", "validated"):
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is in state {row[0]!r}; only an "
                    "uncommitted snapshot can be abandoned"
                )
            conn.execute(
                "UPDATE table_snapshots SET state = 'abandoned' WHERE id = ?", (snapshot_id,)
            )

    def stale_uncommitted(self, table_id: str, *, before: str) -> list[SnapshotRow]:
        """Uncommitted snapshots created before ``before``.

        The age cut-off is what separates a crash from work in progress. The catalog
        cannot tell them apart by state alone — a ``staging`` row is what both look
        like — so the caller decides how long a build is allowed to take.

        Args:
            table_id: The table to examine.
            before: An ISO 8601 timestamp. Snapshots created at or after it are left
                alone.
        """
        rows = self._conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM table_snapshots"
            " WHERE table_id = ? AND state IN ('staging','validated') AND created_at < ?"
            " ORDER BY created_at",
            (table_id, before),
        ).fetchall()
        return [SnapshotRow(*row) for row in rows]

    def begin_retiring(self, snapshot_id: str, *, now: str | None = None) -> SnapshotRow:
        """Move a snapshot to ``retiring``, refusing if a lease is live.

        The check and the transition happen in **one transaction**, which is the whole
        point. ``assert_deletable`` followed by a delete has a gap: a query can resolve
        the pointer and take a lease between the two, and then its snapshot is deleted
        underneath it. Nothing issues a lease for a ``retiring`` snapshot, so once this
        returns the snapshot cannot gain a reader.

        Raises:
            ImmutableSnapshot: It is the table's current snapshot.
            SnapshotInUse: A live lease exists.
            SnapshotHeld: A live hold exists (#705).
            SnapshotNotFound: No such snapshot.
        """
        moment = now or _now()
        with self._immediate() as conn:
            row = conn.execute(
                f"SELECT {_SNAPSHOT_COLUMNS} FROM table_snapshots WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            snapshot = SnapshotRow(*row)
            current = conn.execute(
                "SELECT current_snapshot_id FROM tables WHERE id = ?", (snapshot.table_id,)
            ).fetchone()
            if current is not None and current[0] == snapshot_id:
                raise ImmutableSnapshot(
                    f"snapshot {snapshot_id!r} is the current snapshot of table "
                    f"{snapshot.table_id!r}; deleting it would leave nothing to read"
                )
            live = conn.execute(
                f"SELECT COUNT(*) FROM snapshot_leases WHERE {_LIVE_LEASE}",
                (snapshot_id, moment),
            ).fetchone()[0]
            if live:
                raise SnapshotInUse(
                    f"snapshot {snapshot_id!r} has {live} live lease(s); a query is reading it"
                )
            held = conn.execute(
                f"SELECT kind, reason FROM snapshot_holds WHERE {_LIVE_HOLD} ORDER BY created_at",
                (snapshot_id, moment),
            ).fetchall()
            if held:
                raise SnapshotHeld(
                    f"snapshot {snapshot_id!r} is held: "
                    + "; ".join(f"{kind} ({reason})" for kind, reason in held)
                )
            conn.execute(
                "UPDATE table_snapshots SET state = 'retiring' WHERE id = ?", (snapshot_id,)
            )
        return snapshot

    def pin(
        self, snapshot_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> PinnedSnapshot:
        """Take a lease on a specific snapshot rather than on whatever is current.

        Querying a snapshot by id — a saved analysis, a restore, a comparison against
        a past state — needs the same protection from garbage collection that
        ``resolve_current`` gives. Without this, such a query had no lease at all.

        A ``retiring`` snapshot is refused: it is on its way out, and handing it a new
        reader is how a delete races a query.

        Raises:
            SnapshotStateError: The snapshot is retiring or was never committed.
            SnapshotNotFound: No such snapshot.
        """
        lease_id = f"lease_{secrets.token_hex(12)}"
        acquired = datetime.now(timezone.utc)
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT table_id, state FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            table_id, state = row
            if state == "retiring":
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is being deleted; no new lease is issued for it"
                )
            if state not in ("committed", "quarantined"):
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is in state {state!r}; only a snapshot "
                    "that was committed can be read"
                )
            revision = conn.execute(
                "SELECT revision FROM tables WHERE id = ?", (table_id,)
            ).fetchone()[0]
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

    def place_hold(
        self,
        snapshot_id: str,
        *,
        kind: HoldKind,
        reason: str,
        expires_at: str | None = None,
    ) -> SnapshotHold:
        """Keep a snapshot past garbage collection until released or ``expires_at``.

        Only a snapshot someone could read is held — a committed or quarantined one.
        Holding a staging row would keep bytes nobody can query, and holding a
        ``retiring`` one would race the delete already under way.

        Raises:
            SnapshotStateError: The snapshot was never committed, is retiring, the
                kind is unknown, the reason is empty, or ``expires_at`` is not an
                ISO 8601 time with a UTC offset.
            SnapshotNotFound: No such snapshot.
        """
        if kind not in HOLD_KINDS:
            raise SnapshotStateError(f"unknown hold kind {kind!r}; expected one of {HOLD_KINDS}")
        if not reason.strip():
            raise SnapshotStateError(
                "a hold needs a reason — an unexplained hold is one nobody dares release"
            )
        hold = SnapshotHold(
            hold_id=f"hold_{secrets.token_hex(12)}",
            snapshot_id=snapshot_id,
            kind=kind,
            reason=reason,
            created_at=_now(),
            # Stored as UTC so the row reads the same to every later comparison (#790).
            expires_at=(
                _utc_instant(expires_at, field="expires_at") if expires_at is not None else None
            ),
        )
        with self._immediate() as conn:
            row = conn.execute(
                "SELECT state FROM table_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if row is None:
                raise SnapshotNotFound(f"no such snapshot: {snapshot_id!r}")
            if row[0] not in ("committed", "quarantined"):
                raise SnapshotStateError(
                    f"snapshot {snapshot_id!r} is in state {row[0]!r}; only a snapshot "
                    "that was committed can be held"
                )
            conn.execute(
                "INSERT INTO snapshot_holds (hold_id, snapshot_id, kind, reason, created_at,"
                " expires_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    hold.hold_id,
                    hold.snapshot_id,
                    hold.kind,
                    hold.reason,
                    hold.created_at,
                    hold.expires_at,
                ),
            )
        return hold

    def release_hold(self, hold_id: str) -> None:
        """Release a hold; do nothing when it is already gone."""
        with self._immediate() as conn:
            conn.execute("DELETE FROM snapshot_holds WHERE hold_id = ?", (hold_id,))

    def live_holds(self, snapshot_id: str, *, now: str | None = None) -> list[SnapshotHold]:
        """Holds on a snapshot that have not lapsed, oldest first."""
        rows = self._conn.execute(
            "SELECT hold_id, snapshot_id, kind, reason, created_at, expires_at"
            f" FROM snapshot_holds WHERE {_LIVE_HOLD} ORDER BY created_at",
            (snapshot_id, now or _now()),
        ).fetchall()
        return [SnapshotHold(*row) for row in rows]

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

    def _verify_promoted(self, snapshot_id: str) -> None:
        """Re-read a promoted snapshot and check it against what was recorded.

        Verification happens **outside** the commit transaction on purpose: reading a
        directory is slow, and holding the write lock for it would block every other
        commit on the catalog. The window that opens is acceptable — the files are
        already immutable at this point, so nothing legitimate changes them, and the
        check is against damage rather than against a concurrent writer.

        Raises:
            SnapshotStateError: The directory is empty, or its digest does not match.
            SnapshotNotFound: No such snapshot.
        """
        from .layout import SnapshotLayout, content_digest, is_empty

        snapshot = self.get_snapshot(snapshot_id)
        directory = SnapshotLayout(self._root, snapshot.table_id).snapshot_dir(snapshot_id)
        if is_empty(directory):
            raise SnapshotStateError(
                f"snapshot {snapshot_id!r} has no files. Committing it would replace the "
                "table with emptiness, which is worse than failing the refresh."
            )
        actual = content_digest(directory)
        if actual != snapshot.artifact_digest:
            raise SnapshotStateError(
                f"snapshot {snapshot_id!r} does not match its recorded digest "
                f"({snapshot.artifact_digest} recorded, {actual} on disk). The files "
                "changed after they were promoted."
            )

    def commit_snapshot(
        self,
        snapshot_id: str,
        *,
        expected_revision: int,
        verify_before_commit: bool = False,
    ) -> TableRow:
        """Commit a snapshot and move the table pointer to it.

        **One transaction**: marking the snapshot ``committed`` and moving the
        pointer either both happen or neither does. A crash in between leaves the
        pointer where it was, and the previous current snapshot stays readable.

        The files must already be at their final path
        (``SnapshotLayout.promote``). The order matters — moving the pointer before
        the files are in place makes it point at nothing.

        Pass ``verify_before_commit=True`` to re-read the promoted directory and check
        it against the recorded digest. Without it this only trusts the catalog's own
        state, so a snapshot that was promoted and then damaged — or that a build left
        empty while reporting success — becomes current anyway. Committing an empty
        snapshot replaces a table with emptiness, which is worse than failing.

        Args:
            snapshot_id: The snapshot to commit. Must be ``validated``.
            expected_revision: The table revision that was read. A mismatch is a
                conflict.
            verify_before_commit: Re-read the files and compare their digest to the one
                recorded, and refuse an empty directory.

        Returns:
            The updated table row.

        Raises:
            SnapshotConflict: Another commit moved the pointer first. Not a retry.
            SnapshotStateError: The snapshot is not ``validated``, or verification
                found the files empty or changed.
            SnapshotNotFound: No such snapshot.
        """
        if verify_before_commit:
            self._verify_promoted(snapshot_id)
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
            f"SELECT COUNT(*) FROM snapshot_leases WHERE {_LIVE_LEASE}",
            (snapshot_id, moment),
        ).fetchone()
        return int(row[0])

    def purge_expired_leases(self, *, now: str | None = None) -> int:
        """Delete expired leases and return how many went."""
        moment = now or _now()
        with self._immediate() as conn:
            cursor = conn.execute(
                "DELETE FROM snapshot_leases WHERE julianday(expires_at) <= julianday(?)",
                (moment,),
            )
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
        if self.live_holds(snapshot_id, now=now):
            raise SnapshotHeld(f"snapshot {snapshot_id!r} is held and cannot be deleted")
        return snapshot

    def forget_snapshot(self, snapshot_id: str) -> None:
        """Remove a snapshot row from the catalog.

        Call this **after** the files are gone. The other order leaves files the
        catalog no longer knows about, which is the definition of an orphan.
        """
        with self._immediate() as conn:
            conn.execute("DELETE FROM snapshot_leases WHERE snapshot_id = ?", (snapshot_id,))
            # Only lapsed holds can remain: a live one made begin_retiring refuse.
            conn.execute("DELETE FROM snapshot_holds WHERE snapshot_id = ?", (snapshot_id,))
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
    "HOLD_KINDS",
    "SCHEMA_VERSION",
    "HoldKind",
    "PinnedSnapshot",
    "SnapshotHold",
    "SnapshotRow",
    "SnapshotState",
    "TableCatalog",
    "TableRow",
]

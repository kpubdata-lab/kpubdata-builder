"""Build index (#309, ADR 0003; backend split ADR 0010/0016).

Index completed build metadata to improve list query performance.
manifest.json is canonical; this index is derivative.

``BuildIndex`` is Protocol (interface); default implementation is single-file SQLite-based
``SqliteBuildIndex`` (no-external-deps default). CUBRID backend (``CubridBuildIndex``,
ADR 0016) is in ``build_index_cubrid``; ``make_build_index()`` factory selects
based on ``KPUBDATA_BUILDER_STORAGE_BACKEND``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Collection, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

from .schema_version import UnsupportedSchemaVersionError, says_damaged, stored_version

if TYPE_CHECKING:
    _BaseConn = sqlite3.Connection
else:
    _BaseConn = object

# Schema version: increments on index structure change
# Schema version 2: extends status vocabulary to ok/failed/cancelled (#334 async job model)
# Schema version 4: added dataset_id column (#488). Canonical is BuildSpec snapshot (#487),
# this column is only derived lookup value for dataset→run query performance.
# Schema version 5: added owner_id column (#505). Canonical is manifest.json, this column
# is only derived lookup value for canonical stable owner identity. This index is derived,
# so schema change DROP and recreate table — existing index data lost but
# rebuilding via rebuild_index() from manifest.json restores it.
SCHEMA_VERSION = 5

#: Run ids per ``IN (...)`` lookup in ``count_builds`` — under SQLite's 999-variable
#: floor on old builds, and a size any backend takes.
_IN_CHUNK = 500

# Build index status vocabulary. ADR 0003 derived cache. manifest.json is canonical.
BuildStatus = Literal["ok", "failed", "cancelled"]

# Index filename
_INDEX_FILENAME = "_builds.sqlite"

#: What to do about an index a newer release wrote. The index is derived, so this
#: release can make its own — but only when asked to.
_NEWER_INDEX_REMEDY = (
    "Run the release that wrote it, or rebuild the index for this release from the "
    "run manifests with `kpubdata-builder rebuild-index`."
)


@dataclass(frozen=True)
class BuildEntry:
    """Build index entry."""

    run_id: str
    status: BuildStatus
    started_at: str | None
    finished_at: str | None
    spec_digest: str | None
    error: str | None
    created_by: str | None = None
    dataset_id: str | None = None
    owner_id: str | None = None


class BuildIndex(Protocol):
    """Build index interface (ADR 0010/0016).

    ``SqliteBuildIndex`` (default) and ``CubridBuildIndex`` implement this Protocol.
    ADR 0003 contract: manifest.json is canonical, index is derivative; index write
    failure must not cause build failure (``insert_or_replace``/``delete`` swallow
    exceptions).
    """

    def insert_or_replace(
        self,
        run_id: str,
        status: BuildStatus,
        started_at: str | None,
        finished_at: str | None,
        spec_digest: str | None = None,
        error: str | None = None,
        created_by: str | None = None,
        dataset_id: str | None = None,
        owner_id: str | None = None,
    ) -> None: ...

    def list_builds(self, limit: int | None = 50) -> list[BuildEntry]: ...

    def list_by_dataset(self, dataset_id: str, limit: int | None = None) -> list[BuildEntry]: ...

    def list_recent_owned(
        self, *, limit: int, principal_owner_id: str | None, principal_label: str
    ) -> list[BuildEntry]: ...

    def list_between(self, start_iso: str, end_iso: str) -> list[BuildEntry]: ...

    def latest_successful_finished_at(self) -> str | None: ...

    def count_builds(self, also: Collection[str] = ()) -> int:
        """How many distinct runs are indexed or named in ``also`` (#948)."""
        ...

    def get(self, run_id: str) -> BuildEntry | None: ...

    def delete(self, run_id: str) -> None: ...

    def close(self) -> None: ...


class SqliteBuildIndex:
    """Single-file SQLite-based build index (ADR 0003, default implementation).

    Per ADR 0003:
    - manifest.json is canonical; this index is derivative
    - Index write failure must not cause build failure
    - WAL mode + busy_timeout for concurrency safety
    """

    def __init__(self, output_root: Path, *, index_path: Path | None = None) -> None:
        """Initialize index.

        Args:
            output_root: Build output root directory (index at output_root/_builds.sqlite)
            index_path: Index file path override (temp file for rebuild_index atomic replace, etc)
        """
        self._output_root = output_root
        self._index_path = index_path if index_path is not None else output_root / _INDEX_FILENAME
        self._local = threading.local()
        # Before the first ordinary connection, which sets the journal mode.
        self._refuse_newer(stored_version(self._index_path))
        self._init_db()

    def _refuse_newer(self, found: int | None) -> None:
        """Refuse an index a newer release wrote (#1096).

        An older index is dropped and made again; a newer one is not. Dropping it would
        leave the release that wrote it with an empty index after this one stops, and
        nothing would say so.
        """
        if found is None or found <= SCHEMA_VERSION:
            return
        raise UnsupportedSchemaVersionError(
            store="build index",
            location=str(self._index_path),
            found=found,
            supported=SCHEMA_VERSION,
            remedy=_NEWER_INDEX_REMEDY,
        )

    @property
    def _conn(self) -> sqlite3.Connection:
        """Return thread-local connection (lazy initialization)."""
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return cast(sqlite3.Connection, self._local.conn)

    def _connect(self) -> sqlite3.Connection:
        """Create and configure new SQLite connection."""
        conn = sqlite3.connect(
            str(self._index_path),
            timeout=30.0,  # busy_timeout: wait time on concurrency contention
        )
        conn.execute("PRAGMA journal_mode=WAL")  # Write-Ahead Logging: allow concurrent reads
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        """Initialize database schema."""
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            # Check schema version
            cur = self._conn.execute("SELECT MAX(version) FROM schema_version")
            current_version = cur.fetchone()[0]

            self._refuse_newer(int(current_version) if current_version is not None else None)
            if current_version != SCHEMA_VERSION:
                # Create builds table (existing table DROP and recreate)
                self._conn.execute("DROP TABLE IF EXISTS builds")
                self._conn.execute(
                    """
                    CREATE TABLE builds (
                        run_id TEXT PRIMARY KEY,
                        status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'cancelled')),
                        started_at TEXT,
                        finished_at TEXT,
                        spec_digest TEXT,
                        error TEXT,
                        created_by TEXT,
                        dataset_id TEXT,
                        owner_id TEXT
                    )
                    """
                )
                # finished_at index (latest builds first query)
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_builds_finished_at ON builds(finished_at DESC)"
                )
                # dataset_id index (#488): dataset→run query. legacy runs without snapshot have
                # dataset_id NULL, naturally excluded from dataset grouping.
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_builds_dataset_id ON builds(dataset_id)"
                )
                # Record schema version
                self._conn.execute(
                    f"INSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION})"
                )
                self._conn.execute(
                    "DELETE FROM schema_version WHERE version != ?", (SCHEMA_VERSION,)
                )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        """Transaction context manager."""
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def insert_or_replace(
        self,
        run_id: str,
        status: BuildStatus,
        started_at: str | None,
        finished_at: str | None,
        spec_digest: str | None = None,
        error: str | None = None,
        created_by: str | None = None,
        dataset_id: str | None = None,
        owner_id: str | None = None,
    ) -> None:
        """Insert or replace build entry.

        Args:
            run_id: Build execution identifier
            status: Build state (ok/failed)
            started_at: Build start time (ISO 8601)
            finished_at: Build completion time (ISO 8601)
            spec_digest: spec hash (optional)
            error: Error message (on failure)
            created_by: Build requester identity label (optional, #388)
            dataset_id: BuildSpec.dataset_id (optional, #488). Legacy runs without
                snapshot are None — don't guess/fill dataset_id.
            owner_id: canonical stable owner identity (optional, #505). Legacy runs
                without this field in manifest.json are None.
        """
        try:
            with self._transaction():
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO builds
                    (run_id, status, started_at, finished_at, spec_digest, error, created_by,
                     dataset_id, owner_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        status,
                        started_at,
                        finished_at,
                        spec_digest,
                        error,
                        created_by,
                        dataset_id,
                        owner_id,
                    ),
                )
        except Exception:
            # ADR 0003: index write failure must not cause build failure
            pass

    def list_builds(self, limit: int | None = 50) -> list[BuildEntry]:
        """Return builds sorted by latest finished_at descending.

        Args:
            limit: Max builds to return. None returns all builds.

        Returns:
            BuildEntry list (newest finished_at first)
        """
        sql = """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            ORDER BY finished_at DESC
        """
        if limit is None:
            cur = self._conn.execute(sql)
        else:
            cur = self._conn.execute(f"{sql} LIMIT ?", (limit,))
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def list_by_dataset(self, dataset_id: str, limit: int | None = None) -> list[BuildEntry]:
        """Return builds for dataset_id sorted by latest finished_at descending (#488).

        Args:
            dataset_id: BuildSpec.dataset_id value (exact match only).
            limit: Max builds to return. None returns all builds for this dataset.

        Returns:
            BuildEntry list (newest finished_at first). Empty if no runs indexed
            under this dataset_id.
        """
        sql = """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            WHERE dataset_id = ?
            ORDER BY finished_at DESC
        """
        if limit is None:
            cur = self._conn.execute(sql, (dataset_id,))
        else:
            cur = self._conn.execute(f"{sql} LIMIT ?", (dataset_id, limit))
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def list_recent_owned(
        self, *, limit: int, principal_owner_id: str | None, principal_label: str
    ) -> list[BuildEntry]:
        """Return up to ``limit`` builds owned by principal, latest finished first (#527).

        Apply ownership filter before LIMIT at SQL WHERE stage, not after LIMIT in Python.
        If we LIMIT first (e.g., get top 10 globally then filter), other principals'
        recent runs fill the LIMIT, cutting off our recent runs. This method applies
        filter via SQL WHERE before LIMIT to avoid that problem.

        Policy must match ``service.auth.principal_owns()`` (#505) exactly:

        - If both record and principal have ``owner_id``, compare those values
          (only if ``principal_owner_id`` is not None).
        - Otherwise (record lacks ``owner_id`` or principal lacks ``owner_id``),
          fall back to ``created_by == principal_label``.
        - Match neither: exclude (fail-closed). SQL NULL comparisons naturally fail,
          no special handling needed.

        Args:
            limit: Max builds to return.
            principal_owner_id: Requesting principal's canonical stable owner_id.
                If ``None`` (legacy/owner_id-unset principal), always fall back to
                ``created_by`` comparison regardless of record ``owner_id``
                (same as ``principal_owns()``).
            principal_label: Requesting principal's legacy comparison label
                (``Principal.label``).

        Returns:
            BuildEntry list (finished_at descending, max limit items).
        """
        if principal_owner_id is not None:
            sql = """
                SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                       dataset_id, owner_id
                FROM builds
                WHERE (owner_id IS NOT NULL AND owner_id = ?)
                   OR (owner_id IS NULL AND created_by = ?)
                ORDER BY finished_at DESC
                LIMIT ?
            """
            params: tuple[object, ...] = (principal_owner_id, principal_label, limit)
        else:
            sql = """
                SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                       dataset_id, owner_id
                FROM builds
                WHERE created_by = ?
                ORDER BY finished_at DESC
                LIMIT ?
            """
            params = (principal_label, limit)
        cur = self._conn.execute(sql, params)
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def list_between(self, start_iso: str, end_iso: str) -> list[BuildEntry]:
        """Return builds completed in [start_iso, end_iso) half-open range, ascending (#516).

        Include rows where ``finished_at`` falls in range by string comparison, plus rows
        with NULL ``finished_at`` — SQL can't determine if NULL is in range, so return it
        to caller without silent exclusion, allowing them to count as malformed (#516 partial).
        String ordering matches time ordering for ISO 8601 UTC format (``Z`` suffix,
        zero-padded). Extreme legacy values outside that format may be filtered by this
        query itself. In practice, malformed values are mostly corrupted ISO strings
        (including NULL) sharing same day prefix — this boundary is narrow. Uses
        ``idx_builds_finished_at`` index to avoid full table load.

        Args:
            start_iso: Range start (inclusive), ISO 8601 UTC string.
            end_iso: Range end (exclusive), ISO 8601 UTC string.

        Returns:
            BuildEntry list (finished_at ascending, NULL first).
        """
        cur = self._conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            WHERE finished_at IS NULL OR (finished_at >= ? AND finished_at < ?)
            ORDER BY finished_at ASC
            """,
            (start_iso, end_iso),
        )
        return [
            BuildEntry(
                run_id=row[0],
                status=cast(BuildStatus, row[1]),
                started_at=row[2],
                finished_at=row[3],
                spec_digest=row[4],
                error=row[5],
                created_by=row[6],
                dataset_id=row[7],
                owner_id=row[8],
            )
            for row in cur
        ]

    def latest_successful_finished_at(self) -> str | None:
        """Return finished_at of most recent successful (status='ok') build (#516).

        Used as Artifact Store ``last_write_at`` basis — return only definite proof
        that actual successful build wrote artifact; None if no success record
        (don't fill unknown with arbitrary value). Bounded query using
        ``idx_builds_finished_at`` index.

        Returns:
            ISO 8601 string or None if no success record.
        """
        cur = self._conn.execute(
            """
            SELECT finished_at FROM builds
            WHERE status = 'ok' AND finished_at IS NOT NULL
            ORDER BY finished_at DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        return cast(str | None, row[0]) if row is not None else None

    def count_builds(self, also: Collection[str] = ()) -> int:
        """How many distinct runs are indexed or named in ``also`` (#948).

        ``also`` is the run ids known elsewhere — the async registry's queued and running
        jobs, which the index does not hold until a manifest exists. An id the index
        holds is counted once. One ``COUNT(*)`` plus a primary-key lookup per chunk of
        ``also``; no row is read.
        """
        (indexed,) = self._conn.execute("SELECT COUNT(*) FROM builds").fetchone()
        pending = set(also)
        ids = sorted(pending)
        for start in range(0, len(ids), _IN_CHUNK):
            chunk = ids[start : start + _IN_CHUNK]
            marks = ",".join("?" * len(chunk))
            cur = self._conn.execute(f"SELECT run_id FROM builds WHERE run_id IN ({marks})", chunk)
            pending.difference_update(row[0] for row in cur)
        return int(indexed) + len(pending)

    def get(self, run_id: str) -> BuildEntry | None:
        """Query specific build.

        Args:
            run_id: Build execution identifier

        Returns:
            BuildEntry or None (not found)
        """
        cur = self._conn.execute(
            """
            SELECT run_id, status, started_at, finished_at, spec_digest, error, created_by,
                   dataset_id, owner_id
            FROM builds
            WHERE run_id = ?
            """,
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return BuildEntry(
            run_id=row[0],
            status=cast(BuildStatus, row[1]),
            started_at=row[2],
            finished_at=row[3],
            spec_digest=row[4],
            error=row[5],
            created_by=row[6],
            dataset_id=row[7],
            owner_id=row[8],
        )

    def delete(self, run_id: str) -> None:
        """Delete build entry.

        Args:
            run_id: Build execution identifier
        """
        try:
            with self._transaction():
                self._conn.execute("DELETE FROM builds WHERE run_id = ?", (run_id,))
        except Exception:
            # Ignore index failure
            pass

    def replace_contents(self, entries: Collection[BuildEntry]) -> None:
        """Make the index what ``entries`` say, in one transaction, in this file.

        For a rebuild beside a running server (#1157): its connections stay on the
        same file and see the result. Rows are written over, and a row ``entries`` does
        not name is removed only when its run has no manifest — a build that finished
        while the manifests were being scanned is in the index and not in the scan, and
        is kept.

        Unlike the other writes this one does not swallow a failure: a rebuild is asked
        for, and one that failed leaves the index as it was.
        """
        scanned = {entry.run_id for entry in entries}
        conn = self._conn
        # The write lock from the start, so nothing is added between reading which
        # rows are stale and removing them.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """
                INSERT OR REPLACE INTO builds
                (run_id, status, started_at, finished_at, spec_digest, error, created_by,
                 dataset_id, owner_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        entry.run_id,
                        entry.status,
                        entry.started_at,
                        entry.finished_at,
                        entry.spec_digest,
                        entry.error,
                        entry.created_by,
                        entry.dataset_id,
                        entry.owner_id,
                    )
                    for entry in entries
                ],
            )
            indexed = [str(row[0]) for row in conn.execute("SELECT run_id FROM builds")]
            stale = [
                (run_id,)
                for run_id in indexed
                if run_id not in scanned
                and not (self._output_root / run_id / "manifest.json").is_file()
            ]
            conn.executemany("DELETE FROM builds WHERE run_id = ?", stale)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def close(self, *, checkpoint: bool = True) -> None:
        """Close connection.

        Checkpoint WAL contents to main DB file before close, so file can be
        safely transferred by rename only.

        Args:
            checkpoint: False skips that. ``TRUNCATE`` waits for every reader, up to
                the busy timeout, so a caller beside a running server that is not
                about to move the file leaves the WAL to the server (#1157).
        """
        if hasattr(self._local, "conn"):
            if checkpoint:
                self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._local.conn.close()
            delattr(self._local, "conn")


def _iter_manifest_entries(output_root: Path) -> Iterator[BuildEntry]:
    """Scan manifest.json files under output_root, yield ``BuildEntry``.

    Compute derived index values from canonical (manifest.json + BuildSpec snapshot).
    Skip corrupted/missing runs — index is derivative so missing runs don't affect
    canonical. Shared rebuild source across backends (sqlite/cubrid).
    """
    import json

    import yaml

    from ..manifest import run_status_from_manifest
    from ..spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME, compute_spec_digest

    if not output_root.exists():
        return

    for run_dir in output_root.iterdir():
        if not run_dir.is_dir():
            continue
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.exists():
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue

        # Since manifest.json is canonical, derived rules owned by manifest package (#481) —
        # cancelled run may have empty errors, so existing "errors presence" derivation alone
        # incorrectly promotes to success (ok) when rebuilding.
        # The run's outcome, a failed table commit included (#1106).
        status = cast(BuildStatus, run_status_from_manifest(manifest))
        snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
        spec_digest: str | None = None
        dataset_id: str | None = None
        # is_file() follows symlinks, so explicitly reject symlinks to prevent
        # hashing files outside workspace.
        if snapshot_path.is_file() and not snapshot_path.is_symlink():
            try:
                snapshot_bytes: bytes | None = snapshot_path.read_bytes()
            except OSError:
                snapshot_bytes = None
            if snapshot_bytes is not None:
                spec_digest = compute_spec_digest(snapshot_bytes)
                # dataset_id is only derived lookup value (#488).
                # If can't read or parse snapshot YAML, don't guess,
                # leave as None — index corruption/loss doesn't change
                # canonical (BuildSpec snapshot).
                try:
                    snapshot_doc = yaml.safe_load(snapshot_bytes.decode("utf-8"))
                except (UnicodeDecodeError, yaml.YAMLError):
                    snapshot_doc = None
                if isinstance(snapshot_doc, dict):
                    raw_dataset_id = snapshot_doc.get("dataset_id")
                    if isinstance(raw_dataset_id, str) and raw_dataset_id:
                        dataset_id = raw_dataset_id

        yield BuildEntry(
            run_id=run_dir.name,
            status=status,
            started_at=manifest.get("started_at"),
            finished_at=manifest.get("finished_at"),
            spec_digest=spec_digest,
            error=None,
            created_by=manifest.get("created_by"),
            dataset_id=dataset_id,
            owner_id=manifest.get("owner_id"),
        )


#: The columns of ``builds`` as this release creates it.
_BUILDS_COLUMNS = frozenset(
    {
        "run_id",
        "status",
        "started_at",
        "finished_at",
        "spec_digest",
        "error",
        "created_by",
        "dataset_id",
        "owner_id",
    }
)


def _has_this_releases_table(index_path: Path) -> bool:
    """Whether ``builds`` is there with the columns this release writes.

    Read on a read-only connection, as the version is. False when the file is not a
    usable database, or is not there.

    Raises:
        sqlite3.Error: The table could not be looked at for another reason — the index
            is locked, cannot be opened, the disk failed. That says nothing about the
            table, and answering False would have the caller replace the file of a
            server that is only busy (#1157).
    """
    if not index_path.is_file():
        return False
    try:
        with closing(sqlite3.connect(f"{index_path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(builds)")}
    except sqlite3.Error as exc:
        if says_damaged(exc):
            return False
        raise
    return columns == _BUILDS_COLUMNS


def _rebuild_sqlite(output_root: Path) -> int:
    """Build SQLite index fresh to .tmp, then atomically replace (#366).

    Existing index survives if scan fails. Atomic rename works only for single-file
    SQLite, so separate from cubrid path. An index of another version is never opened,
    so this also replaces one a newer release wrote — the remedy its refusal names
    (#1096). This release's own index is refilled in place instead (#1157).
    """
    if not output_root.exists():
        return 0

    index_path = output_root / _INDEX_FILENAME
    if stored_version(index_path) == SCHEMA_VERSION and _has_this_releases_table(index_path):
        # This release's index, which a running server may have open (#1157). Renaming
        # a new file into its place leaves the server's connections on the file that
        # was there: each thread that had one keeps writing builds to a file nobody
        # else reads, and the lists differ by which thread answers. So it is filled
        # again where it is. An index of another version is not open in a server of
        # this release — that server refuses a newer one and remakes an older one when
        # it starts — and is replaced by the file below.
        #
        # A failure here is raised, a lock that was not released in time included:
        # replacing the file because the server was busy writing to it is the split
        # this avoids.
        entries = list(_iter_manifest_entries(output_root))
        index = SqliteBuildIndex(output_root)
        try:
            index.replace_contents(entries)
        finally:
            # No checkpoint: ``TRUNCATE`` waits out the whole busy timeout for a
            # server connection that is reading, and nothing here moves the file.
            index.close(checkpoint=False)
        return len(entries)

    # Not this release's index, or its version says so and its table does not — missing,
    # or not the columns this release writes. That last one is the file a rebuild is
    # run to recover, and it is replaced as the others are. A server that has it open
    # stays on the old file until it is restarted.
    tmp_path = output_root / f"{_INDEX_FILENAME}.tmp"
    backup_path = output_root / f"{_INDEX_FILENAME}.bak"

    # Clean up temp files left from interrupted previous run
    tmp_path.unlink(missing_ok=True)

    index = SqliteBuildIndex(output_root, index_path=tmp_path)
    try:
        count = 0
        for entry in _iter_manifest_entries(output_root):
            index.insert_or_replace(
                run_id=entry.run_id,
                status=entry.status,
                started_at=entry.started_at,
                finished_at=entry.finished_at,
                spec_digest=entry.spec_digest,
                created_by=entry.created_by,
                dataset_id=entry.dataset_id,
                owner_id=entry.owner_id,
            )
            count += 1
    finally:
        index.close()

    # Atomic replace: backup existing index to .bak, rename .tmp to original.
    #
    # The old index's ``-wal`` and ``-shm`` go with it. A process that ended without
    # closing its connections — killed, out of memory, or a thread whose connection
    # nobody closed — leaves them behind, and SQLite applies a ``-wal`` it finds next
    # to a database to that database: the new index would be read as the old one,
    # or as a mix of the two, the next time it was opened (#1096).
    sidecars = ("-wal", "-shm")
    for suffix in sidecars:
        # What the scan's own connection left; ``close()`` checkpointed it.
        Path(f"{tmp_path}{suffix}").unlink(missing_ok=True)
        Path(f"{backup_path}{suffix}").unlink(missing_ok=True)
    backup_path.unlink(missing_ok=True)
    moved: list[str] = []
    if index_path.exists():
        index_path.rename(backup_path)
    for suffix in sidecars:
        sidecar = Path(f"{index_path}{suffix}")
        if sidecar.exists():
            sidecar.rename(Path(f"{backup_path}{suffix}"))
            moved.append(suffix)

    try:
        tmp_path.rename(index_path)
    except OSError:
        # Restore from backup if replace fails, with the files that belong to it.
        if backup_path.exists():
            backup_path.rename(index_path)
        for suffix in moved:
            Path(f"{backup_path}{suffix}").rename(Path(f"{index_path}{suffix}"))
        raise
    else:
        backup_path.unlink(missing_ok=True)
        for suffix in sidecars:
            Path(f"{backup_path}{suffix}").unlink(missing_ok=True)

    return count


def _rebuild_cubrid(output_root: Path) -> int:
    """Rebuild CUBRID index from FS manifest scan (truncate + reinsert)."""
    from .backend import get_engine
    from .build_index_cubrid import CubridBuildIndex

    # The one place a newer index is replaced: the operator asked for it (#1096).
    index = CubridBuildIndex(get_engine(), replace_newer=True)
    try:
        return index.rebuild(_iter_manifest_entries(output_root))
    finally:
        index.close()


def rebuild_index(output_root: Path) -> int:
    """Rebuild index from filesystem scan (backend-aware, ADR 0016).

    Scan manifest.json canonical, refill derived index. Per backend:
    - sqlite, an index of this release's version: refilled where it is, in one
      transaction, so a running server's connections see it (#1157). One that cannot
      be written that way is replaced as below.
    - sqlite, any other index or none: build to .tmp, atomically rename-replace (#366).
    - cubrid: truncate builds table, reinsert in single transaction.

    Args:
        output_root: Build output root directory

    Returns:
        Number of builds rebuilt
    """
    from .backend import storage_backend

    if storage_backend() == "cubrid":
        return _rebuild_cubrid(output_root)
    return _rebuild_sqlite(output_root)


def bring_index_up_to_date(output_root: Path) -> int | None:
    """Rebuild the index from the manifests when the one stored is not this release's.

    ``serve`` calls this before it builds the service (#1096). Opening an older index
    drops its table and makes it again, empty, and with no index at all an empty one is
    made; the server then answered as healthy with every earlier run missing from its
    lists until someone ran ``rebuild-index``. The manifests are canonical, so the
    index is filled from them before the first request.

    An index a newer release wrote is not touched here: opening it refuses.

    Returns:
        The number of runs indexed, or None when the stored index was already this
        release's and nothing was done.
    """
    from .backend import storage_backend

    if storage_backend() == "cubrid":
        from .backend import get_engine
        from .build_index_cubrid import stored_index_version

        found = stored_index_version(get_engine())
        if found is not None and found >= SCHEMA_VERSION:
            return None
        return _rebuild_cubrid(output_root)
    index_path = output_root / _INDEX_FILENAME
    found = stored_version(index_path)
    if found is not None and found >= SCHEMA_VERSION:
        return None
    if found is None and index_path.exists():
        # A file with no version in it: not ours to replace without being asked.
        return None
    return _rebuild_sqlite(output_root)


def make_build_index(output_root: Path) -> BuildIndex:
    """Create ``BuildIndex`` implementation for selected backend (ADR 0016).

    sqlite (default) → ``SqliteBuildIndex(output_root)``; cubrid → ``CubridBuildIndex``
    sharing global Engine. cubrid module (and sqlalchemy) imported only in cubrid branch.
    """
    from .backend import storage_backend

    if storage_backend() == "cubrid":
        from .backend import get_engine
        from .build_index_cubrid import CubridBuildIndex

        return CubridBuildIndex(get_engine())
    return SqliteBuildIndex(output_root)


__all__ = [
    "BuildEntry",
    "BuildIndex",
    "SCHEMA_VERSION",
    "SqliteBuildIndex",
    "bring_index_up_to_date",
    "make_build_index",
    "rebuild_index",
]

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
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol, cast

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
        self._init_db()

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
            cur = self._conn.execute("SELECT version FROM schema_version")
            row = cur.fetchone()
            current_version = row[0] if row else None

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

    def close(self) -> None:
        """Close connection.

        Checkpoint WAL contents to main DB file before close, so file can be
        safely transferred by rename only.
        """
        if hasattr(self._local, "conn"):
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

    from ..manifest import status_from_manifest
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
        status = cast(BuildStatus, status_from_manifest(manifest))
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


def _rebuild_sqlite(output_root: Path) -> int:
    """Build SQLite index fresh to .tmp, then atomically replace (#366).

    Existing index survives if scan fails. Atomic rename works only for single-file
    SQLite, so separate from cubrid path.
    """
    if not output_root.exists():
        return 0

    index_path = output_root / _INDEX_FILENAME
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

    # Atomic replace: backup existing index to .bak, rename .tmp to original
    backup_path.unlink(missing_ok=True)
    if index_path.exists():
        index_path.rename(backup_path)

    try:
        tmp_path.rename(index_path)
    except OSError:
        # Restore from backup if replace fails
        if backup_path.exists():
            backup_path.rename(index_path)
        raise
    else:
        backup_path.unlink(missing_ok=True)

    return count


def _rebuild_cubrid(output_root: Path) -> int:
    """Rebuild CUBRID index from FS manifest scan (truncate + reinsert)."""
    from .backend import get_engine
    from .build_index_cubrid import CubridBuildIndex

    index = CubridBuildIndex(get_engine())
    try:
        return index.rebuild(_iter_manifest_entries(output_root))
    finally:
        index.close()


def rebuild_index(output_root: Path) -> int:
    """Rebuild index from filesystem scan (backend-aware, ADR 0016).

    Scan manifest.json canonical, refill derived index. Per backend:
    - sqlite: build to .tmp, atomically rename-replace (#366).
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
    "make_build_index",
    "rebuild_index",
]

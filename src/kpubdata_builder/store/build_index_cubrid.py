"""CUBRID-based build index (ADR 0016).

Implement same ``BuildIndex`` Protocol as ``SqliteBuildIndex`` using SQLAlchemy Core
(not ORM — maintain existing raw-SQL style). Preserve manifest.json canonical principle
and SCHEMA_VERSION, and ADR 0003 rule 4: "index write failure must not cause build failure".

This module imported only in cubrid branch of ``make_build_index()`` — importing
``sqlalchemy`` so default (sqlite) path doesn't pull optional dependency.

Concurrency: Receive process-global single Engine (``backend.get_engine()``, connection
pool + pool_pre_ping), borrow short connection per operation via ``with engine.begin()``.
Don't reuse connection across threads (#334 async job ThreadPoolExecutor notes).

Upsert handled dialect-independently as delete+insert in single transaction within
``with engine.begin()`` — don't depend on CUBRID dialect MERGE/ON DUPLICATE support.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
)

from .build_index import _NEWER_INDEX_REMEDY, SCHEMA_VERSION, BuildEntry, BuildStatus
from .schema_version import UnsupportedSchemaVersionError

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Row

_SCHEMA_VERSION_TABLE = "build_schema_version"
# Run ids per `IN (...)` lookup in `count_builds`, as in SqliteBuildIndex.
_IN_CHUNK = 500


class CubridBuildIndex:
    """CUBRID-based build index (ADR 0016). Implements ``BuildIndex`` Protocol."""

    def __init__(self, engine: Engine, *, replace_newer: bool = False) -> None:
        """
        Args:
            engine: The shared SQLAlchemy engine.
            replace_newer: Recreate an index a newer release wrote instead of refusing
                it. Only ``rebuild_index`` passes this (#1096).
        """
        self._engine = engine
        self._replace_newer = replace_newer
        self._metadata = MetaData()
        # Derived index schema. Canonical is manifest.json — if schema version changes,
        # DROP and recreate (data can be restored via rebuild_index).
        self._builds = Table(
            "builds",
            self._metadata,
            Column("run_id", String(255), primary_key=True),
            Column("status", String(16), nullable=False),
            Column("started_at", String(40)),
            Column("finished_at", String(40)),
            Column("spec_digest", String(128)),
            Column("error", Text),
            Column("created_by", String(255)),
            Column("dataset_id", String(255)),
            Column("owner_id", String(255)),
        )
        self._schema_version = Table(
            _SCHEMA_VERSION_TABLE,
            self._metadata,
            Column("version", Integer, primary_key=True),
        )
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Check schema version, recreate builds table if mismatch.

        Index is derivative, so DROP + recreate is safe on schema change
        (rebuild from canonical manifest.json via rebuild_index possible). An index a
        newer release wrote is the exception: dropping it would empty that release's
        index without a word, so it is refused unless a rebuild asked for it (#1096).

        Raises:
            UnsupportedSchemaVersionError: The index is newer than this release.
        """
        with self._engine.begin() as conn:
            existing = set(inspect(conn).get_table_names())
            version: int | None = None
            if _SCHEMA_VERSION_TABLE in existing:
                row = conn.execute(select(func.max(self._schema_version.c.version))).first()
                version = int(row[0]) if row is not None and row[0] is not None else None
            if version == SCHEMA_VERSION:
                return
            if version is not None and version > SCHEMA_VERSION and not self._replace_newer:
                raise UnsupportedSchemaVersionError(
                    store="build index",
                    location=f"the CUBRID table {self._builds.name}",
                    found=version,
                    supported=SCHEMA_VERSION,
                    remedy=_NEWER_INDEX_REMEDY,
                )
            # Version mismatch (or first creation): recreate derived table.
            self._builds.drop(conn, checkfirst=True)
            self._schema_version.drop(conn, checkfirst=True)
            self._schema_version.create(conn, checkfirst=True)
            self._builds.create(conn, checkfirst=True)
            conn.execute(insert(self._schema_version).values(version=SCHEMA_VERSION))

    def _row_to_entry(self, row: Row[Any]) -> BuildEntry:
        m = row._mapping
        return BuildEntry(
            run_id=m["run_id"],
            status=cast(BuildStatus, m["status"]),
            started_at=m["started_at"],
            finished_at=m["finished_at"],
            spec_digest=m["spec_digest"],
            error=m["error"],
            created_by=m["created_by"],
            dataset_id=m["dataset_id"],
            owner_id=m["owner_id"],
        )

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
        values = {
            "run_id": run_id,
            "status": status,
            "started_at": started_at,
            "finished_at": finished_at,
            "spec_digest": spec_digest,
            "error": error,
            "created_by": created_by,
            "dataset_id": dataset_id,
            "owner_id": owner_id,
        }
        try:
            # delete+insert within single transaction — doesn't depend on dialect upsert.
            with self._engine.begin() as conn:
                conn.execute(delete(self._builds).where(self._builds.c.run_id == run_id))
                conn.execute(insert(self._builds).values(**values))
        except Exception:
            # ADR 0003 rule 4: index write failure must not cause build failure.
            pass

    def list_builds(self, limit: int | None = 50) -> list[BuildEntry]:
        stmt = select(self._builds).order_by(self._builds.c.finished_at.desc())
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def list_by_dataset(self, dataset_id: str, limit: int | None = None) -> list[BuildEntry]:
        stmt = (
            select(self._builds)
            .where(self._builds.c.dataset_id == dataset_id)
            .order_by(self._builds.c.finished_at.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def list_recent_owned(
        self, *, limit: int, principal_owner_id: str | None, principal_label: str
    ) -> list[BuildEntry]:
        # Apply ownership filter in WHERE before LIMIT (#527) — same policy as service.auth.
        # principal_owns(). NULL comparison naturally fail-closed.
        b = self._builds.c
        if principal_owner_id is not None:
            cond = or_(
                and_(b.owner_id.is_not(None), b.owner_id == principal_owner_id),
                and_(b.owner_id.is_(None), b.created_by == principal_label),
            )
        else:
            cond = b.created_by == principal_label
        stmt = select(self._builds).where(cond).order_by(b.finished_at.desc()).limit(limit)
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def list_between(self, start_iso: str, end_iso: str) -> list[BuildEntry]:
        # [start, end) range + finished_at NULL (can't determine → pass to caller, #516).
        b = self._builds.c
        stmt = (
            select(self._builds)
            .where(
                or_(
                    b.finished_at.is_(None),
                    and_(b.finished_at >= start_iso, b.finished_at < end_iso),
                )
            )
            .order_by(b.finished_at.asc())
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [self._row_to_entry(r) for r in rows]

    def latest_successful_finished_at(self) -> str | None:
        # finished_at of most recent successful build (#516). None if no success record.
        b = self._builds.c
        stmt = (
            select(b.finished_at)
            .where(and_(b.status == "ok", b.finished_at.is_not(None)))
            .order_by(b.finished_at.desc())
            .limit(1)
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return str(row[0]) if row is not None else None

    def count_builds(self, also: Collection[str] = ()) -> int:
        # Distinct runs indexed or named in `also` (#948); see SqliteBuildIndex.
        b = self._builds.c
        pending = set(also)
        ids = sorted(pending)
        with self._engine.connect() as conn:
            indexed = conn.execute(select(func.count()).select_from(self._builds)).scalar_one()
            for start in range(0, len(ids), _IN_CHUNK):
                chunk = ids[start : start + _IN_CHUNK]
                rows = conn.execute(select(b.run_id).where(b.run_id.in_(chunk))).all()
                pending.difference_update(str(row[0]) for row in rows)
        return int(indexed) + len(pending)

    def get(self, run_id: str) -> BuildEntry | None:
        stmt = select(self._builds).where(self._builds.c.run_id == run_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        return self._row_to_entry(row) if row is not None else None

    def delete(self, run_id: str) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(delete(self._builds).where(self._builds.c.run_id == run_id))
        except Exception:
            # Ignore index failure (ADR 0003 rule 4).
            pass

    def rebuild(self, entries: Iterable[BuildEntry]) -> int:
        """Truncate builds table, reinsert from scan entries (single transaction).

        Rebuild is explicit management operation, unlike ``insert_or_replace``, so
        don't swallow exceptions — failure rolls back entire transaction, preserving
        previous index.
        """
        count = 0
        with self._engine.begin() as conn:
            conn.execute(delete(self._builds))
            for entry in entries:
                conn.execute(
                    insert(self._builds).values(
                        run_id=entry.run_id,
                        status=entry.status,
                        started_at=entry.started_at,
                        finished_at=entry.finished_at,
                        spec_digest=entry.spec_digest,
                        error=entry.error,
                        created_by=entry.created_by,
                        dataset_id=entry.dataset_id,
                        owner_id=entry.owner_id,
                    )
                )
                count += 1
        return count

    def close(self) -> None:
        """no-op. Global Engine is shared resource, don't dispose here.

        (``backend.dispose_engine()`` cleans up on process exit.)
        """


__all__ = ["CubridBuildIndex"]

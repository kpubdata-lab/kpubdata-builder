"""Saved analyses: SQL stored with the snapshot it read (#783).

A saved analysis has to open on another device and, after its table is refreshed, run
again against the **same input**. So what is stored is the concrete snapshot id the
query read — never ``current``, which would silently mean different data tomorrow —
and that snapshot gets a ``saved_analysis`` hold, placed while the query's lease is
still held, so garbage collection cannot remove it in between. Deleting the analysis
releases the hold.

Only metadata of the result is kept (columns, row count, truncated, when it ran), not
the rows. Analyses live in the same workspace as the tables they read
(``ownership.warehouse_workspace``): another owner's analysis is absent, not forbidden.

**Which SQL it is (#875).** The same text can mean something else to another engine:
unnamed aggregates are named differently, nulls sort differently, functions and
operators differ. So an analysis records the SQL dialect and engine it was saved with —
``sql_dialect``, ``engine``, ``engine_version``, ``query_contract_version``. Analyses
saved before the DuckDB cutover (#874) carry ``sql_dialect: legacy-polars``; they are
never re-run on DuckDB as if nothing had changed: ``run`` refuses them
(``analysis_migration_required``) until the SQL is reviewed and saved as a new analysis.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from kpubdata_builder.service import ownership
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.service.warehouse_api import WarehouseApiService, parse_table_query
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS
from kpubdata_builder.store.schema_version import StoreSchema, add_missing_columns
from kpubdata_builder.warehouse import TableCatalog, WarehouseError

_MAX_NAME_LENGTH = 200


#: The dialect saved SQL is written in now, and the one analyses saved before the
#: DuckDB cutover (#874) are recorded with.
SQL_DIALECT = "duckdb"
LEGACY_SQL_DIALECT = "legacy-polars"

#: Columns added after the first schema, with what existing rows get (#875).
_ADDED_COLUMNS = (
    ("sql_dialect", f"TEXT NOT NULL DEFAULT '{LEGACY_SQL_DIALECT}'"),
    ("engine", "TEXT NOT NULL DEFAULT 'polars'"),
    ("engine_version", "TEXT"),
    ("query_contract_version", "TEXT"),
)
_COLUMNS = (
    "analysis_id",
    "workspace_id",
    "owner_id",
    "name",
    "sql",
    "row_limit",
    "table_name",
    "snapshot_id",
    "hold_id",
    "result_meta",
    "created_at",
    *(name for name, _ in _ADDED_COLUMNS),
)


def current_provenance() -> dict[str, str]:
    """The dialect and engine an analysis saved now is written for."""
    import duckdb

    from kpubdata_builder.service.app import API_CONTRACT_VERSION

    return {
        "sql_dialect": SQL_DIALECT,
        "engine": "duckdb",
        "engine_version": duckdb.__version__,
        "query_contract_version": API_CONTRACT_VERSION,
    }


@dataclass(frozen=True)
class SavedAnalysis:
    """One stored analysis."""

    analysis_id: str
    workspace_id: str
    owner_id: str | None
    name: str
    sql: str
    limit: int
    table: str
    snapshot_id: str
    hold_id: str
    result_meta: dict[str, JsonValue]
    created_at: str
    sql_dialect: str = SQL_DIALECT
    engine: str = "duckdb"
    engine_version: str | None = None
    query_contract_version: str | None = None

    @property
    def migration_required(self) -> bool:
        """Saved for another SQL dialect: it must be reviewed, not re-run (#875)."""
        return self.sql_dialect != SQL_DIALECT

    def body(self) -> dict[str, JsonValue]:
        return {
            "analysis_id": self.analysis_id,
            "name": self.name,
            "sql": self.sql,
            "limit": self.limit,
            "bindings": [{"table": self.table, "snapshot_id": self.snapshot_id}],
            "result_meta": self.result_meta,
            "created_at": self.created_at,
            "sql_dialect": self.sql_dialect,
            "engine": self.engine,
            "engine_version": self.engine_version,
            "query_contract_version": self.query_contract_version,
            "migration_required": self.migration_required,
        }


def _create_v1(conn: sqlite3.Connection) -> None:
    """Version 1: the table, its index, and the dialect columns (#875).

    A file from before the store recorded a version may predate those columns. It gains
    them here, and its rows are the legacy analyses the defaults describe. This runs
    under the write lock of the transaction that records the version, so two processes
    opening one old store add each column once.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS analyses ("
        " analysis_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_id TEXT,"
        " name TEXT NOT NULL, sql TEXT NOT NULL, row_limit INTEGER NOT NULL,"
        " table_name TEXT NOT NULL, snapshot_id TEXT NOT NULL, hold_id TEXT NOT NULL,"
        " result_meta TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_analyses_workspace"
        " ON analyses(workspace_id, created_at DESC)"
    )
    add_missing_columns(conn, "analyses", _ADDED_COLUMNS)


#: The store's schema, one step per version (#1096).
SCHEMA = StoreSchema(
    store="saved analysis store",
    migrations=(_create_v1,),
    remedy=(
        "Run the release that wrote it, or restore the output directory from a backup "
        "taken before the upgrade; the saved analyses cannot be rebuilt from anything "
        "else."
    ),
)


class AnalysisStore:
    """SQLite store of saved analyses, every read and delete scoped by workspace."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA.bring_up_to_date(path, lambda: sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """One connection per call: committed on success, rolled back on error, closed."""
        with closing(sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)) as conn, conn:
            yield conn

    def put(self, analysis: SavedAnalysis) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                f"INSERT INTO analyses ({', '.join(_COLUMNS)})"
                f" VALUES ({', '.join('?' for _ in _COLUMNS)})",
                (
                    analysis.analysis_id,
                    analysis.workspace_id,
                    analysis.owner_id,
                    analysis.name,
                    analysis.sql,
                    analysis.limit,
                    analysis.table,
                    analysis.snapshot_id,
                    analysis.hold_id,
                    json.dumps(analysis.result_meta, ensure_ascii=False),
                    analysis.created_at,
                    analysis.sql_dialect,
                    analysis.engine,
                    analysis.engine_version,
                    analysis.query_contract_version,
                ),
            )

    def get(self, workspace_id: str, analysis_id: str) -> SavedAnalysis | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {_SELECT} FROM analyses WHERE workspace_id = ? AND analysis_id = ?",
                (workspace_id, analysis_id),
            ).fetchone()
        return None if row is None else _row(row)

    def list(self, workspace_id: str) -> list[SavedAnalysis]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {_SELECT} FROM analyses WHERE workspace_id = ?"
                " ORDER BY created_at DESC, analysis_id DESC",
                (workspace_id,),
            ).fetchall()
        return [_row(row) for row in rows]

    def delete(self, workspace_id: str, analysis_id: str) -> SavedAnalysis | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                f"SELECT {_SELECT} FROM analyses WHERE workspace_id = ? AND analysis_id = ?",
                (workspace_id, analysis_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM analyses WHERE analysis_id = ?", (analysis_id,))
        return _row(row)


_SELECT = ", ".join(_COLUMNS)


def _row(row: tuple[object, ...]) -> SavedAnalysis:
    (
        analysis_id,
        workspace_id,
        owner_id,
        name,
        sql,
        limit,
        table,
        snapshot_id,
        hold_id,
        result_meta,
        created_at,
        sql_dialect,
        engine,
        engine_version,
        query_contract_version,
    ) = row
    return SavedAnalysis(
        analysis_id=cast(str, analysis_id),
        workspace_id=cast(str, workspace_id),
        owner_id=cast("str | None", owner_id),
        name=cast(str, name),
        sql=cast(str, sql),
        limit=cast(int, limit),
        table=cast(str, table),
        snapshot_id=cast(str, snapshot_id),
        hold_id=cast(str, hold_id),
        result_meta=cast(dict[str, JsonValue], json.loads(cast(str, result_meta))),
        created_at=cast(str, created_at),
        sql_dialect=cast(str, sql_dialect),
        engine=cast(str, engine),
        engine_version=cast("str | None", engine_version),
        query_contract_version=cast("str | None", query_contract_version),
    )


def _not_found(analysis_id: str) -> ServiceResponse:
    return ServiceResponse(
        404, {"error": f"no such analysis: {analysis_id}", "code": "analysis_not_found"}
    )


class AnalysesApiService:
    """Create, list, read, delete and re-run saved analyses."""

    def __init__(
        self,
        *,
        store: Callable[[], AnalysisStore],
        warehouse: WarehouseApiService,
        table_catalog: Callable[[], TableCatalog | None],
    ) -> None:
        self._store = store
        self._warehouse = warehouse
        self._table_catalog = table_catalog

    def create(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """Run the query once, hold the snapshot it read, and store the binding."""
        try:
            name = (body or {}).get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("name must be a non-empty string")
            if len(name) > _MAX_NAME_LENGTH:
                raise ValueError(f"name must be at most {_MAX_NAME_LENGTH} characters")
            table, snapshot, sql, limit = parse_table_query(body, extra=frozenset({"name"}))
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})

        analysis_id = f"ana_{secrets.token_hex(16)}"

        def hold(catalog: TableCatalog, snapshot_id: str) -> JsonValue:
            return catalog.place_hold(
                snapshot_id,
                kind="saved_analysis",
                reason=f"saved analysis {analysis_id} ({name})",
            ).hold_id

        response = self._warehouse.run(
            table, snapshot, sql, limit=limit, principal=principal, while_pinned=hold
        )
        if response.status_code != 200:
            return response
        ran = response.body
        result = cast(dict[str, JsonValue], ran["result"])
        pinned = cast(dict[str, JsonValue], ran["snapshot"])
        analysis = SavedAnalysis(
            analysis_id=analysis_id,
            workspace_id=ownership.warehouse_workspace(principal.owner_id),
            owner_id=principal.owner_id,
            name=name,
            sql=sql,
            limit=limit,
            table=cast(str, pinned["logical_name"]),
            snapshot_id=cast(str, pinned["snapshot_id"]),
            hold_id=cast(str, ran["pinned"]),
            result_meta={
                "columns": result["columns"],
                "column_meta": result["column_meta"],
                "row_count": len(cast(list[JsonValue], result["rows"])),
                "truncated": result["truncated"],
                "executed_at": datetime.now(timezone.utc).isoformat(),
            },
            created_at=datetime.now(timezone.utc).isoformat(),
            **current_provenance(),
        )
        try:
            self._store().put(analysis)
        except sqlite3.Error:
            self._release(analysis.hold_id)
            raise
        return ServiceResponse(200, {"analysis": analysis.body(), "result": result})

    def list(self, *, principal: Principal) -> ServiceResponse:
        workspace = ownership.warehouse_workspace(principal.owner_id)
        return ServiceResponse(200, {"analyses": [a.body() for a in self._store().list(workspace)]})

    def get(self, analysis_id: str, *, principal: Principal) -> ServiceResponse:
        workspace = ownership.warehouse_workspace(principal.owner_id)
        analysis = self._store().get(workspace, analysis_id)
        if analysis is None:
            return _not_found(analysis_id)
        return ServiceResponse(200, analysis.body())

    def delete(self, analysis_id: str, *, principal: Principal) -> ServiceResponse:
        """Delete the analysis, then release its hold so the snapshot can be reclaimed."""
        workspace = ownership.warehouse_workspace(principal.owner_id)
        analysis = self._store().delete(workspace, analysis_id)
        if analysis is None:
            return _not_found(analysis_id)
        self._release(analysis.hold_id)
        return ServiceResponse(200, {"analysis_id": analysis_id, "deleted": True})

    def run(self, analysis_id: str, *, principal: Principal) -> ServiceResponse:
        """Run the stored SQL against the stored snapshot — not today's current one —
        in the dialect it was saved with; an analysis saved for another refuses (#875)."""
        workspace = ownership.warehouse_workspace(principal.owner_id)
        analysis = self._store().get(workspace, analysis_id)
        if analysis is None:
            return _not_found(analysis_id)
        if analysis.migration_required:
            # Never silently: the same text may mean something else on this engine.
            return ServiceResponse(
                409,
                {
                    "error": f"analysis {analysis_id} was saved for {analysis.sql_dialect} "
                    f"SQL and is not re-run as {SQL_DIALECT} SQL; review the SQL and save "
                    "it as a new analysis",
                    "code": "analysis_migration_required",
                    "sql_dialect": analysis.sql_dialect,
                },
            )
        return self._warehouse.run(
            analysis.table,
            analysis.snapshot_id,
            analysis.sql,
            limit=analysis.limit,
            principal=principal,
        )

    def _release(self, hold_id: str) -> None:
        catalog = self._table_catalog()
        if catalog is None:
            return
        try:
            catalog.release_hold(hold_id)
        except (WarehouseError, sqlite3.Error):
            # The analysis is gone either way; a hold left behind names the analysis
            # in its reason, so an operator can find and release it.
            return


__all__ = [
    "LEGACY_SQL_DIALECT",
    "SQL_DIALECT",
    "AnalysesApiService",
    "AnalysisStore",
    "SavedAnalysis",
    "current_provenance",
]

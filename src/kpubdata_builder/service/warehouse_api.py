"""Read committed warehouse tables through the API (#797).

Builds committed snapshots (#703) but nothing read them: ``resolve_current`` and
``pin`` had no caller outside ``warehouse/``, and ``POST /query`` still read a run's
stage files. This module is the reading side.

- ``GET /warehouse/tables`` and ``GET /warehouse/tables/{name}`` list the caller's
  tables and a table's snapshots.
- ``POST /warehouse/query`` runs read-only SQL against one table. ``current`` is
  resolved to a snapshot id **once**, under a lease, before the query starts, so a
  refresh committed while it runs does not change what it reads and garbage collection
  leaves the snapshot alone. The response names the snapshot that was read.

A caller only ever sees the workspace its own builds commit into
(``ownership.warehouse_workspace``), so another owner's table is not forbidden but
absent: the same 404 as a name that was never built.

``POST /query`` keeps its request shape. Multi-table SQL over pinned snapshots (#704)
extends this endpoint rather than that one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import ownership
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.query_service_api import execute_query
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages._path_safety import ensure_within
from kpubdata_builder.warehouse import (
    PinnedSnapshot,
    SnapshotLayout,
    SnapshotNotFound,
    SnapshotRow,
    SnapshotStateError,
    TableCatalog,
    TableNotFound,
    TableRow,
    WarehouseError,
)

_ALLOWED_FIELDS = {"table", "snapshot", "sql", "limit"}
#: The file a snapshot's Gold package holds its table in.
_TABLE_FILE = "table.parquet"
#: Snapshot states a reader may be pointed at.
_READABLE = ("committed", "quarantined")


def _not_configured() -> ServiceResponse:
    return ServiceResponse(
        404,
        {"error": "this deployment has no warehouse", "code": "warehouse_not_configured"},
    )


def _table_not_found(name: str) -> ServiceResponse:
    return ServiceResponse(404, {"error": f"no such table: {name}", "code": "table_not_found"})


def _table_body(table: TableRow) -> dict[str, JsonValue]:
    return {
        "table_id": table.id,
        "logical_name": table.logical_name,
        "current_snapshot_id": table.current_snapshot_id,
        "revision": table.revision,
    }


def _snapshot_body(snapshot: SnapshotRow) -> dict[str, JsonValue]:
    return {
        "snapshot_id": snapshot.id,
        "run_id": snapshot.run_id,
        "state": snapshot.state,
        "row_count": snapshot.row_count,
        "created_at": snapshot.created_at,
        "committed_at": snapshot.committed_at,
    }


class WarehouseApiService:
    """List and query the caller's committed tables."""

    def __init__(
        self, *, table_catalog: Callable[[], TableCatalog | None], engine: QueryService
    ) -> None:
        self._table_catalog = table_catalog
        self._engine = engine

    @staticmethod
    def _find(catalog: TableCatalog, name: str, principal: Principal) -> TableRow | None:
        workspace = ownership.warehouse_workspace(principal.owner_id)
        return next((t for t in catalog.list_tables(workspace) if t.logical_name == name), None)

    def list_tables(self, *, principal: Principal) -> ServiceResponse:
        catalog = self._table_catalog()
        if catalog is None:
            return _not_configured()
        workspace = ownership.warehouse_workspace(principal.owner_id)
        return ServiceResponse(
            200, {"tables": [_table_body(t) for t in catalog.list_tables(workspace)]}
        )

    def get_table(self, name: str, *, principal: Principal) -> ServiceResponse:
        catalog = self._table_catalog()
        if catalog is None:
            return _not_configured()
        table = self._find(catalog, name, principal)
        if table is None:
            return _table_not_found(name)
        snapshots = [s for s in catalog.list_snapshots(table.id) if s.state in _READABLE]
        body = _table_body(table)
        body["snapshots"] = [_snapshot_body(s) for s in snapshots]
        return ServiceResponse(200, body)

    def query(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        try:
            name, snapshot, sql, limit = parse_table_query(body)
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})
        return self.run(name, snapshot, sql, limit=limit, principal=principal)

    def run(
        self,
        name: str,
        snapshot: str,
        sql: str,
        *,
        limit: int,
        principal: Principal,
        while_pinned: Callable[[TableCatalog, str], JsonValue] | None = None,
    ) -> ServiceResponse:
        """Pin the snapshot, run ``sql`` against it, release the lease.

        ``while_pinned`` runs after a successful query and before the lease is released,
        with the catalog and the snapshot id; its return value is added to the body as
        ``pinned``. A saved analysis places its hold there (#783), so garbage collection
        has no window between the read and the hold. A ``WarehouseError`` it raises
        answers 409.
        """
        catalog = self._table_catalog()
        if catalog is None:
            return _not_configured()
        table = self._find(catalog, name, principal)
        if table is None:
            return _table_not_found(name)

        try:
            pin = _pin(catalog, table, snapshot)
        except (TableNotFound, SnapshotNotFound) as exc:
            return ServiceResponse(404, {"error": str(exc), "code": "snapshot_not_found"})
        except SnapshotStateError as exc:
            return ServiceResponse(409, {"error": str(exc), "code": "snapshot_unavailable"})
        pinned: JsonValue = None
        try:
            snapshot_dir = SnapshotLayout(catalog.root, table.id).snapshot_dir(pin.snapshot_id)
            table_path = snapshot_dir / _TABLE_FILE
            try:
                ensure_within(snapshot_dir, table_path, label="warehouse table")
                readable = not table_path.is_symlink() and table_path.is_file()
            except ValueError:
                readable = False
            if not readable:
                return ServiceResponse(
                    404,
                    {
                        "error": "the snapshot holds no queryable table",
                        "code": "artifact_unavailable",
                    },
                )
            response = execute_query(self._engine, table_path, sql, limit=limit)
            if response.status_code != 200:
                return response
            if while_pinned is not None:
                try:
                    pinned = while_pinned(catalog, pin.snapshot_id)
                except WarehouseError as exc:
                    return ServiceResponse(409, {"error": str(exc), "code": "snapshot_unavailable"})
        finally:
            catalog.release(pin.lease_id)
        body: dict[str, JsonValue] = {
            "snapshot": {
                "table_id": table.id,
                "logical_name": table.logical_name,
                "snapshot_id": pin.snapshot_id,
                "revision": pin.revision,
            },
            "result": response.body,
        }
        if while_pinned is not None:
            body["pinned"] = pinned
        return ServiceResponse(200, body)


def _pin(catalog: TableCatalog, table: TableRow, snapshot: str) -> PinnedSnapshot:
    """Lease the snapshot to read: the current one, or a named one of this table."""
    if snapshot == "current":
        return catalog.resolve_current(table.id)
    # A snapshot id is only honoured for the table named with it; otherwise a caller
    # could read any snapshot by guessing its id under a table of their own.
    if snapshot not in catalog.known_snapshot_ids(table.id):
        raise SnapshotNotFound(f"table {table.logical_name!r} has no snapshot {snapshot!r}")
    return catalog.pin(snapshot)


def parse_table_query(
    body: Mapping[str, JsonValue] | None, *, extra: frozenset[str] = frozenset()
) -> tuple[str, str, str, int]:
    """Validate a table query body, rejecting unknown fields other than ``extra``."""
    if body is None:
        raise ValueError("request body is required")
    if not set(body).issubset(_ALLOWED_FIELDS | extra):
        raise ValueError("request contains unknown fields")
    table = body.get("table")
    snapshot = body.get("snapshot", "current")
    sql = body.get("sql")
    limit = body.get("limit", 100)
    if not isinstance(table, str) or not table:
        raise ValueError("table must be a non-empty string")
    if not isinstance(snapshot, str) or not snapshot:
        raise ValueError("snapshot must be 'current' or a snapshot id")
    if not isinstance(sql, str) or not sql:
        raise ValueError("sql must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    return table, snapshot, sql, limit


__all__ = ["WarehouseApiService", "parse_table_query"]

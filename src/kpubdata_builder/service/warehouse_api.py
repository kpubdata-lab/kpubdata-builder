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

``POST /warehouse/rows`` reads one page of a snapshot for a table screen (#815): the
snapshot is pinned the same way, and ``query.rows`` gives the page a stable order and a
count that says whether it was computed.

``POST /warehouse/aggregate`` runs a validated aggregate over a pinned snapshot (#818):
named functions over allowed columns, every row aggregated before the top N is taken,
and a unit column checked so values counted in different units are never added up.

``POST /query`` keeps its request shape. Multi-table SQL over pinned snapshots (#704)
extends this endpoint rather than that one.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from kpubdata_builder.query.aggregate import (
    AggregatePlan,
    Measure,
    check_aggregate_plan,
    parse_aggregate_plan,
)
from kpubdata_builder.query.engine import QueryExecutionError, QueryTimeoutError
from kpubdata_builder.query.rows import check_plan, parse_rows_plan
from kpubdata_builder.query.service import QueryBusyError, QueryService
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
_ROWS_FIELDS = {"table", "snapshot", "offset", "page_size", "columns", "sort", "filters", "count"}
_AGGREGATE_FIELDS = {
    "table",
    "snapshot",
    "group_by",
    "measures",
    "filters",
    "order_by",
    "limit",
    "unit_column",
    "unit_policy",
}
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
        "coverage": _coverage(snapshot.coverage),
    }


def _coverage(raw: str | None) -> JsonValue:
    """The snapshot's recorded fetch coverage (#816), or None when none was recorded.

    None means unknown: a snapshot committed before coverage was recorded, or one whose
    record cannot be read, is never reported as complete.
    """
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return cast(JsonValue, value) if isinstance(value, dict) else None


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

    def rows(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """One page of a pinned snapshot, in a stable order, with a count and its status.

        ``snapshot`` is ``current`` on the first page and the returned ``snapshot_id`` on
        the next ones, so every page reads the same snapshot however many refreshes are
        committed in between.
        """
        try:
            if body is None:
                raise ValueError("request body is required")
            if not set(body).issubset(_ROWS_FIELDS):
                raise ValueError("request contains unknown fields")
            name = body.get("table")
            snapshot = body.get("snapshot", "current")
            if not isinstance(name, str) or not name:
                raise ValueError("table must be a non-empty string")
            if not isinstance(snapshot, str) or not snapshot:
                raise ValueError("snapshot must be 'current' or a snapshot id")
            plan = parse_rows_plan(body)
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})

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
        try:
            table_path = _readable_table(catalog, table, pin.snapshot_id)
            if table_path is None:
                return _no_table_file()
            import polars as pl

            try:
                check_plan(plan, pl.read_parquet_schema(table_path))
            except ValueError as exc:
                return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})
            try:
                result = self._engine.execute_rows(table_path, plan.to_json(), limit=plan.page_size)
            except QueryBusyError:
                return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})
            except QueryTimeoutError:
                return ServiceResponse(504, {"error": "read timed out", "code": "query_timeout"})
            except QueryExecutionError:
                return ServiceResponse(
                    400, {"error": "read failed", "code": "query_execution_failed"}
                )
            row_count = catalog.get_snapshot(pin.snapshot_id).row_count
        finally:
            catalog.release(pin.lease_id)

        count = result.meta.get("count")
        has_more = result.meta.get("has_more") is True
        if not isinstance(count, int) and not plan.filters and row_count is not None:
            # With no filter the snapshot's own row count is the answer.
            count = row_count
        returned = len(result.rows)
        body_out: dict[str, JsonValue] = {
            "snapshot": {
                "table_id": table.id,
                "logical_name": table.logical_name,
                "snapshot_id": pin.snapshot_id,
                "revision": pin.revision,
            },
            "columns": list(result.columns),
            "column_meta": list(result.column_meta),
            "rows": list(result.rows),
            # The requested keys. Ties are always broken by the row's position in the
            # snapshot, which is not a column and is not sent.
            "order": [
                {"column": k.column, "direction": "desc" if k.descending else "asc"}
                for k in plan.sort
            ],
            "page": {
                "offset": plan.offset,
                "page_size": plan.page_size,
                "returned": returned,
                "has_more": has_more,
                "next_offset": plan.offset + returned if has_more else None,
            },
            "count": (
                {"status": "exact", "value": count}
                if isinstance(count, int)
                else {"status": "not_computed", "value": None}
            ),
            "execution_ms": result.execution_ms,
            "startup_ms": result.startup_ms,
            "engine_execution_ms": result.engine_execution_ms,
        }
        return ServiceResponse(200, body_out)

    def aggregate(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """A validated aggregate over a pinned snapshot (#818).

        Every row that passes the filters is aggregated, then the groups are sorted and
        the top ``limit`` returned; ``result`` says how many groups there were, so a
        client can tell a full result from a top-N one. Aggregates are never computed
        over a sample.
        """
        try:
            if body is None:
                raise ValueError("request body is required")
            if not set(body).issubset(_AGGREGATE_FIELDS):
                raise ValueError("request contains unknown fields")
            name = body.get("table")
            snapshot = body.get("snapshot", "current")
            if not isinstance(name, str) or not name:
                raise ValueError("table must be a non-empty string")
            if not isinstance(snapshot, str) or not snapshot:
                raise ValueError("snapshot must be 'current' or a snapshot id")
            plan = parse_aggregate_plan(body)
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})

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
        try:
            table_path = _readable_table(catalog, table, pin.snapshot_id)
            if table_path is None:
                return _no_table_file()
            import polars as pl

            try:
                check_aggregate_plan(plan, pl.read_parquet_schema(table_path))
            except ValueError as exc:
                return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})
            try:
                result = self._engine.execute_aggregate(
                    table_path, plan.to_json(), limit=plan.limit
                )
            except QueryBusyError:
                return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})
            except QueryTimeoutError:
                return ServiceResponse(
                    504, {"error": "aggregate timed out", "code": "query_timeout"}
                )
            except QueryExecutionError:
                return ServiceResponse(
                    400, {"error": "aggregate failed", "code": "query_execution_failed"}
                )
        finally:
            catalog.release(pin.lease_id)

        snapshot_body: dict[str, JsonValue] = {
            "table_id": table.id,
            "logical_name": table.logical_name,
            "snapshot_id": pin.snapshot_id,
            "revision": pin.revision,
        }
        refusal = result.meta.get("refusal")
        if isinstance(refusal, dict):
            # Refused rather than cut short: a partial aggregate would look complete.
            return ServiceResponse(422, {**refusal, "snapshot": snapshot_body})
        group_count = result.meta.get("group_count")
        input_rows = result.meta.get("input_row_count")
        if not isinstance(group_count, int) or not isinstance(input_rows, int):
            return ServiceResponse(
                400, {"error": "aggregate failed", "code": "query_execution_failed"}
            )
        returned = len(result.rows)
        return ServiceResponse(
            200,
            {
                "snapshot": snapshot_body,
                "columns": list(result.columns),
                "column_meta": list(result.column_meta),
                "rows": list(result.rows),
                "group_by": list(plan.group_by),
                "measures": [_measure_body(m, plan) for m in plan.measures],
                "order": [
                    {"key": k.key, "direction": "desc" if k.descending else "asc"}
                    for k in plan.order
                ],
                "unit": _unit_body(plan),
                "input": {"row_count": input_rows, "sampled": False},
                "result": {
                    "completeness": "full" if returned == group_count else "top_n",
                    "group_count": group_count,
                    "returned": returned,
                    "limit": plan.limit,
                },
                "execution_ms": result.execution_ms,
                "startup_ms": result.startup_ms,
                "engine_execution_ms": result.engine_execution_ms,
            },
        )

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
            table_path = _readable_table(catalog, table, pin.snapshot_id)
            if table_path is None:
                return _no_table_file()
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


def _measure_body(measure: Measure, plan: AggregatePlan) -> dict[str, JsonValue]:
    """A measure as applied. ``additive`` is the caller's assertion, echoed back."""
    unit_bound = measure.fn in ("sum", "avg", "min", "max") and plan.unit_column is not None
    return {
        "as": measure.alias,
        "fn": measure.fn,
        "column": measure.column,
        "additive": measure.additive if measure.fn == "sum" else None,
        # Which column the measure's unit is read from, when rows carry one.
        "unit_column": plan.unit_column if unit_bound else None,
    }


def _unit_body(plan: AggregatePlan) -> dict[str, JsonValue]:
    """How units were handled: not checked, one unit per group, or split by unit."""
    if plan.unit_column is None:
        check = "not_checked"
    elif plan.unit_column in plan.key_columns:
        check = "split"
    else:
        check = "single_unit"
    return {"column": plan.unit_column, "policy": plan.unit_policy, "check": check}


def _no_table_file() -> ServiceResponse:
    return ServiceResponse(
        404,
        {"error": "the snapshot holds no queryable table", "code": "artifact_unavailable"},
    )


def _readable_table(catalog: TableCatalog, table: TableRow, snapshot_id: str) -> Path | None:
    """The snapshot's table file, or None when it is missing, a symlink or outside it."""
    snapshot_dir = SnapshotLayout(catalog.root, table.id).snapshot_dir(snapshot_id)
    table_path = snapshot_dir / _TABLE_FILE
    try:
        ensure_within(snapshot_dir, table_path, label="warehouse table")
    except ValueError:
        return None
    if table_path.is_symlink() or not table_path.is_file():
        return None
    return table_path


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

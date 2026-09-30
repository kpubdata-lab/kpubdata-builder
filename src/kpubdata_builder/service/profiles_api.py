"""Column profiles of a committed snapshot (#817).

``GET /warehouse/tables/{name}/profile`` describes each column of one snapshot — type,
nulls, NaN and infinite counts, value range — computed by ``query.profile`` under the
same limits as a query. What is computed and what is withheld is documented there.

The profile never touches the snapshot. It is a separate file under the table's
``_profiles/`` directory, keyed by snapshot id and checked against the snapshot's
content digest and the algorithm version before it is reused; garbage collection
removes it with the snapshot. The sensitivity decision reads the BuildSpec of the run
that produced the snapshot, so it is fixed for a snapshot as its bytes are.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from kpubdata_builder.query.engine import QueryExecutionError, QueryTimeoutError
from kpubdata_builder.query.profile import PROFILE_ALGORITHM_VERSION, ProfilePlan
from kpubdata_builder.query.service import QueryBusyError, QueryService
from kpubdata_builder.service import ownership
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.column_semantics import spec_semantics, table_key
from kpubdata_builder.service.datasets import read_snapshot_spec
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.service.warehouse_api import _pin, _readable_table
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.semantics import ColumnSemantics
from kpubdata_builder.tabular.wire import mark_identifiers
from kpubdata_builder.warehouse import (
    SnapshotLayout,
    SnapshotNotFound,
    SnapshotStateError,
    TableCatalog,
    TableNotFound,
)


def _error(status: int, code: str, message: str) -> ServiceResponse:
    return ServiceResponse(status, {"error": message, "code": code})


class ProfilesApiService:
    """Compute, cache and return a snapshot's column profile."""

    def __init__(
        self,
        *,
        output_root: Path,
        table_catalog: Callable[[], TableCatalog | None],
        engine: QueryService,
    ) -> None:
        self._output_root = output_root
        self._table_catalog = table_catalog
        self._engine = engine

    def get(self, name: str, snapshot: str, *, principal: Principal) -> ServiceResponse:
        catalog = self._table_catalog()
        if catalog is None:
            return _error(404, "warehouse_not_configured", "this deployment has no warehouse")
        workspace = ownership.warehouse_workspace(principal.owner_id)
        table = next((t for t in catalog.list_tables(workspace) if t.logical_name == name), None)
        if table is None:
            return _error(404, "table_not_found", f"no such table: {name}")
        try:
            pin = _pin(catalog, table, snapshot)
        except (TableNotFound, SnapshotNotFound) as exc:
            return _error(404, "snapshot_not_found", str(exc))
        except SnapshotStateError as exc:
            return _error(409, "snapshot_unavailable", str(exc))
        try:
            row = catalog.get_snapshot(pin.snapshot_id)
            spec = read_snapshot_spec(self._output_root, row.run_id)
            semantics = spec_semantics(spec, table_key(spec, table.logical_name))
            layout = SnapshotLayout(catalog.root, table.id)
            cache = layout.profile_path(pin.snapshot_id)
            profile = _cached(cache, pin.snapshot_id, row.artifact_digest)
            if profile is None:
                table_path = _readable_table(catalog, table, pin.snapshot_id)
                if table_path is None:
                    return _error(
                        404, "artifact_unavailable", "the snapshot holds no queryable table"
                    )
                policy = spec.pii if spec is not None else None
                plan = ProfilePlan(
                    allow_all_pii=policy is not None and policy.mode == "allow",
                    allow_columns=tuple(policy.allow_columns) if policy is not None else (),
                )
                try:
                    result = self._engine.execute_profile(table_path, plan.to_json())
                except QueryBusyError:
                    return _error(429, "query_busy", "query is busy")
                except QueryTimeoutError:
                    return _error(504, "query_timeout", "profiling timed out")
                except QueryExecutionError:
                    return _error(400, "query_execution_failed", "profiling failed")
                computed = result.meta.get("profile")
                if not isinstance(computed, dict):
                    return _error(400, "query_execution_failed", "profiling failed")
                profile = {
                    **computed,
                    "snapshot_id": pin.snapshot_id,
                    "artifact_digest": row.artifact_digest,
                    "computed_at": datetime.now(timezone.utc).isoformat(),
                }
                _store(cache, profile)
        finally:
            catalog.release(pin.lease_id)
        return ServiceResponse(
            200,
            {
                "snapshot": {
                    "table_id": table.id,
                    "logical_name": table.logical_name,
                    "snapshot_id": pin.snapshot_id,
                    "revision": pin.revision,
                },
                "profile": cast(JsonValue, _with_identifiers(profile, semantics)),
            },
        )


def _with_identifiers(
    profile: dict[str, JsonValue], semantics: dict[str, ColumnSemantics]
) -> dict[str, JsonValue]:
    """The profile with text code columns reported as `identifier` (#702).

    Applied to each answer, not to the cached file: the cache holds what the bytes are,
    and the declaration comes from the snapshot's BuildSpec and kpubdata.
    """
    columns = profile.get("columns")
    if not semantics or not isinstance(columns, list):
        return profile
    if not all(isinstance(column, dict) for column in columns):
        return profile
    entries = cast(list[dict[str, JsonValue]], columns)
    return {**profile, "columns": cast(JsonValue, mark_identifiers(entries, semantics))}


def _cached(path: Path, snapshot_id: str, digest: str) -> dict[str, JsonValue] | None:
    """The cached profile when it describes these bytes with this algorithm, else None."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("snapshot_id") != snapshot_id
        or data.get("artifact_digest") != digest
        or data.get("algorithm_version") != PROFILE_ALGORITHM_VERSION
    ):
        return None
    return cast(dict[str, JsonValue], data)


def _store(path: Path, profile: dict[str, JsonValue]) -> None:
    """Write atomically, so a reader never sees half a profile. Failure is not fatal."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        # The profile is still returned; it is computed again next time.
        return


__all__ = ["ProfilesApiService"]

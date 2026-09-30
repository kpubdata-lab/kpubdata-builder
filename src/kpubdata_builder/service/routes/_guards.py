"""Run route existence and ownership guard shared by all."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from ...stages._path_safety import ensure_within
from .. import ownership as ownership_module
from ..auth import Principal
from ..responses import ServiceResponse

if TYPE_CHECKING:
    from ..app import BuilderService


def _read_manifest_ownership(service: BuilderService, run_id: str) -> tuple[str | None, str | None]:
    manifest_path = service._output_root / run_id / "manifest.json"
    if not manifest_path.exists():
        return None, None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return cast(str | None, data.get("created_by")), cast(str | None, data.get("owner_id"))
    except Exception:
        return None, None


def not_owner(run_id: str) -> ServiceResponse:
    """The answer to a request for another owner's run (#796).

    In a multi-user deployment it is the same 404 a missing run gets, so a run id cannot
    be probed for existence; otherwise 403, as before.
    """
    if ownership_module.hides_foreign_runs():
        return ServiceResponse(404, {"error": f"run not found: {run_id}"})
    return ServiceResponse(403, {"error": "forbidden: not run owner"})


def check_ownership(
    service: BuilderService, run_id: str, principal: Principal
) -> ServiceResponse | None:
    created_by, owner_id = _read_manifest_ownership(service, run_id)
    if ownership_module.ownership_allows(
        created_by=created_by, owner_id=owner_id, principal=principal
    ):
        return None
    return not_owner(run_id)


def check_run_exists(service: BuilderService, run_id: str) -> ServiceResponse | None:
    run_dir = service._output_root / run_id
    ensure_within(service._output_root, run_dir, label="run directory")
    if run_dir.is_dir():
        return None
    return ServiceResponse(404, {"error": f"run not found: {run_id}"})


def check_active_run_access(
    service: BuilderService, run_id: str, principal: Principal
) -> ServiceResponse | None:
    """Existence and ownership decision through pre-manifest (queued/running) phase (#496
    follow-up).

    Started in ``/builds/{run_id}/events``, later ``GET /builds/{run_id}`` (#480) and
    ``POST /builds/{run_id}/cancel`` (#481) reuse the same rules — to avoid routes
    handling active jobs having different 404/403 semantics.

    ``check_run_exists``/``check_ownership`` assume run directory and manifest.json
    already exist — but async jobs record ``run_submitted`` in event store *before*
    worker enqueue (``BuilderService.submit_build`` ``on_accept`` hook), run directory
    is created by worker at ``BuildContext.create()``, and manifest only after run
    finishes. In between (queued/running), ``check_run_exists`` returns 404, and if
    ownership is on, ``check_ownership`` returns fail-closed 403 (due to missing manifest,
    both created_by/owner_id are None), making events polling impossible.

    Decision order (run directory existence is not used as transition criterion — only
    manifest):
        1. If manifest.json exists, use existing ``check_ownership`` path (manifest-based,
           prefers stable ``owner_id``) — completed runs are decided by this path only,
           even if registry has terminal entry.
        2. If no manifest and async job registry (``AsyncBuildExecutor``, in-process
           memory) has snapshot (active or terminal ended without manifest due to
           enqueue failure), decide ownership by that snapshot's stable ``owner_id``
           (#505 canonical identity — ``created_by``/``Principal.label`` are legacy
           fallbacks only; we pass them as-is to ``ownership_allows`` for priority).
        3. If neither exist, 404.

    Snapshot ``owner_id`` is the value ``BuilderService.submit_build`` preserved in
    registry — not exposed in wire response (``BuildJobSnapshot.to_body()`` never exports
    it). ``_run_build_job`` reuses this value in persisted manifest/BuildIndex (#505 SSOT)
    record, but still does not pass it to ``kind="file"`` source resolver (#498) — async
    file-backed source owner propagation limit remains.

    Other routes like ``/manifest``, ``/stages`` still handle only persisted runs; they
    do not use this function — keeping impact scope narrow to active-job-handling routes.
    """
    run_dir = service._output_root / run_id
    ensure_within(service._output_root, run_dir, label="run directory")
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        return check_ownership(service, run_id, principal)
    snapshot = service._async_builds.get(run_id)
    if snapshot is not None:
        if ownership_module.ownership_allows(
            created_by=snapshot.created_by, owner_id=snapshot.owner_id, principal=principal
        ):
            return None
        return not_owner(run_id)
    return ServiceResponse(404, {"error": f"run not found: {run_id}"})


def check_existing_run_access(
    service: BuilderService, run_id: str, principal: Principal
) -> ServiceResponse | None:
    """If the caller-specified run_id **already exists**, verify ownership (#635).

    Decision rules match ``check_active_run_access``; only how missing runs are
    handled differs. That is for query routes: if run is missing, 404. Here, missing
    run_id is normal — means starting a new build with that name.

    Without this gate, sync ``POST /build`` did not verify who owned the caller's
    run_id. Giving someone else's run_id would overwrite that run's output and return
    results in response. Async ``POST /builds`` blocks with 409 in the same situation.
    """
    run_dir = service._output_root / run_id
    ensure_within(service._output_root, run_dir, label="run directory")
    if (run_dir / "manifest.json").exists():
        created_by, owner_id = _read_manifest_ownership(service, run_id)
        if ownership_module.ownership_allows(
            created_by=created_by, owner_id=owner_id, principal=principal
        ):
            return None
        # Stays 403 in every deployment (#796 hides reads, not this): a build that
        # names a taken run id is refused whatever the answer, and a 404 to a write
        # would say the id is free when it is not.
        return ServiceResponse(403, {"error": "forbidden: not run owner"})
    snapshot = service._async_builds.get(run_id)
    if snapshot is None:
        # run_id does not exist yet — new build.
        return None
    if ownership_module.ownership_allows(
        created_by=snapshot.created_by, owner_id=snapshot.owner_id, principal=principal
    ):
        return None
    return ServiceResponse(403, {"error": "forbidden: not run owner"})

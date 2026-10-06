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
        3. If neither exist, the recorded submission (event store) decides, by the
           same ownership rule: this is a run a restart interrupted (#996).
        4. If none exist, 404.

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
    # A run a restart interrupted has no manifest and is gone from the registry; who
    # submitted it is in the event store (#996). Someone else still gets what a run
    # that does not exist gets.
    submission = service._event_store.submission(run_id)
    if submission is not None:
        if ownership_module.ownership_allows(
            created_by=submission.created_by, owner_id=submission.owner_id, principal=principal
        ):
            return None
        return not_owner(run_id)
    return ServiceResponse(404, {"error": f"run not found: {run_id}"})


def _run_id_taken(status: int, run_id: str, code: str, error: str) -> ServiceResponse:
    return ServiceResponse(status, {"error": error, "code": code, "run_id": run_id})


def check_existing_run_access(
    service: BuilderService,
    run_id: str,
    principal: Principal,
    *,
    used_status: int = 409,
    synchronous: bool = False,
) -> ServiceResponse | None:
    """If the caller-specified run_id **already exists**, verify ownership (#635).

    Decision rules match ``check_active_run_access`` — manifest, then the job
    registry, then the recorded submission — and only how missing runs are handled
    differs. That is for query routes: if run is missing, 404. Here, missing run_id is
    normal — means starting a new build with that name.

    Without this gate, sync ``POST /build`` did not verify who owned the caller's
    run_id. Giving someone else's run_id would overwrite that run's output and return
    results in response. Async ``POST /builds`` had only its 409 for a completed run:
    a job still in the registry was returned to whoever named it, so that route calls
    this too (#991).
    """
    run_dir = service._output_root / run_id
    ensure_within(service._output_root, run_dir, label="run directory")
    if (run_dir / "manifest.json").exists():
        created_by, owner_id = _read_manifest_ownership(service, run_id)
        if ownership_module.ownership_allows(
            created_by=created_by, owner_id=owner_id, principal=principal
        ):
            if synchronous:
                # A run id is one attempt on this route too (#1065): building under a
                # completed run's id would write over its output. ``POST /builds`` answers
                # the same from its submit step, where the build index is read.
                return _run_id_taken(
                    used_status,
                    run_id,
                    "run_id_completed",
                    "run_id already completed; build under a new run_id",
                )
            return None
        # Stays 403 in every deployment (#796 hides reads, not this): a build that
        # names a taken run id is refused whatever the answer, and a 404 to a write
        # would say the id is free when it is not.
        return ServiceResponse(403, {"error": "forbidden: not run owner"})
    snapshot = service._async_builds.get(run_id)
    if snapshot is not None:
        if ownership_module.ownership_allows(
            created_by=snapshot.created_by, owner_id=snapshot.owner_id, principal=principal
        ):
            if not synchronous:
                # ``POST /builds`` hands the caller their own job back (#991).
                return None
            # The synchronous route has no job to hand back, and would build over the
            # one that is running or has ended under this id (#1065).
            if snapshot.status in ("succeeded", "failed", "cancelled"):
                return _run_id_taken(
                    used_status,
                    run_id,
                    "run_id_ended",
                    "run_id already ended; submit the retry under a new run_id",
                )
            return _run_id_taken(
                used_status,
                run_id,
                "run_id_in_progress",
                "a build is running under this run_id; build under a new run_id",
            )
        return ServiceResponse(403, {"error": "forbidden: not run owner"})
    # A run a restart interrupted has neither a manifest nor a registry entry, but its
    # id is not free: the event store says who submitted it, and that person can still
    # read why it ended (#996). Someone else building under the id would read those
    # events while their job runs, and hand their own failure to the first submitter
    # if it is interrupted in turn (#1025).
    #
    # Nor is it free to its own submitter (#1042): a run id is one attempt. A second
    # build under it would append its events after the first attempt's ``run_failed``,
    # and each attempt's ending would be read as the other's state. A retry takes a new
    # id — the same answer a completed run's id gets.
    submission = service._event_store.submission(run_id)
    if submission is None:
        # run_id does not exist yet — new build.
        return None
    if not ownership_module.ownership_allows(
        created_by=submission.created_by, owner_id=submission.owner_id, principal=principal
    ):
        return ServiceResponse(403, {"error": "forbidden: not run owner"})
    # ``used_status``: 409 where the route's 409 is an error body (``POST /builds``, like
    # a completed run's id). ``POST /build`` declares its 409 as a build response, so it
    # asks for 400 — the id cannot be used for this request.
    return ServiceResponse(
        used_status,
        {
            "error": "run_id already ended; submit the retry under a new run_id",
            "code": "run_id_ended",
            "run_id": run_id,
        },
    )


def refuse_missing_provider_keys(service: BuilderService, spec_yaml: str) -> ServiceResponse | None:
    """The refusal for a build whose request carries no key for a provider it calls.

    One answer for both build routes (#1070): asynchronous, where the build would be
    accepted and fail later, and synchronous, where it failed in the same request as an
    ordinary provider error that named neither the cause nor the header.
    """
    missing = service.providers_missing_a_key(spec_yaml)
    if not missing:
        return None
    names = ", ".join(missing)
    return ServiceResponse(
        400,
        {
            "error": f"this build calls {names}, and the request carries no key for it; "
            "send it in the X-Provider-Key header",
            "code": "provider_credential_required",
            "providers": list(missing),
        },
    )


#: The states a job has ended in. A run named in ``retry_of`` is in one of them, has a
#: manifest, or was interrupted by a restart (#1103).
RETRIABLE_JOB_STATUSES = ("succeeded", "failed", "cancelled")


def check_retry_of(
    service: BuilderService,
    run_id: str | None,
    retry_of: str | None,
    principal: Principal,
    *,
    in_progress_status: int = 409,
) -> ServiceResponse | None:
    """Whether a build may say it retries ``retry_of`` (#1042, #1103).

    The link is a claim about another run, and it is shown back — on the job, in the
    manifest. So the named run must be one the caller may read: anyone else gets exactly
    what reading that run would give them, and learns nothing new from the attempt. A run
    cannot retry itself.

    And the named run must have **ended** (#1103). A retry takes over what that run had
    fetched (#1071); from a run still being written it would copy a checkpoint its
    writer is appending to, and two builds of one spec would then spend the provider's
    quota side by side. Ended means one of:

    - a manifest exists — the run finished, whatever its outcome;
    - its job is ``succeeded``, ``failed`` or ``cancelled`` (``RETRIABLE_JOB_STATUSES``);
    - only its submission is recorded: a restart interrupted it, and nothing runs it.

    ``queued``, ``running`` and ``cancelling`` are refused with ``retry_of_in_progress``.
    The state is asked after ownership, so it is told only to someone who could read it
    from ``GET /builds/{run_id}`` anyway.

    ``in_progress_status``: 409 where the route's 409 is an error body (``POST /builds``).
    ``POST /build`` declares its 409 as a build response, so it asks for 400.
    """
    if retry_of is None:
        return None
    if run_id is not None and run_id == retry_of:
        return ServiceResponse(400, {"error": "'retry_of' must name another run"})
    denied = check_active_run_access(service, retry_of, principal)
    if denied is not None:
        return denied
    # The job is read before the manifest: a job that is writing its manifest is still
    # ``running``, and the file alone would say it had ended.
    snapshot = service._async_builds.get(retry_of)
    if snapshot is None or snapshot.status in RETRIABLE_JOB_STATUSES:
        return None
    return ServiceResponse(
        in_progress_status,
        {
            "error": "the run named in 'retry_of' has not ended; wait for it or cancel it first",
            "code": "retry_of_in_progress",
            "retry_of": retry_of,
            "status": snapshot.status,
        },
    )

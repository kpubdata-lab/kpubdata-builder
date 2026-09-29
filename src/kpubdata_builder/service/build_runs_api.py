"""Build execution service — run, submit, poll and cancel (#596, #637).

The last domain left in ``BuilderService``. #637 held it back because its dependencies
were spread across the class: the provider credential lookup and client creation
alone were two, and together with spec validation, uploads, the event store, the
catalog, the index, the artifact store and the job registry a constructor would have
taken the class back. Those two now arrive as **one** callable, ``open_client`` — the
build path never used them apart — which is what makes the boundary narrower than
the class rather than a copy of it.

What stays on ``BuilderService``, on purpose:

- ``build``, ``submit_build``, ``build_status``, ``cancel_build`` as thin delegates, so
  routing, dispatch and every caller see the same methods;
- ``_run_build_job``, which the job registry calls and which calls ``self.build``.
  Tests subclass ``BuilderService`` and override both to hold a build at a chosen point
  (#596, coupling form 3). The registry's ``runner`` is passed at submit time, so an
  override is the method that runs.

**Wire contract unchanged.** Status codes, bodies and events are the ones the code
produced in ``app.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

import yaml
from typing_extensions import assert_never

from ..events import BuildEvent, BuildEventStore
from ..manifest import status_from_manifest
from ..pipeline import CancellationProbe, run_build
from ..spec import BuildSpec, JsonValue
from ..stages._path_safety import validate_path_segment
from ..stages.bronze.build import SourceClient
from ..store.artifacts import ArtifactStore
from ..store.build_index import BuildIndex
from ..uploads import UploadRepository
from ..warehouse import TableCatalog
from .auth import Principal
from .jobs import AsyncBuildExecutor, BuildJobRunner, generate_run_id
from .providers import ProviderCredentialConflictError
from .redaction import redact_json_secrets, redact_secret_text
from .responses import ServiceResponse

logger = logging.getLogger(__name__)

OpenClient = Callable[
    [Principal | None, str | None, tuple[str, ...]],
    tuple[SourceClient, Mapping[str, str]],
]
"""``(principal, credential_owner_id, providers) -> (client, provider_keys)``.

Resolves the requester's provider credentials and creates a client carrying them. The
keys come back too, because the build redacts them from what it returns. Raises
``ProviderCredentialConflictError``/``ValueError`` for a bad credential request and
anything else when no client can be made.
"""


def _declared_dataset_id(spec_yaml: str) -> str | None:
    """The spec's top-level ``dataset_id``, or None — read only to label the job (#781).

    Not validation: the worker validates the whole spec when it runs, and a spec that
    fails here fails there with a proper answer. This only lets a table show that a
    refresh is queued or running before the run finishes.
    """
    try:
        document = yaml.safe_load(spec_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(document, dict):
        return None
    dataset_id = document.get("dataset_id")
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


class BuildRunsApiService:
    """Runs a build, queues one, reports on it and cancels it."""

    def __init__(
        self,
        *,
        output_root: Path,
        api_version: str,
        load_validated: Callable[[str], BuildSpec | ServiceResponse],
        open_client: OpenClient,
        close_client: Callable[[SourceClient], None],
        upload_repository_for: Callable[[BuildSpec], UploadRepository | None],
        event_store: Callable[[], BuildEventStore],
        table_catalog: Callable[[], TableCatalog | None],
        warehouse_configured: bool,
        build_index: BuildIndex,
        store: ArtifactStore,
        async_builds: AsyncBuildExecutor,
    ) -> None:
        self._output_root = output_root
        self._api_version = api_version
        self._load_validated = load_validated
        self._open_client = open_client
        self._close_client = close_client
        self._upload_repository_for = upload_repository_for
        # Accessors, not values: the event store and the catalog are created on first
        # use, so a preview-only workspace leaves no file behind (#496, #703).
        self._event_store = event_store
        self._table_catalog = table_catalog
        self._warehouse_configured = warehouse_configured
        self._build_index = build_index
        self._store = store
        self._async_builds = async_builds

    def build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
        manifest_owner_id: str | None = None,
        credential_owner_id: str | None = None,
        principal: Principal | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> ServiceResponse:
        """Execute pipeline and return result.

        Response code policy:
            - All sources succeed: 200
            - Any source fetch/stage fails: 502 (upstream source dependency failure).
              Manifest kept per partial policy, outcomes and manifest in body.

        ``owner_id`` is canonical stable owner identity (#505). ``created_by``
        legacy display label recorded concurrently — wire contract unchanged.

        ``manifest_owner_id`` separate value for persisted manifest ownership (and
        BuildIndex reading it, #505 SSOT) only — separated from ``owner_id``
        (kind="file" source resolver upload ownership check, #498). Omit defaults
        to ``owner_id`` (backward compat). async run (``_run_build_job``) uses
        this to record submitting principal owner_id in manifest/BuildIndex while
        still not passing owner_id to file resolver (#496 follow-up).

        ``credential_owner_id`` internal value for async worker interpreting
        public_api credential only as submitting principal stable identity. Not
        passed to file source resolver; if request principal exists, principal's
        owner_id always takes precedence (ADR 0012, ADR 0014).

        ``cancellation`` cooperative cancel probe passed only from async job
        (#481). ``None`` (sync ``POST /build``, CLI) means pipeline never checks
        cancellation, behaving 100% as before — no cancel enforcement on sync
        builds. Cancelled-ending run returns 409 with ``status="cancelled"``
        summary; only async worker (``AsyncBuildExecutor._run``) sees this
        response, not in job snapshot or HTTP wire — partial artifacts' canonical
        source is partial manifest.
        """
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        # Provider credential meaningful only for kind="public_api" sources (#498) —
        # file/url sources' provider always empty string.
        provider_names = tuple(
            source.provider for source in spec_or_error.sources if source.kind == "public_api"
        )
        try:
            client, provider_keys = self._open_client(
                principal, credential_owner_id, provider_names
            )
        except (ProviderCredentialConflictError, ValueError) as exc:
            return ServiceResponse(400, {"error": str(exc)})
        except Exception:
            return ServiceResponse(502, {"error": "provider client unavailable"})
        try:
            result = run_build(
                spec_or_error,
                client=client,
                output_root=self._output_root,
                run_id=run_id,
                created_by=created_by,
                owner_id=owner_id,
                manifest_owner_id=manifest_owner_id,
                upload_repository=self._upload_repository_for(spec_or_error),
                event_store=self._event_store(),
                cancellation=cancellation,
                catalog=self._table_catalog(),
                # A provider that echoes the request would put the key into the data.
                secret_values=tuple(provider_keys.values()),
            )
        finally:
            self._close_client(client)
        secret_values = tuple(provider_keys.values())
        if secret_values:
            try:
                manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
                redacted_manifest = redact_json_secrets(manifest_data, secret_values)
                if redacted_manifest != manifest_data:
                    result.manifest_path.write_text(
                        json.dumps(redacted_manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                return ServiceResponse(500, {"error": "failed to secure build manifest"})
        outcomes: list[JsonValue] = [
            {
                "source_key": outcome.source_key,
                "status": outcome.status,
                "stages_completed": list(outcome.stages_completed),
                "error": redact_secret_text(outcome.error, secret_values),
            }
            for outcome in result.outcomes
        ]
        # Cancelled run (#481) is neither success nor failure. This response visible
        # only to async worker, not included in job snapshot (BuildJob.response), so
        # wire contract (200/502) stays unchanged — outcomes omitted so SourceOutcome
        # enum (ok/failed) not violated. Canonical source of partial artifacts is
        # partial manifest recorded below.
        cancelled = result.status == "cancelled"
        status_code = 200 if result.status == "ok" else 409 if cancelled else 502
        body: dict[str, JsonValue] = {
            "status": result.status,
            "run_id": result.context.run_id,
            "manifest": str(result.manifest_path),
            "api_version": self._api_version,
        }
        if not cancelled:
            body["outcomes"] = outcomes
        # Composition (#506) result exposed separately from outcomes (per-source) —
        # no bronze/silver/gold stage concept, must distinguish clearly as "combined
        # result". Null if BuildSpec.composition absent.
        if result.composition_outcome is not None:
            body["composition"] = {
                "name": result.composition_outcome.name,
                "status": result.composition_outcome.status,
                "error": redact_secret_text(result.composition_outcome.error, secret_values),
            }
        else:
            body["composition"] = None
        # Committed table snapshots (#703). Absent rather than empty when this
        # deployment has no warehouse: an empty object would say "nothing was
        # committed", and a caller cannot tell that from "committing was never
        # configured". The same distinction #700 drew for drift baselines.
        if self._warehouse_configured:
            body["materialized"] = {
                source_key: {
                    "table_id": committed.table.id,
                    "logical_name": committed.table.logical_name,
                    "snapshot_id": committed.snapshot.id,
                    "revision": committed.table.revision,
                }
                for source_key, committed in sorted(result.materialized.items())
            }
        # Failed build exposes first failed outcome's error as top-level `error`
        # summary, allowing consumers like Studio to surface human-readable reason
        # immediately without parsing outcomes array (#226). Also check
        # composition_outcome to catch case where only composition failed and all
        # sources succeeded (#506).
        if result.status == "failed":
            first_error = next(
                (o.error for o in result.outcomes if o.status != "ok" and o.error), None
            )
            if first_error is None and result.composition_outcome is not None:
                first_error = result.composition_outcome.error
            body["error"] = redact_secret_text(first_error, secret_values) or "build failed"

        # ADR 0003: Update index after build completes (best-effort, build success
        # maintained if fails)
        try:
            manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            started_at = manifest_data.get("started_at")
            finished_at = manifest_data.get("finished_at")
            self._build_index.insert_or_replace(
                run_id=result.context.run_id,
                status=result.status,  # type: ignore[arg-type]
                started_at=started_at,
                finished_at=finished_at,
                spec_digest=result.spec_digest,
                created_by=manifest_data.get("created_by"),
                owner_id=manifest_data.get("owner_id"),
                # dataset_id from canonical spec this run actually executed
                # (identical to snapshot) — derivation search value only, not guessed
                # separately (#488).
                dataset_id=spec_or_error.dataset_id,
            )
            # ADR 0016: Promote manifest document to authoritative store. sqlite/local
            # FS file is canonical (same content rewrite harmless), cubrid CUBRID row
            # canonical + FS mirror. best-effort — canonical/mirror already on FS,
            # promotion failure doesn't fail build.
            self._store.put_manifest(result.context.run_id, manifest_data)
        except Exception:
            # Index update/manifest promotion failure doesn't fail build (ADR 0003) —
            # canonical already on FS. But **not** silently. With no logging, unknown
            # how long/why BuildIndex lagged. Querying missed completed runs with no
            # investigative clues.
            logger.exception(
                "build index update or manifest promotion failed; "
                "the run itself succeeded and the filesystem copy is authoritative",
                extra={"run_id": result.context.run_id},
            )

        return ServiceResponse(status_code, body)

    def submit_build(
        self,
        spec_yaml: str,
        *,
        runner: BuildJobRunner,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """Queue async build job and return initial state (#482).

        ``runner`` is what the worker calls — ``BuilderService._run_build_job``, passed
        at submit time so a subclass override is the one that runs.

        ``owner_id`` persisted in job registry snapshot — not exposed in wire
        response (``to_body()``). This value in registry serves two: (1) active
        run ownership judgment (``check_active_run_access``, #496 follow-up),
        (2) ``_run_build_job`` passes as ``manifest_owner_id`` to build() for
        persisted manifest/BuildIndex (#505 SSOT) accurate owner_id recording.
        Still not passed as ``owner_id`` (kind="file" source resolver, #498) to
        build() — async file-backed source owner propagation limitation maintained.
        """
        resolved_run_id = run_id or generate_run_id()
        if self._build_index.get(resolved_run_id) is not None:
            return ServiceResponse(
                409,
                {
                    "error": "run_id already completed",
                    "run_id": resolved_run_id,
                },
            )

        def _record_run_submitted() -> None:
            # Called *before* AsyncBuildExecutor.submit() queues job to worker pool
            # (#496) — "existing"/"queue_full" means not new submission, so never
            # called. store.append() doesn't swallow failures, propagates directly
            # (BuildEventStore event timeline sole canonical source) — here keep
            # that propagation. At this point job not yet queued, so if this event
            # fails to record, job also never created: "event lost but job running"
            # contradiction never happens (recorder absorption differs; here no real
            # side effect yet to compromise other canonical).
            self._event_store().append(
                BuildEvent(
                    seq=0,
                    timestamp=datetime.now(tz=timezone.utc),
                    run_id=resolved_run_id,
                    event="run_submitted",
                    status="ok",
                    message="build accepted for async execution",
                )
            )

        def _record_enqueue_failure() -> None:
            # Called after registry.mark_failed(), before exception re-raise (#496
            # lifecycle contract: timeline itself must express this failure too) —
            # run_submitted already recorded, so don't erase (append-only), record
            # terminal event to same run_id with existing "run_failed" vocabulary.
            # No raw exception/stack trace — bounded, safe fixed message only
            # (consistent defensive principle with other recorder events). Even if
            # this append fails, log only and absorb — registry already confirmed
            # "failed", so this secondary event recording failure must not obscure
            # original enqueue failure (re-raised exception).
            try:
                self._event_store().append(
                    BuildEvent(
                        seq=0,
                        timestamp=datetime.now(tz=timezone.utc),
                        run_id=resolved_run_id,
                        event="run_failed",
                        status="fail",
                        message="build could not be queued for execution",
                    )
                )
            except Exception:
                logger.error(
                    "failed to record run_failed event after enqueue failure (run_id=%s)",
                    resolved_run_id,
                    exc_info=True,
                )

        try:
            result = self._async_builds.submit(
                spec_yaml=spec_yaml,
                run_id=resolved_run_id,
                created_by=created_by,
                owner_id=owner_id,
                dataset_id=_declared_dataset_id(spec_yaml),
                runner=runner,
                on_accept=_record_run_submitted,
                on_enqueue_failure=_record_enqueue_failure,
            )
        except Exception:
            # run_submitted event append failure, or event recorded but subsequent
            # worker pool queuing (executor.submit) itself fails — both reach here,
            # job not executed either way, so no 202 (#496).
            logger.error(
                "failed to accept build submission; build was not queued (run_id=%s)",
                resolved_run_id,
                exc_info=True,
            )
            return ServiceResponse(500, {"error": "failed to accept build submission"})
        match result.status:
            case "accepted":
                if result.snapshot is None:
                    raise RuntimeError("accepted async build is missing snapshot")
                return ServiceResponse(202, result.snapshot.to_body())
            case "existing":
                if result.snapshot is None:
                    raise RuntimeError("existing async build is missing snapshot")
                return ServiceResponse(200, result.snapshot.to_body())
            case "queue_full":
                return ServiceResponse(429, {"error": "async build queue is full"})
            case unreachable:
                assert_never(unreachable)

    def build_status(self, run_id: str) -> ServiceResponse:
        """Return active/terminal async build job status (#482).

        Registry holds terminal jobs max 256 (#666). Older run queried registry-
        only → 404 makes completed run "not found" due to age, yet artifacts/
        manifest still on disk. So registry miss falls through to persisted
        manifest. Manifest is terminal state canonical (``status_from_manifest``),
        making this path more authoritative than registry cache.

        404 only when manifest also missing — this server never created it or
        pipeline pre-entry termination left nothing.
        """
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        snapshot = self._async_builds.get(run_id)
        if snapshot is not None:
            return ServiceResponse(200, snapshot.to_body())
        evicted = self._build_status_from_manifest(run_id)
        if evicted is not None:
            return ServiceResponse(200, evicted)
        return ServiceResponse(404, {"error": f"build job not found: {run_id}"})

    def _build_status_from_manifest(self, run_id: str) -> dict[str, JsonValue] | None:
        """Restore evicted terminal job status from persisted manifest.

        Matches registry snapshot shape — callers need not distinguish paths.
        Does not carry ``response`` — that was build response body memory cache,
        not restorable from manifest. Omit rather than invent missing values.
        """
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return None
        manifest_status = status_from_manifest(manifest)
        status = "succeeded" if manifest_status == "ok" else manifest_status
        started = manifest.get("started_at")
        finished = manifest.get("finished_at")
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "status": status,
            "created_at": started if isinstance(started, str) else "",
            "updated_at": finished if isinstance(finished, str) else "",
        }
        created_by = manifest.get("created_by")
        if isinstance(created_by, str):
            body["created_by"] = created_by
        # error not carried. manifest error strings may contain paths (#664 same
        # reason), detail already provided by ``GET /builds/{run_id}/manifest`` —
        # no reason to re-expose here.
        return body

    def cancel_build(self, run_id: str) -> ServiceResponse:
        """Request cancel of active (queued/running) async build job (#481).

        Route must validate run_id format and apply ownership gate before calling
        (``routes/builds.py`` — same canonical rules as ``GET /builds/{run_id}``).

        Response determined by registry's atomic judgment (``request_cancel``) alone,
        deterministic regardless of race.

        - ``queued`` → immediately ``cancelled`` (runner never executes), 200.
        - ``running`` → ``cancelling``, 200. Terminal boundary comes next.
        - Already ``cancelling``/``cancelled`` → 200 (idempotent, current snapshot).
        - ``succeeded``/``failed`` or pipeline already confirmed normal termination
          → 409. Reuse existing conflict convention, not new error vocabulary
          (same "conflict" meaning as POST /builds "run_id already completed" 409).
        - registry unknown run → 404 (same message as ``GET /builds/{run_id}``).
        """
        outcome, snapshot = self._async_builds.request_cancel(run_id)
        if outcome == "unknown" or snapshot is None:
            return ServiceResponse(404, {"error": f"build job not found: {run_id}"})
        if outcome == "terminal":
            return ServiceResponse(
                409,
                {
                    "error": "build job is no longer cancellable",
                    "run_id": run_id,
                    "status": snapshot.status,
                },
            )
        if outcome == "cancelled":
            # queued job already terminalled here — worker doesn't execute runner
            # (``AsyncBuildExecutor._run``'s ``begin_run`` gate). Terminal event
            # recorded same as running path, only at terminal transition, once only.
            self.record_run_cancelled(run_id)
        return ServiceResponse(200, snapshot.to_body())

    def record_run_cancelled(self, run_id: str) -> None:
        """Record cancelled terminal event (#481). Failure not re-raised.

        Job already confirmed ``cancelled`` at this point — event append failure
        cannot undo confirmed terminal state, so log only and absorb like
        ``_record_enqueue_failure``. Called from worker thread, so exception
        propagation invalid. Message fixed string, no raw exception/path/credentials.
        """
        try:
            self._event_store().append(
                BuildEvent(
                    seq=0,
                    timestamp=datetime.now(tz=timezone.utc),
                    run_id=run_id,
                    event="run_cancelled",
                    status="ok",
                    message="build cancelled at a safe stage boundary",
                )
            )
        except Exception:
            logger.error("failed to record run_cancelled event (run_id=%s)", run_id, exc_info=True)


__all__ = ["BuildRunsApiService", "OpenClient"]

"""Publish domain service (#596 follow-up, #637).

Encapsulates readiness/publish/receipt/reconcile/audit and remote existence probe.

Two recent defects occurred in this area, and both went unnoticed because
**decision-making and invocation were buried in a single class**. #634: the publish
path derived manifest status independently, causing cancelled runs to be read as
succeeded — the canonical rule ``status_from_manifest`` already existed but was
unreachable. #632: the remote probe called ``dataset_info`` with a non-existent
argument; existing tests monkeypatched the method entirely, so no one reviewed the
body.

Separating the domain makes both structurally harder to miss. Required inputs are
exposed in the constructor (``output_root``, receipt repository, async job registry),
and the service can be instantiated without stubbing probes.

**wire contract is unchanged.** ``BuilderService`` delegates to this module using
the same method names, so route adapters and dispatch remain unaware.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import cast

from kpubdata_builder.credentials.store import CredentialRepository
from kpubdata_builder.manifest import status_from_manifest
from kpubdata_builder.publishers import PUBLISHER_REGISTRY
from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import publish as publish_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.service.publish_credentials import resolve_publish_credentials
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import BuildSpec, JsonValue

logger = logging.getLogger(__name__)

#: Manifest status vocabulary (ok/failed/cancelled) → publish status vocabulary.
#: (#481, #491). Maintains one place to bridge vocabularies, preventing the
#: publish path from deriving status independently and diverging from the
#: canonical rule.
_MANIFEST_TO_PUBLISH_STATUS: dict[str, publish_service.RunStatus] = {
    "ok": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _publish_receipt_response(
    claim_status: publish_service.PublishClaimStatus,
    receipt: publish_service.PublishReceipt,
) -> ServiceResponse | None:
    """Convert existing receipt state to replay/409 wire response."""
    if claim_status == "claimed":
        return None
    if claim_status == "replay" and receipt.result is not None:
        return ServiceResponse(200, cast(dict[str, JsonValue], receipt.result))
    if claim_status == "replay":
        claim_status = "state_unknown"
    conflict_codes = {
        "in_progress": (
            "publish_in_progress",
            "publish operation is already in progress",
        ),
        "state_unknown": (
            "publish_state_unknown",
            "publish operation outcome is unknown; automatic retry is blocked",
        ),
        "conflict": (
            "publish_conflict",
            "this run and destination were already published with different options",
        ),
    }
    code, message = conflict_codes[claim_status]
    return ServiceResponse(409, {"error": message, "code": code})


class PublishApiService:
    """Publish readiness/execution/receipt/reconcile/audit (#491, #551, #563)."""

    def __init__(
        self,
        *,
        output_root: Path,
        publish_receipts: publish_service.PublishReceiptStore,
        async_builds: AsyncBuildExecutor,
        credential_repository: CredentialRepository | None = None,
    ) -> None:
        self._output_root = output_root
        self._publish_receipts = publish_receipts
        self._async_builds = async_builds
        self._credential_repository = credential_repository

    def _publish_context(
        self, run_id: str
    ) -> tuple[publish_service.RunStatus, dict[str, JsonValue] | None, BuildSpec | None]:
        """Retrieve (status, manifest, spec) shared by readiness/POST.

        Called assuming route adapter has already determined existence/ownership
        (same pattern as ``/stages``, ``/quality``). If manifest exists, it is
        the canonical source (terminal run); otherwise, active/terminal status is
        read from async job registry (#482) — the same two sources used by
        ``routes._guards.check_active_run_access`` (#496 follow-up pattern reused).
        """
        manifest = cast(
            "dict[str, JsonValue] | None", datasets_service.read_manifest(self._output_root, run_id)
        )
        if manifest is not None:
            # Status determination is delegated to the manifest package's canonical
            # rule (#481). When only checking errors presence, cancelled runs were
            # read as succeeded — cancellation leaves no errors. run_status_blocker's
            # ``run_cancelled`` was already present but unreachable in this path,
            # allowing partial artifacts to be published to HF/Kaggle.
            status = _MANIFEST_TO_PUBLISH_STATUS[
                status_from_manifest(cast("dict[str, object]", manifest))
            ]
            spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
            return status, manifest, spec
        snapshot = self._async_builds.get(run_id)
        if snapshot is not None:
            return snapshot.status, None, None
        # route adapter's check_active_run_access already guarantees existence —
        # theoretically unreachable; fail-closed to failed.
        return "failed", None, None

    def publish_readiness(
        self,
        run_id: str,
        target: str,
        destination: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """GET /builds/{run_id}/publish/readiness (#491).

        Side-effect-free — does not call Publisher or create remote datasets.
        ready == no blockers present; computed deterministically.

        ``owner_id`` is used only for credential blocker determination. Without it,
        readiness only consulted server environment variables and returned ready —
        but actual POST validation is per-requester, so in deployments with closed
        fallback, readiness and publish would give different answers.
        """
        resolved_target, error = publish_service.resolve_target(target)
        if resolved_target is None:
            return ServiceResponse(
                400,
                {"error": error or "invalid target", "code": "unsupported_target"},
            )

        status, manifest, spec = self._publish_context(run_id)
        result = publish_service.build_readiness(
            run_id=run_id,
            target=resolved_target,
            destination=destination or "",
            status=status,
            manifest=cast("dict[str, object] | None", manifest),
            spec=spec,
            output_root=self._output_root,
            credentials=resolve_publish_credentials(
                self._credential_repository, owner_id, resolved_target
            ),
        )
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "target": result.target,
                "ready": result.ready,
                "blockers": cast(JsonValue, [b.to_body() for b in result.blockers]),
                "warnings": cast(JsonValue, [w.to_body() for w in result.warnings]),
            },
        )

    def publish(
        self,
        run_id: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """POST /builds/{run_id}/publish (#491).

        Performs the same deterministic checks as readiness — does not trust that
        the caller ran GET readiness first (TOCTOU: state can change after readiness
        passes). If any blocker exists, Publisher is never called.
        """
        if not isinstance(body, Mapping):
            return ServiceResponse(400, {"error": "request body must be a JSON object"})

        unknown_fields = sorted(
            str(key) for key in body if key not in {"target", "destination", "options"}
        )
        if unknown_fields:
            return ServiceResponse(
                400, {"error": f"unsupported request field(s): {unknown_fields!r}"}
            )

        resolved_target, error = publish_service.resolve_target(body.get("target"))
        if resolved_target is None:
            return ServiceResponse(
                400,
                {"error": error or "invalid target", "code": "unsupported_target"},
            )

        destination_error = publish_service.validate_destination(
            resolved_target, body.get("destination")
        )
        if destination_error is not None:
            return ServiceResponse(400, {"error": destination_error})
        destination = cast(str, body["destination"])

        options_error, options = publish_service.validate_options(
            resolved_target, body.get("options")
        )
        if options_error is not None:
            return ServiceResponse(400, {"error": options_error})

        owner_key = principal.owner_id or principal.label
        try:
            existing = self._publish_receipts.lookup(
                owner_key=owner_key,
                run_id=run_id,
                target=resolved_target,
                destination=destination,
                options=options,
            )
        except Exception as exc:
            logger.error(
                "publish receipt lookup failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            return ServiceResponse(
                409,
                {
                    "error": "publish operation state is unavailable; retry is blocked",
                    "code": "publish_state_unknown",
                },
            )
        if existing is not None:
            existing_response = _publish_receipt_response(*existing)
            if existing_response is not None:
                return existing_response

        # Move credential interpretation before readiness. Previously, it was
        # interpreted right before calling publisher; at that point, receipt was
        # already claimed, so rejection left the claim. Empty results omitted the
        # kwarg, causing publisher to fall back to ``os.environ`` — so
        # ``REQUIRE_OWN_PUBLISH_CREDENTIAL`` had no effect.
        credentials = resolve_publish_credentials(
            self._credential_repository, principal.owner_id, resolved_target
        )
        status, manifest, spec = self._publish_context(run_id)
        readiness = publish_service.build_readiness(
            run_id=run_id,
            target=resolved_target,
            destination=destination,
            status=status,
            manifest=cast("dict[str, object] | None", manifest),
            spec=spec,
            output_root=self._output_root,
            credentials=credentials,
        )
        if not readiness.ready or readiness.artifacts is None:
            return ServiceResponse(
                409,
                {
                    "error": f"run is not ready to publish to {resolved_target!r}",
                    "blockers": cast(JsonValue, [b.to_body() for b in readiness.blockers]),
                },
            )

        try:
            claim_status, receipt = self._publish_receipts.claim(
                owner_key=owner_key,
                run_id=run_id,
                target=resolved_target,
                destination=destination,
                options=options,
            )
        except Exception as exc:
            logger.error(
                "publish receipt claim failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            return ServiceResponse(
                409,
                {
                    "error": "publish operation state is unavailable; retry is blocked",
                    "code": "publish_state_unknown",
                },
            )

        claimed_response = _publish_receipt_response(claim_status, receipt)
        if claimed_response is not None:
            return claimed_response

        publisher = PUBLISHER_REGISTRY[resolved_target]
        # local target: interpret destination as absolute path within publish-root
        # (#550). Re-interpret and validate here even if readiness passed (TOCTOU
        # re-validation, same principle as #491).
        effective_destination: str = destination
        if resolved_target == "local":
            resolved_local = publish_service.resolve_local_destination(destination)
            if isinstance(resolved_local, publish_service.PublishIssue):
                return ServiceResponse(
                    409,
                    {
                        "error": f"run is not ready to publish to {resolved_target!r}",
                        "blockers": [cast(JsonValue, resolved_local.to_body())],
                    },
                )
            effective_destination = str(resolved_local[1])
        publish_kwargs: dict[str, object] = {"destination": effective_destination, **options}
        # Always pass even if empty. Publisher interprets ``credentials=None`` as
        # "caller did not set it" (CLI path) and reads environment variables —
        # service path always sets it. Omitting it here breaks that distinction.
        if not credentials.not_required:
            publish_kwargs["credentials"] = dict(credentials.values)
        try:
            result = publisher.publish(readiness.artifacts.paths, **publish_kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            # Exceptions from Publisher (PublishError, credential/dependency
            # RuntimeError, and other unreviewed exceptions) are not treated as
            # "safe known exceptions" — external SDK messages may contain raw
            # remote responses (#491 guideline 1) or local filesystem absolute paths.
            # Client always receives only stable generic messages.
            #
            # Server logs do not use str(exc)/repr(exc) or traceback (logger.exception()
            # re-logs raw message to log) — only exception type and already-vetted
            # context are logged.
            logger.error(
                "publish failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            try:
                self._publish_receipts.mark_unknown(receipt.fingerprint)
            except Exception as receipt_exc:
                logger.error(
                    "publish receipt unknown-state persist failed: "
                    "run_id=%s target=%s error_type=%s",
                    run_id,
                    resolved_target,
                    type(receipt_exc).__name__,
                )
            return ServiceResponse(
                502,
                {"error": "publish failed due to an unexpected error", "code": "publish_failed"},
            )

        response_body: dict[str, JsonValue] = {
            "run_id": run_id,
            "target": resolved_target,
            "publisher": result.publisher,
            "destination": destination,
            "reference": result.reference,
            "artifact_count": result.artifact_count,
            "status": result.status,
        }
        try:
            self._publish_receipts.mark_succeeded(
                receipt.fingerprint, cast(dict[str, object], response_body)
            )
        except Exception as exc:
            logger.error(
                "publish receipt success persist failed: run_id=%s target=%s error_type=%s",
                run_id,
                resolved_target,
                type(exc).__name__,
            )
            with suppress(Exception):
                self._publish_receipts.mark_unknown(receipt.fingerprint)
            return ServiceResponse(
                502,
                {"error": "publish failed due to an unexpected error", "code": "publish_failed"},
            )
        return ServiceResponse(200, response_body)

    def get_publish_receipt(
        self,
        run_id: str,
        target: str,
        destination: str,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """GET /builds/{run_id}/publish/receipt (#551).

        An operator permanently blocked on unknown receipt queries its state.
        Mismatched owner returns 404; does not expose existence of different
        owner's receipt.
        """
        owner_key = principal.owner_id or principal.label
        receipt = self._publish_receipts.get_by_key(
            owner_key=owner_key, run_id=run_id, target=target, destination=destination
        )
        if receipt is None:
            return ServiceResponse(
                404, {"error": "publish receipt not found", "code": "receipt_not_found"}
            )
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "target": receipt.target,
            "destination": receipt.destination,
            "state": receipt.state,
            "fingerprint": receipt.fingerprint,
            "options": cast(JsonValue, receipt.options),
            "reconcilable": receipt.state == "unknown",
        }
        if receipt.result is not None:
            body["result"] = cast(JsonValue, receipt.result)
        return ServiceResponse(200, body)

    def publish_audit_log(self, run_id: str, *, principal: Principal) -> ServiceResponse:
        """GET /builds/{run_id}/publish/audit (#563).

        Returns reconcile/reset audit history per owner — includes cases where
        receipt was already deleted by reset. Entries contain only minimal fields
        (fingerprint/action/actor/recorded_at); no credentials or path originals.
        """
        owner_key = principal.owner_id or principal.label
        entries = self._publish_receipts.audit_entries(owner_key=owner_key, run_id=run_id)
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "entries": cast(JsonValue, entries),
            },
        )

    def reconcile_publish(
        self,
        run_id: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """POST /builds/{run_id}/publish/reconcile (#551).

        Confirms unknown receipt by checking remote state. If results exist
        remotely, confirms as succeeded; if absent, resets receipt to allow
        re-publish (new claim). If remote check itself is impossible, returns 503
        — no changes are made.
        """
        if not isinstance(body, Mapping):
            return ServiceResponse(400, {"error": "request body must be a JSON object"})
        unknown_fields = sorted(str(key) for key in body if key not in {"target", "destination"})
        if unknown_fields:
            return ServiceResponse(
                400, {"error": f"unsupported request field(s): {unknown_fields!r}"}
            )

        resolved_target, target_error = publish_service.resolve_target(body.get("target"))
        if resolved_target is None:
            return ServiceResponse(
                400, {"error": target_error or "invalid target", "code": "unsupported_target"}
            )
        destination_error = publish_service.validate_destination(
            resolved_target, body.get("destination")
        )
        if destination_error is not None:
            return ServiceResponse(400, {"error": destination_error})
        destination = cast(str, body["destination"])

        owner_key = principal.owner_id or principal.label
        receipt = self._publish_receipts.get_by_key(
            owner_key=owner_key, run_id=run_id, target=resolved_target, destination=destination
        )
        if receipt is None:
            return ServiceResponse(
                404, {"error": "publish receipt not found", "code": "receipt_not_found"}
            )

        if receipt.state == "succeeded":
            # Idempotently return already-confirmed receipt state (no remote re-query).
            body_out: dict[str, JsonValue] = {
                "run_id": run_id,
                "state": "succeeded",
                "reconciled": False,
                "fingerprint": receipt.fingerprint,
            }
            if receipt.result is not None:
                body_out["result"] = cast(JsonValue, receipt.result)
            return ServiceResponse(200, body_out)

        probe = self._probe_remote_publish_target(resolved_target, destination)
        if probe is None:
            return ServiceResponse(
                503,
                {
                    "error": "remote state could not be determined; nothing was changed",
                    "code": "reconcile_unavailable",
                },
            )
        remote_exists = probe

        if remote_exists:
            result: dict[str, object] = {
                "run_id": run_id,
                "target": resolved_target,
                "destination": destination,
                "reconciled": True,
                "status": "succeeded",
            }
            try:
                self._publish_receipts.reconcile_succeeded(receipt.fingerprint, result)
            except Exception as exc:
                logger.error(
                    "publish receipt reconcile persist failed: run_id=%s target=%s error_type=%s",
                    run_id,
                    resolved_target,
                    type(exc).__name__,
                )
                return ServiceResponse(
                    503,
                    {
                        "error": "reconcile outcome could not be persisted; nothing was changed",
                        "code": "reconcile_unavailable",
                    },
                )
            return ServiceResponse(
                200,
                {
                    "run_id": run_id,
                    "state": "succeeded",
                    "reconciled": True,
                    "fingerprint": receipt.fingerprint,
                },
            )

        # No remote results — cannot confirm publishing actually happened;
        # nonetheless reset receipt to allow re-publish at operator discretion
        # (logged in audit trail).
        reset_ok = self._publish_receipts.reset(
            receipt.fingerprint, action="reconcile_absent_reset"
        )
        if not reset_ok:
            return ServiceResponse(
                503,
                {
                    "error": "reconcile reset could not be persisted; nothing was changed",
                    "code": "reconcile_unavailable",
                },
            )
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "state": "reset",
                "reconciled": True,
                "retry_allowed": True,
                "fingerprint": receipt.fingerprint,
            },
        )

    def reset_publish_receipt(
        self,
        run_id: str,
        target: str,
        destination: str,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """DELETE /builds/{run_id}/publish/receipt (#551) — explicit reset.

        Deletes receipt in any state, allowing new claim. No remote side effects
        occur (already-published results are not reverted). Logged in audit trail.
        """
        owner_key = principal.owner_id or principal.label
        receipt = self._publish_receipts.get_by_key(
            owner_key=owner_key, run_id=run_id, target=target, destination=destination
        )
        if receipt is None:
            return ServiceResponse(
                404, {"error": "publish receipt not found", "code": "receipt_not_found"}
            )
        reset_ok = self._publish_receipts.reset(receipt.fingerprint, action="manual_reset")
        if not reset_ok:
            return ServiceResponse(
                503,
                {
                    "error": "receipt reset could not be persisted; nothing was changed",
                    "code": "reconcile_unavailable",
                },
            )
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "state": "reset",
                "retry_allowed": True,
                "fingerprint": receipt.fingerprint,
            },
        )

    def _probe_remote_publish_target(self, target: str, destination: str) -> bool | None:
        """Check if publish result exists remotely (#551).

        Returns: True (exists)/False (absent)/None (cannot determine —
        credential/network issue). Probe is read-only; does not consume credentials
        or mutate remote state.
        """
        if target == "huggingface":
            token = os.environ.get("HF_TOKEN", "").strip()
            if not token:
                return None
            try:
                from huggingface_hub import HfApi  # type: ignore[import-not-found]
            except ImportError:
                return None
            try:
                api = HfApi(token=token)
                # dataset_info is already dataset-scoped and takes no repo_type.
                # Passing one raised TypeError, which the handler below swallowed
                # into "cannot tell" — so every probe reported inconclusive and
                # reconcile could never confirm a published dataset.
                api.dataset_info(repo_id=destination)
            except TypeError:
                # A signature mismatch is our bug, not a remote condition. Let it
                # surface instead of masquerading as an unreachable remote.
                raise
            except Exception as exc:
                # Distinguish repo absence (gated 401/404 variants) from access
                # failure — huggingface_hub reports absence as RepositoryNotFoundError.
                name = type(exc).__name__
                if name in ("RepositoryNotFoundError", "GatedRepoError"):
                    return False
                if getattr(exc, "status_code", None) in (401, 403):
                    # Non-existent private repos also appear as 401 due to HF;
                    # if owner, treat as absent (asset created with my credential
                    # should be accessible).
                    return False
                if getattr(exc, "status_code", None) == 404:
                    return False
                return None
            return True
        return None

"""Publish 도메인 서비스 (#596 후속, #637).

readiness / publish / receipt / reconcile / audit 과 원격 존재 조사(probe)를 담는다.

이 영역에서 최근 두 건의 결함이 나왔고, 둘 다 **판정과 호출이 한 클래스 안에 묻혀
있어서** 눈에 띄지 않았다. #634 는 publish 경로가 manifest 상태를 자체적으로
파생시켜 취소된 run 을 succeeded 로 읽었다 — 정본 규칙 ``status_from_manifest`` 가
이미 있었는데도 닿지 않았다. #632 는 원격 probe 가 존재하지 않는 인자로
``dataset_info`` 를 부르고 있었는데, 기존 테스트가 그 메서드를 통째로 monkeypatch
해서 아무도 본문을 보지 않았다.

도메인을 떼면 둘 다 구조적으로 어려워진다. 필요한 입력이 생성자에 드러나고
(``output_root``, receipt 저장소, async job registry), probe 를 stub 하지 않고도
서비스를 만들 수 있다.

**wire 계약은 바뀌지 않는다.** ``BuilderService`` 가 같은 이름의 메서드를 그대로
두고 여기로 위임하므로 route adapter 와 dispatch 는 아무것도 모른다.
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

#: manifest 상태 어휘(ok/failed/cancelled) → publish 상태 어휘 (#481, #491).
#: 두 어휘를 잇는 자리를 하나로 두어, publish 경로가 상태를 따로 파생시키다
#: 정본과 어긋나는 일을 막는다.
_MANIFEST_TO_PUBLISH_STATUS: dict[str, publish_service.RunStatus] = {
    "ok": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _publish_receipt_response(
    claim_status: publish_service.PublishClaimStatus,
    receipt: publish_service.PublishReceipt,
) -> ServiceResponse | None:
    """기존 receipt 상태를 replay/409 wire response로 변환한다."""
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
    """publish readiness/실행/receipt/reconcile/audit (#491, #551, #563)."""

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
        """publish readiness/POST가 공유하는 (status, manifest, spec) 조회.

        route adapter가 이미 존재/ownership을 판정했다는 전제 아래(``/stages``,
        ``/quality``와 동일한 패턴) 호출된다. manifest가 있으면 그것이 정본
        (terminal run)이고, 없으면 async job registry(#482)에서 active/terminal
        상태를 읽는다 — ``routes._guards.check_active_run_access``와 동일한
        두 소스를 쓴다(#496 follow-up 패턴 재사용).
        """
        manifest = cast(
            "dict[str, JsonValue] | None", datasets_service.read_manifest(self._output_root, run_id)
        )
        if manifest is not None:
            # 상태 판정은 manifest 패키지의 정본 규칙에 맡긴다 (#481). 여기서
            # errors 유무만 보던 시절에는 취소된 run이 succeeded로 읽혔다 —
            # 취소는 errors를 남기지 않기 때문이다. run_status_blocker의
            # ``run_cancelled``는 이미 있었지만 이 경로에서는 닿지 않았고,
            # 그래서 중간에 끊긴 partial 산출물이 HF/Kaggle에 게시될 수 있었다.
            status = _MANIFEST_TO_PUBLISH_STATUS[
                status_from_manifest(cast("dict[str, object]", manifest))
            ]
            spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
            return status, manifest, spec
        snapshot = self._async_builds.get(run_id)
        if snapshot is not None:
            return snapshot.status, None, None
        # route adapter의 check_active_run_access가 이미 존재를 보장했으므로
        # 이론상 도달하지 않는다 — fail-closed로 failed 취급한다.
        return "failed", None, None

    def publish_readiness(
        self,
        run_id: str,
        target: str,
        destination: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """GET /builds/{run_id}/publish/readiness (#491)."""
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
        """POST /builds/{run_id}/publish (#491)."""
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

        # credential 해석을 readiness **앞**으로 옮긴다. 예전에는 publisher 를
        # 부르기 직전에 해석했는데, 그 자리에서는 이미 receipt 를 claim 한
        # 뒤라 거절해도 claim 이 남는다. 그리고 결과가 비면 kwarg 를 생략해서
        # publisher 가 ``os.environ`` 으로 내려갔다 — 그래서
        # ``REQUIRE_OWN_PUBLISH_CREDENTIAL`` 이 아무 일도 하지 않았다.
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
        # local target은 destination을 publish-root 안의 절대 경로로 해석해
        # 넘긴다(#550). readiness가 이미 통과했어도 여기서 다시 해석·검증한다
        # (TOCTOU 재검증, #491과 동일 원칙).
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
        # 값이 비어 있어도 항상 넘긴다. publisher 는 ``credentials=None`` 만
        # "호출자가 정하지 않았다"(CLI 경로)로 보고 환경변수를 읽는다 — 서비스
        # 경로는 언제나 정한다. 여기서 생략하면 그 구분이 무너진다.
        if not credentials.not_required:
            publish_kwargs["credentials"] = dict(credentials.values)
        try:
            result = publisher.publish(readiness.artifacts.paths, **publish_kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            # Publisher가 던지는 예외(PublishError, credential/dependency
            # RuntimeError, 그 외 검토하지 않은 예외 포함)는 어떤 것도 "안전한
            # known exception"으로 취급하지 않는다 — 외부 SDK 예외 메시지에는
            # 원격 응답 원문이나(#491 지침 1) 로컬 filesystem 절대 경로가 섞일
            # 수 있다. client에는 항상 stable generic 메시지만 보낸다.
            #
            # 서버 log도 str(exc)/repr(exc)나 traceback(로그에 다시 raw
            # message를 남기는 logger.exception())을 쓰지 않는다 — exception
            # type과 이미 안전하다고 확인된 context만 남긴다.
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
        """GET /builds/{run_id}/publish/receipt (#551)."""
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
        """GET /builds/{run_id}/publish/audit (#563)."""
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
        """POST /builds/{run_id}/publish/reconcile (#551)."""
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
            # 이미 확정된 receipt는 멱등하게 그 상태를 돌려준다(원격 재조회 없음).
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

        # 원격에 결과가 없다 — 게시가 실제로 일어나지 않았다고 확정할 수 없어도
        # receipt를 reset해 운영자 판단으로 재게시를 허용한다(감사 로그에 남긴다).
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
        """DELETE /builds/{run_id}/publish/receipt (#551) — 명시적 reset.

        어떤 상태든 receipt를 삭제해 새 claim을 허용한다. 원격 부작용은 전혀
        발생하지 않는다(이미 게시된 결과를 되돌리지 않는다). 감사 로그에 남긴다.
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
        """원격에 publish 결과가 존재하는지 조사한다 (#551).

        반환값: True(존재)/False(부재)/None(판단 불가 — credential·네트워크 문제).
        조사 자체가 credential을 소비하거나 원격을 변경하지 않는 read-only다.
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
                # repo 부재(gated 401/404 계열)와 접근 실패를 구분한다 —
                # huggingface_hub는 부재를 RepositoryNotFoundError로 알려준다.
                name = type(exc).__name__
                if name in ("RepositoryNotFoundError", "GatedRepoError"):
                    return False
                if getattr(exc, "status_code", None) in (401, 403):
                    # 존재하지 않는 private repo도 401로 보이는 HF 특성상
                    # 소유자라면 부재로 간주한다(asset이 내 credential로
                    # 생성됐다면 접근 가능해야 하기 때문).
                    return False
                if getattr(exc, "status_code", None) == 404:
                    return False
                return None
            return True
        return None

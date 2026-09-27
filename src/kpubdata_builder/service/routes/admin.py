"""관리자 전용 route adapter (#679).

**여기서 노출하는 것은 메타데이터뿐이다.** run 의 산출물 바이트, 사용자의
credential, 응답 본문은 어떤 엔드포인트도 돌려주지 않는다.

이 제품은 BYOK 다 — 사용자가 자기 키로 받은 데이터는 사용자 것이라는 약속이
있다. 관리자가 그것을 열 수 있어야 하는지는 아직 정해지지 않았다(#679 의
(a)/(b)/(c)). 정해지기 전까지는 가장 좁은 쪽인 (a) 로 둔다 — 나중에 넓히는
것은 더할 수 있지만, 넓혀 놓고 좁히는 것은 이미 본 것을 되돌리지 못한다.

관리자가 할 수 있는 일:
    - 모든 run 의 **상태**를 본다 (누가·언제·성공했는지)
    - 지금 적용 중인 **정책 설정**을 본다

할 수 없는 일:
    - 산출물 열람 · credential 열람 · 남의 run 조작
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs

from ...spec import JsonValue
from .. import ownership as ownership_module
from .. import publish_credentials
from ..admin_audit import record_admin_action
from ..auth import Principal
from ..responses import ServiceResponse
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

#: /admin/runs 가 한 번에 돌려주는 최대 건수.
_MAX_LIMIT = 200

#: 종결된 job 상태. jobs.py 의 _TERMINAL_STATUSES 와 같은 집합이다 — 그쪽은
#: 비공개라 여기서 다시 적는다.
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})

def _forbidden(principal: Principal, action: str) -> ServiceResponse:
    """관리자가 아닌 요청. **거부도 기록한다** — 누가 관리 경로를 두드렸는지가
    허용된 요청만큼 중요하다."""
    record_admin_action(principal, action, outcome="denied")
    return ServiceResponse(403, {"error": "forbidden: administrator role required"})


def _parse_limit(query: str) -> int:
    raw = parse_qs(query).get("limit", ["50"])[-1]
    try:
        limit = int(raw)
    except ValueError:
        return 50
    return max(1, min(limit, _MAX_LIMIT))


def _admin_runs(service: BuilderService, principal: Principal, query: str) -> ServiceResponse:
    """모든 소유자의 run 을 상태만 돌려준다.

    ``BuildIndex`` 를 직접 읽는다. ``service.list_builds`` 는 응답 직전에
    ``owner_id`` 를 떼는데(#505), 관리자에게는 **어느 사용자의 run 인지가
    그 정보의 핵심**이다. ``owner_id`` 는 해시라 되돌릴 수 없어서, 사용자를
    구분하면서도 신원 자체를 드러내지 않는다 — 관리 목적에 정확히 맞는 수준이다.
    """
    limit = _parse_limit(query)
    try:
        entries = service._build_index.list_builds(limit=limit)
    except Exception:
        # 인덱스를 못 읽으면 파일시스템 폴백으로 내려가지 않는다. 폴백은
        # 소유자 정보가 덜 정확한데, 관리자 화면이 그것을 사실로 보여주면
        # 잘못된 근거로 판단하게 된다. 빈 목록이 낫다.
        record_admin_action(principal, "admin.runs.list", outcome="index_unavailable")
        return ServiceResponse(503, {"error": "build index unavailable"})

    # run_id -> (정렬 키, 응답 행). registry job 은 실제 시작 시각이 없으므로
    # 접수 시각을 순서에만 쓰고 응답에는 넣지 않는다. 완료된 index row 는
    # BuildIndex 의 계약과 같이 완료 시각으로 정렬한다.
    rows: dict[str, tuple[str, dict[str, JsonValue]]] = {}
    # 진행 중인 run 을 먼저 넣는다. BuildIndex 는 manifest 가 생긴 뒤에만
    # 채워지므로 queued/running job 은 인덱스에 아예 없다 — 그런데 멈춘 run 이
    # 바로 운영자가 찾는 것이다. 인덱스 항목이 같은 run_id 로 있으면 그쪽이
    # 이긴다(종결된 상태가 더 최신이다).
    for job in service._async_builds.list_all():
        # BuildJobSnapshot 은 created_at/updated_at 을 갖는다. finished_at 은
        # 종결된 job 에만 의미가 있으므로 진행 중이면 비워 둔다 — updated_at 을
        # 그대로 넣으면 "아직 도는 run 이 방금 끝났다" 로 읽힌다.
        finished = job.updated_at if job.status in _TERMINAL_JOB_STATUSES else None
        rows[job.run_id] = (
            job.created_at,
            {
                "run_id": job.run_id,
                "status": job.status,
                "started_at": None,
                "finished_at": finished,
                "owner_id": job.owner_id,
            },
        )
    for entry in entries:
        rows[entry.run_id] = (
            entry.finished_at or "",
            {
                "run_id": entry.run_id,
                "status": entry.status,
                "started_at": entry.started_at,
                "finished_at": entry.finished_at,
                "owner_id": entry.owner_id,
            },
        )

    ordered = sorted(
        rows.items(),
        key=lambda item: (item[1][0], item[0]),
        reverse=True,
    )[:limit]
    runs: list[JsonValue] = [cast(JsonValue, row) for _, (_, row) in ordered]
    record_admin_action(principal, "admin.runs.list", target=f"limit={limit}")
    return ServiceResponse(200, {"runs": runs, "count": len(runs)})


def _admin_config(principal: Principal) -> ServiceResponse:
    """지금 적용 중인 정책 설정.

    값이 아니라 **상태**만 돌려준다 — 토큰·키·issuer URL 은 넣지 않는다.
    운영자가 알아야 하는 것은 "소유권 강제가 켜져 있는가" 이지 그 값이 무엇으로
    설정됐는지가 아니다.
    """
    record_admin_action(principal, "admin.config.read")
    return ServiceResponse(
        200,
        {
            "enforce_ownership": ownership_module.enforce_ownership(),
            "publish_server_credential_fallback": (publish_credentials.server_fallback_allowed()),
        },
    )


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del body
    if not path.startswith("/admin/"):
        return None
    if method != "GET":
        return None

    if path == "/admin/runs":
        if not principal.is_admin:
            return _forbidden(principal, "admin.runs.list")
        return _admin_runs(service, principal, query)

    if path == "/admin/config":
        if not principal.is_admin:
            return _forbidden(principal, "admin.config.read")
        return _admin_config(principal)

    return None

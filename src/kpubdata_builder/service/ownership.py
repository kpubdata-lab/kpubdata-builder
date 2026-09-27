"""run/dataset 소유권 판정의 단일 canonical 게이트 (#389/#504/#505).

``service.app`` / ``service.datasets`` / ``query.resolver`` 등 모든
ownership consumer가 이 모듈을 공유한다 — endpoint마다 비교 로직을 중복
구현하지 않는다(#504 review).

게이팅 정책:
    - ``ENFORCE_OWNERSHIP`` off → 항상 허용(하위 호환).
    - dev/service principal → 모든 run 허용 (#679 이전부터. 아래 참조).

**OIDC 관리자는 여기서 통과하지 않는다.** ``Principal.is_admin`` 은 관리
엔드포인트(``routes/admin.py``)를 열 뿐이고, 남의 run 산출물 바이트를 열지는
않는다. 관리자가 사용자 데이터를 볼 수 있는지는 아직 정해지지 않았다(#679) —
이 제품은 BYOK 이고, "당신 키로 받은 데이터는 당신 것" 이 약속이다. 결정 전에
새로운 principal 종류로 그 권한을 넓히지 않는다.

dev/service 의 전체 접근은 #679 이전부터 있던 것이라 그대로 둔다. 여기서
빼면 기존 단일 사용자 배포와 API 키 기반 Studio 배포가 깨진다.

레코드 비교 자체는 ``service.auth.principal_owns``(#505 canonical:
stable ``owner_id`` 우선, legacy ``created_by``/label 폴백, fail-closed)를
그대로 사용한다 — 두 구현이 드리프트하지 않게 비교 로직은 auth에 단일로
둔다.
"""

from __future__ import annotations

import os

from .auth import Principal, principal_owns

_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


def enforce_ownership() -> bool:
    """소유권 강제가 활성화되어 있는지 (#389). 기본 off — 하위 호환."""
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


def _has_grandfathered_full_access(principal: Principal) -> bool:
    """dev/service principal 의 무조건 전체 run 접근 (#679 이전부터).

    ``Principal.is_admin`` 을 쓰지 않는다. OIDC 관리자까지 여기로 들이면
    "관리자는 남의 데이터를 본다" 를 결정 없이 확정하는 셈이 된다(#679).
    """
    return principal.kind in ("dev", "service")


def ownership_allows(
    *,
    created_by: str | None,
    owner_id: str | None,
    principal: Principal,
    enforce: bool | None = None,
) -> bool:
    """레코드(created_by/owner_id)를 principal이 접근할 수 있는지 판정한다.

    ``enforce``를 생략하면 환경변수(``ENFORCE_OWNERSHIP``)를 읽는다.
    비교는 ``principal_owns``(#505)에 위임한다.
    """
    if enforce is None:
        enforce = enforce_ownership()
    if not enforce:
        return True
    if _has_grandfathered_full_access(principal):
        return True
    return principal_owns(created_by=created_by, owner_id=owner_id, principal=principal)

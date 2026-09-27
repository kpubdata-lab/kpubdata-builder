"""관리 행위 감사 로그 (#679).

관리자는 자기 것이 아닌 자원을 본다. 그래서 **무엇을 했는지가 남아야 한다** —
남지 않으면 관리자 권한은 사후에 확인할 수 없는 권한이 된다.

여기서 하는 일은 한 줄 기록이 전부다. 저장소를 따로 두지 않는다 — 기록을
어디에 얼마나 보관할지는 배포가 정할 일이고, 표준 logging 으로 내보내면 그
결정을 배포의 로그 수집 설정에 맡길 수 있다.

**기록에 credential 을 넣지 않는다.** 이 모듈은 행위자·행위·대상만 받는다.
값이 아니라 이름만 받도록 시그니처가 강제한다.
"""

from __future__ import annotations

import logging

from .auth import Principal

__all__ = ["record_admin_action"]

#: 감사 전용 logger. 배포가 이것만 따로 수집·보관할 수 있게 이름을 분리한다.
_audit_logger = logging.getLogger("kpubdata_builder.admin_audit")


def record_admin_action(
    principal: Principal,
    action: str,
    *,
    target: str | None = None,
    outcome: str = "allowed",
) -> None:
    """관리 행위를 한 줄 남긴다.

    ``principal`` 에서 꺼내는 것은 ``label`` 과 ``owner_id`` 뿐이다 — 둘 다
    설계상 secret 을 담지 않는다(``Principal`` docstring 참조).

    ``target`` 은 자원 **식별자**다. 자원의 내용을 넣지 않는다.
    """
    _audit_logger.info(
        "admin action: actor=%s owner_id=%s action=%s target=%s outcome=%s",
        principal.label,
        principal.owner_id,
        action,
        target if target is not None else "-",
        outcome,
    )

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
import threading

from .auth import Principal

__all__ = ["record_admin_action"]

#: 감사 전용 logger. 배포가 이것만 따로 수집·보관할 수 있게 이름을 분리한다.
_audit_logger = logging.getLogger("kpubdata_builder.admin_audit")

# 이 서비스에는 logging 설정이 없다(``basicConfig``/``dictConfig`` 호출 0건).
# 그래서 root logger 의 기본 임계값 WARNING 이 적용되고, INFO 로 남긴 감사
# 기록은 **한 줄도 나가지 않는다.** 실측으로 확인했다.
#
# 감사 기록이 조용히 사라지는 것은 감사 기록이 없는 것보다 나쁘다 — 있다고
# 믿게 만든다. 그래서 이 logger 만은 스스로 임계값을 정하고, 배포가 아무
# handler 도 붙이지 않았을 때에 한해 stderr 로 내보낸다.
#
# 배포가 자체 handler 를 붙였으면 건드리지 않는다. 그쪽이 수집·보관 정책을
# 아는 주체다.
_audit_logger.setLevel(logging.INFO)


#: 이 모듈이 직접 붙인 fallback handler. 붙인 적이 있으면 다시 붙지 않는다 —
#: 여러 번 붙으면 같은 감사 기록이 여러 줄로 남아 개수를 셀 수 없게 된다.
_fallback_handler: logging.Handler | None = None

#: 첫 기록에서 판단하므로 두 요청이 동시에 들어올 수 있다. 잠그지 않으면 둘 다
#: 검사를 통과해 handler 가 두 개 붙고, 같은 기록이 두 줄 남는다.
_output_lock = threading.Lock()


def _ensure_audit_output() -> None:
    """handler 가 아무 데도 없을 때만 stderr handler 를 하나 붙인다.

    **import 시점이 아니라 첫 기록 시점에 판단한다.** import 는 배포가 logging
    을 설정하기 전에 일어날 수 있고, 그때 미리 붙여 두면 나중에 배포가 root
    handler 를 붙였을 때 같은 기록이 양쪽으로 두 번 나간다.
    """
    global _fallback_handler
    if _fallback_handler is not None:
        return
    with _output_lock:
        if _fallback_handler is not None:
            return
        logger: logging.Logger | None = _audit_logger
        while logger is not None:
            if logger.handlers:
                return
            if not logger.propagate:
                break
            logger = logger.parent
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))
        _audit_logger.addHandler(handler)
        _fallback_handler = handler


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
    _ensure_audit_output()
    _audit_logger.info(
        "admin action: actor=%s owner_id=%s action=%s target=%s outcome=%s",
        principal.label,
        principal.owner_id,
        action,
        target if target is not None else "-",
        outcome,
    )

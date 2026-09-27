"""Publish credential 해석 — 요청자별 우선, 서버 전역은 폴백 (#635).

publish credential 이 요청자별이 아니라 서버 환경변수 하나였다. 그러면 인증된
아무 사용자나 **서버 소유자의 Hugging Face / Kaggle 계정으로** 게시할 수 있다.
provider credential 은 이미 principal 별 저장소를 갖고 있는데(ADR 0012) publish
경로만 그 앞을 지나쳐 환경변수를 직접 읽었다.

여기서 기본값을 바꾸지는 않는다. 요청자에게 저장된 credential 이 있으면 그것을
쓰고, 없으면 예전처럼 서버 환경변수로 내려간다 — 단일 사용자 배포에서는 전역
토큰 하나가 정상 구성이고, 그 배포를 깨지 않는다. 다중 사용자 배포가 요청자별로
분리하고 싶으면 이제 그렇게 할 수 있다는 것이 이 모듈의 요점이다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from kpubdata_builder.credentials.store import CredentialRepository

__all__ = [
    "PUBLISH_CREDENTIAL_SLOTS",
    "PublishCredentialResolution",
    "resolve_publish_credentials",
    "server_fallback_allowed",
]

logger = logging.getLogger(__name__)

#: true 면 요청자에게 저장된 credential 이 없을 때 서버 환경변수로 내려가지
#: 않는다. 기본은 미설정(= 폴백 허용) — 단일 사용자 배포를 깨지 않는다.
_REQUIRE_OWN_CREDENTIAL_ENV = "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL"

#: target 별로 필요한 환경변수 이름과, 그 값을 저장하는 credential slot.
#:
#: slot 이름에 ``publish:`` 를 붙여 provider credential(``datago`` 등)과 같은
#: 이름공간을 쓰지 않게 한다 — 같은 owner 가 둘 다 가질 수 있어야 한다.
PUBLISH_CREDENTIAL_SLOTS: Mapping[str, tuple[str, ...]] = {
    "huggingface": ("HF_TOKEN",),
    "kaggle": ("KAGGLE_USERNAME", "KAGGLE_KEY"),
    "local": (),
}


def _slot(target: str, variable: str) -> str:
    """credential slot 이름. ``normalize_provider`` 를 통과해야 한다.

    저장소가 provider key 를 ``^[a-z0-9][a-z0-9_-]{0,63}$`` 로 검증하므로 콜론을
    쓸 수 없다. 처음에 ``publish:huggingface:HF_TOKEN`` 으로 만들었다가 실제
    SQLite 저장소에서 ValueError 가 났다 — 가짜 repository 로만 테스트해서
    놓쳤다. 하이픈으로 구분하고 소문자로 눕힌다.
    """
    return f"publish-{target}-{variable}".lower().replace("_", "-")


@dataclass(frozen=True, slots=True)
class PublishCredentialResolution:
    """이 게시에 쓸 credential 과, 왜 비어 있는지.

    빈 mapping 하나로는 세 가지 서로 다른 상황을 구분할 수 없다 — target 이
    credential 을 아예 요구하지 않는 경우(local), 아무 데도 값이 없는 경우,
    그리고 요청자에게 저장된 값이 없어서 **정책상 거절된** 경우다. 호출자가
    빈 dict 를 보고 "그럼 안 넘기면 되지" 로 처리하는 바람에 publisher 가
    ``os.environ`` 으로 내려갔고, ``REQUIRE_OWN_PUBLISH_CREDENTIAL`` 이 아무
    일도 하지 않았다.
    """

    values: Mapping[str, str] = field(default_factory=dict)
    #: 요청자에게 저장된 credential 이 없고, 서버 폴백도 닫혀 있다.
    refused: bool = False
    #: 이 target 은 credential 자체가 필요 없다(local).
    not_required: bool = False


def resolve_publish_credentials(
    repository: CredentialRepository | None,
    owner_id: str | None,
    target: str,
) -> PublishCredentialResolution:
    """이 게시에 쓸 credential 을 해석한다.

    우선순위는 ``CredentialResolver`` 와 같다 — 요청자 credential, 그다음 서버
    환경변수. 어느 쪽에도 없으면 그 키는 결과에 없다.

    **부분 해석을 하지 않는다.** kaggle 처럼 두 값이 한 쌍인 target 에서 하나는
    요청자 것, 하나는 서버 것을 섞으면 어느 계정으로 게시되는지 아무도 말할 수
    없다. 요청자가 그 target 의 값을 하나라도 저장해 두었으면 그 target 은
    요청자 credential 로만 해석한다.
    """
    variables = PUBLISH_CREDENTIAL_SLOTS.get(target, ())
    if not variables:
        return PublishCredentialResolution(not_required=True)

    stored: dict[str, str] = {}
    if repository is not None and owner_id is not None:
        for variable in variables:
            # 저장소 조회 실패(키 형식, 복호화, 백엔드 장애)를 게시 실패로
            # 흘리지 않는다 — 저장된 credential 이 없는 것과 같게 다루고 서버
            # 폴백으로 내려간다. 다만 조용히 넘기지는 않는다.
            try:
                value = repository.get_secret(owner_id, _slot(target, variable))
            except Exception:
                logger.warning(
                    "could not read the stored %s publish credential for this principal; "
                    "falling back to the server environment",
                    target,
                    exc_info=True,
                )
                stored = {}
                break
            if value:
                stored[variable] = value
    if stored:
        return PublishCredentialResolution(values=stored)

    if not server_fallback_allowed():
        # 서버 토큰 폴백을 닫으면, credential 을 저장하지 않은 principal 은
        # 게시할 수 없다. 다중 사용자 배포가 "아무나 서버 소유자 계정으로 게시"
        # 를 끝내려면 이 스위치가 필요하다 (#635).
        return PublishCredentialResolution(refused=True)

    resolved: dict[str, str] = {}
    for variable in variables:
        value = os.environ.get(variable, "").strip()
        if value:
            resolved[variable] = value
    return PublishCredentialResolution(values=resolved)


def server_fallback_allowed() -> bool:
    """저장된 credential 이 없을 때 서버 환경변수로 내려가도 되는지.

    기본은 허용이다 — 단일 사용자 배포에서 전역 토큰 하나는 정상 구성이고,
    기본값을 뒤집으면 그 배포가 조용히 게시를 멈춘다. 다중 사용자 배포는
    ``KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL=true`` 로 닫는다.
    """
    return os.environ.get(_REQUIRE_OWN_CREDENTIAL_ENV, "").lower() not in ("true", "1")

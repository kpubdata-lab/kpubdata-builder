"""HTTP 서비스 인증 (#384 B2, #385 B3, ADR 0006/0009, #505).

인증 결과를 ``bool`` 대신 ``Principal`` 로 반환해 "누가 요청했는가"를 보존한다 (B2).
본 모듈은 두 인증 경로를 통합한다 (B3, ADR 0009):

- ``X-API-Key`` — 서비스 계정(스케줄 워크플로 등 Google 로그인 불가 소비자).
- ``Authorization: Bearer <Google ID token>`` — 사람 사용자(Studio). JWKS로 오프라인 검증.

``OIDC_ISSUER`` 미설정 시 Bearer 경로가 비활성화되어 기존 배포에 영향이 없다.
설정 시 ``OIDC_AUDIENCE`` 가 필수이고 ``pyjwt`` extra가 설치되어야 한다 (fail-closed).
IdP에 계정을 만든 사람은 누구나 로그인할 수 있는 것이 기본 정책이다 — 특정 조직/개인으로
제한하려면 ``OIDC_ALLOWED_HD``/``OIDC_ALLOWED_SUBJECTS``/``OIDC_ALLOWED_EMAILS`` 를
설정한다 (선택, deploy.md 참조).

``Principal`` 의 display 역할과 persistent ownership 역할을 분리한다 (#505):

- ``identifier``/``label`` — 사람이 읽는 표시용 라벨이자 기존(#388/#389) ``created_by``와의
  하위 호환 비교에 쓰인다. OIDC의 경우 로그 노출 최소화를 위해 이미 ``sub`` 앞 8자만
  담아왔다 — 이 필드는 트렁케이션이 있어 단독으로는 충돌 방지를 보장하지 않는다.
- ``owner_id`` — ``compute_owner_id()`` 로 계산되는 canonical하고 stable한 persistent
  owner identity. OIDC는 트렁케이션 없이 전체 issuer+subject를 해시해 충돌을 방지한다.
  신규 리소스의 ownership 판정은 이 필드를 우선 사용해야 한다(``principal_owns()`` 참조).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import time
from dataclasses import dataclass

# 서버가 요구하는 API 키. 환경변수로만 주입한다 (#248).
# ADR 0006에 따라 fail-closed로 동작: dev-mode 미설정 + API 키 미설정 시 인증을 거부한다.
_API_KEY_ENV = "KPUBDATA_BUILDER_API_KEY"
_DEV_MODE_ENV = "KPUBDATA_BUILDER_DEV_MODE"

# OIDC 설정 (ADR 0009, #385). OIDC_ISSUER 미설정 시 Bearer 비활성.
_OIDC_ISSUER_ENV = "OIDC_ISSUER"
_OIDC_AUDIENCE_ENV = "OIDC_AUDIENCE"
_OIDC_JWKS_URL_ENV = "OIDC_JWKS_URL"
_OIDC_JWKS_TTL_ENV = "OIDC_JWKS_TTL"
_DEFAULT_JWKS_TTL_SECONDS = 3600
_DISCOVERY_TIMEOUT_SECONDS = 5
_TOKEN_LEEWAY_SECONDS = 60
_OIDC_ALLOWED_HD_ENV = "OIDC_ALLOWED_HD"
_OIDC_ALLOWED_SUBJECTS_ENV = "OIDC_ALLOWED_SUBJECTS"
_OIDC_ALLOWED_EMAILS_ENV = "OIDC_ALLOWED_EMAILS"
#: ownership.enforce_ownership() 이 읽는 이름. 여기서 import 하면 순환이므로
#: 이름만 둔다 — 두 곳이 갈리면 test_env_var_contract 가 잡는다.
_ENFORCE_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"
# 허용 목록을 "필수"로 되돌리는 스위치. 미설정이면 공개 가입(제한 없음)이 기본이다.
_OIDC_REQUIRE_ALLOWLIST_ENV = "OIDC_LEGACY_REQUIRE_ALLOWLIST"

#: 관리자로 대우할 OIDC ``sub`` 목록 (#679). 쉼표/공백 구분.
#:
#: 관리자는 **설정으로만** 지정한다. 런타임에 API 로 관리자를 늘릴 수 있으면
#: 관리자 하나가 탈취됐을 때 되돌릴 방법이 없다 — 설정 파일과 재시작이
#: 되돌리는 경로다.
_ADMIN_SUBJECTS_ENV = "KPUBDATA_BUILDER_ADMIN_SUBJECTS"

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Principal:
    """인증된 요청 주체 (#384, #505).

    인가(C1/C2/#505)에서 run 소유권 판단에 쓰인다. ``kind`` 는 확장 가능 —
    ``'dev'``(로컬 개발), ``'service'``(X-API-Key), ``'oidc'``(Bearer, ADR 0009).
    식별자는 민감 값(원본 API 키 등)을 담지 않는다 — manifest created_by 등에는
    안전한 라벨(``apikey:<name>``)만 쓴다.

    ``identifier``/``label`` 은 display/legacy 호환용이고, ``owner_id`` 가 신규
    persistent ownership 판정에 쓰이는 canonical stable identity다(#505). 이 둘의
    역할은 의도적으로 분리되어 있다 — display label(예: 향후 프로필 이름/이메일
    노출)이 바뀌어도 ``owner_id`` 는 바뀌지 않아야 한다.
    """

    kind: str
    identifier: str | None = None
    owner_id: str | None = None
    #: 관리 작업(#679)을 할 수 있는가. ``kind`` 로 유추하지 않는다 — 예전에는
    #: ``kind in ("dev", "service")`` 가 곧 관리자였고, 그래서 **관리자가 되는
    #: 유일한 방법이 OIDC 로 로그인하지 않는 것**이었다. 역할을 값으로 들고
    #: 있어야 OIDC 로 인증한 사람도 관리자가 될 수 있다.
    is_admin: bool = False

    @property
    def label(self) -> str:
        """manifest created_by용 display 라벨 (#388).

        하위 호환을 위해 유지된다 — legacy(#505 이전) 리소스의 ownership fallback
        비교에 쓰인다(``principal_owns()`` 참조). 신규 ownership 판정에는 대신
        ``owner_id`` 를 우선 사용해야 한다.
        """
        return f"{self.kind}:{self.identifier}" if self.identifier else self.kind


def compute_owner_id(kind: str, *material: str) -> str:
    """canonical하고 stable한 persistent owner identity를 계산한다 (#505).

    해시 입력은 모든 필드(kind와 material 각 부분)를 8바이트 big-endian 길이
    프리픽스로 프레이밍해 이어 붙인다 — 구분자 기반 결합(``"\\0"`` 등)은 필드
    값에 구분자가 포함되면 충돌할 수 있어(예: issuer="a", sub="b\\0c" vs
    issuer="a\\0b", sub="c") #505의 concatenation collision 방지 보장을 깨뜨릴
    수 있으므로 쓰지 않는다. 길이 프리픽스는 필드 경계를 결정적으로 고정해
    모호성이 없다.

    ``kind`` 을 해시 입력에 포함시켜(principal 종류가 다르면 동일 material이라도
    절대 같은 owner_id가 나오지 않게 한다 — domain separation).

    반환값은 SHA-256 hex digest 기반이라 원본 claim(sub/email 등)을 복원할 수
    없다 — owner ID를 로그/저장소에 남겨도 raw claim이 노출되지 않는다.
    """
    framed = b"".join(_frame_owner_id_field(value) for value in (kind, *material))
    digest = hashlib.sha256(framed).hexdigest()
    return f"{kind}:{digest}"


def _frame_owner_id_field(value: str) -> bytes:
    raw = value.encode("utf-8")
    return len(raw).to_bytes(8, "big") + raw


def principal_owns(*, created_by: str | None, owner_id: str | None, principal: Principal) -> bool:
    """레코드가 ``principal`` 소유인지 판정하는 단일 canonical 구현이다 (#505).

    ``/query``, ``/builds``, dataset/stage/quality 조회 등 모든 ownership
    consumer가 이 함수를 공유한다 — endpoint마다 비교 로직을 중복 구현하지 않는다.

    - 레코드와 principal 양쪽 모두 stable ``owner_id`` 가 있으면(신규 경로) 이를
      우선 비교한다 — OIDC subject 트렁케이션 충돌 없이 안전하다.
    - 둘 중 하나라도 ``owner_id`` 가 없으면(legacy 레코드 또는 owner_id 미설정
      principal) 기존 ``created_by``/``label`` 비교로 폴백한다(#388/#389 이후
      하위 호환 — 기존 리소스를 즉시 접근 불가로 만들지 않는다).
    - 두 값이 모두 없으면(예: created_by가 아예 기록되지 않은 legacy 레코드)
      비교는 항상 실패한다 — "owner 정보 없음 = 누구나 접근 가능"으로 취급하지
      않는다(fail-closed).
    """
    if owner_id is not None and principal.owner_id is not None:
        return owner_id == principal.owner_id
    return created_by == principal.label


@dataclass(frozen=True)
class AuthError:
    """인증 실패. ``status_code`` 로 dispatch 응답 코드를 결정한다 (#385).

    기본 401(인증 거부). JWKS 조회 실패 등 일시적 인프라 장애는 503로 구분해
    클라이언트가 재시도를 구분하게 한다.
    """

    reason: str
    status_code: int = 401


def _is_dev_mode() -> bool:
    """로컬 개발 모드인지 확인한다 (#321, ADR 0006).

    KPUBDATA_BUILDER_DEV_MODE가 'true'/'1'이면 dev-mode로 간주하여 인증을 생략한다.
    프로덕션 배포에서는 이 환경변수를 설정하지 않아야 한다.
    """
    return os.environ.get(_DEV_MODE_ENV, "").lower() in ("true", "1")


def _verify_api_key(api_key: str | None) -> Principal | AuthError:
    """X-API-Key 경로 (B2). fail-closed: 키 미설정·불일치 → AuthError.

    현재 구성은 인스턴스당 단일 공유 정적 키만 지원한다(ADR 0006) — 키 값 자체를
    owner_id 소재로 쓰지 않는다(원문 secret이 owner_id/로그에 노출되면 안 됨,
    #505). 고정된 이름표("default")를 해시해 하나의 stable service owner
    identity를 부여한다.
    """
    expected = os.environ.get(_API_KEY_ENV)
    if not expected:
        return AuthError(reason="api key not configured")
    # compare_digest 는 str 두 개를 받을 때 ASCII 만 허용한다 — 비ASCII 헤더
    # 하나가 TypeError 로 500 을 만들고, 그 경로는 인증 실패로 기록되지도 않아
    # 스로틀을 그냥 지나친다. 바이트로 비교하면 그런 입력도 평범한 불일치다.
    if api_key is not None and hmac.compare_digest(
        api_key.encode("utf-8"), expected.encode("utf-8")
    ):
        # service principal 은 계속 관리자다 — 예전 ``kind in ("dev", "service")``
        # 게이트와 같은 권한이다(#679). 달라진 것은 그 사실이 값으로 보인다는 것.
        return Principal(
            kind="service",
            owner_id=compute_owner_id("service", "default"),
            is_admin=True,
        )
    return AuthError(reason="invalid api key")


# --- OIDC Bearer (B3, ADR 0009) -------------------------------------------------
# JWKS 클라이언트는 지연 생성·캐시한다. OIDC 비활성 시 None.
_jwks_client: object | None = None
_jwks_url_cached: str | None = None
_jwks_lock = threading.Lock()
# OIDC discovery 결과 캐시: issuer → (jwks_uri, expires_at). #435.
_discovery_cache: dict[str, tuple[str, float]] = {}


def _oidc_issuers() -> list[str]:
    raw = os.environ.get(_OIDC_ISSUER_ENV, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _discover_jwks_uri(issuer: str) -> str:
    """OIDC discovery 문서에서 jwks_uri를 읽는다 (RFC 8414, #435).

    issuer의 ``/.well-known/openid-configuration``을 조회해 ``jwks_uri`` 필드를
    반환한다. Google/Auth0/Keycloak 등 IdP마다 JWKS 경로가 달라 경로 추정이
    깨지던 기존 동작을 대체한다. 결과는 TTL 캐시된다.
    """
    import json
    import urllib.request

    now = time.monotonic()
    cached = _discovery_cache.get(issuer)
    if cached is not None:
        jwks_uri, expires_at = cached
        if now < expires_at:
            return jwks_uri

    base = issuer if issuer.startswith("http") else "https://" + issuer
    discovery_url = base.rstrip("/") + "/.well-known/openid-configuration"
    with urllib.request.urlopen(discovery_url, timeout=_DISCOVERY_TIMEOUT_SECONDS) as resp:
        doc = json.loads(resp.read())
    jwks_uri_raw = doc.get("jwks_uri")
    if not isinstance(jwks_uri_raw, str) or not jwks_uri_raw:
        raise RuntimeError(f"discovery at {discovery_url} has no jwks_uri")
    ttl = int(os.environ.get(_OIDC_JWKS_TTL_ENV, "") or _DEFAULT_JWKS_TTL_SECONDS)
    _discovery_cache[issuer] = (jwks_uri_raw, now + ttl)
    return jwks_uri_raw


def _oidc_jwks_url() -> str:
    """JWKS URL. OIDC_JWKS_URL 명시 시 discovery를 건너뛴다 (#435).

    명시 없으면 첫 issuer의 discovery 문서에서 jwks_uri를 읽는다 (RFC 8414).
    기존 ``issuer + /.well-known/jwks.json`` 추정은 Google이 저 경로를 쓰지
    않아 404 → 503 실패를 일으켰다.
    """
    explicit = os.environ.get(_OIDC_JWKS_URL_ENV, "").strip()
    if explicit:
        return explicit
    issuers = _oidc_issuers()
    if not issuers:
        raise RuntimeError("OIDC_ISSUER not set")
    return _discover_jwks_uri(issuers[0])


def _oidc_allowlists() -> tuple[set[str], set[str], set[str]]:
    """(hd, subjects, emails) 허용 목록 — 설정된 경우에만 적용되는 선택적 제한 (#386).

    기본 정책은 공개 가입이다(IdP에 계정이 있으면 누구나 로그인). 특정 도메인/계정만
    허용하는 제한 배포에서만 이 목록을 설정하고, 그때는 하나라도 매칭돼야 통과한다.
    """

    def _parse(env_name: str) -> set[str]:
        raw = os.environ.get(env_name, "")
        return {s.strip() for s in raw.replace(" ", ",").split(",") if s.strip()}

    return (
        _parse(_OIDC_ALLOWED_HD_ENV),
        _parse(_OIDC_ALLOWED_SUBJECTS_ENV),
        _parse(_OIDC_ALLOWED_EMAILS_ENV),
    )


def _admin_subjects() -> set[str]:
    """관리자로 대우할 OIDC ``sub`` 집합 (#679). 미설정이면 빈 집합."""
    raw = os.environ.get(_ADMIN_SUBJECTS_ENV, "")
    return {s.strip() for s in raw.replace(" ", ",").split(",") if s.strip()}


def validate_oidc_config() -> None:
    """서버 기동 시 호출 (serve). OIDC 설정 오류면 RuntimeError (fail-closed, #385).

    - OIDC_ISSUER 미설정 → no-op (Bearer 비활성, 기존 배포 무영향).
    - OIDC_ISSUER 설정 + OIDC_AUDIENCE 미설정 → 거부.
    - pyjwt 미설치 → 거부 (``auth`` extra 필요).
    - 허용 목록(OIDC_ALLOWED_*)은 **선택**이다 — 공개 가입이 기본 정책이기 때문이다.
      제한 배포에서 목록 누락을 기동 실패로 잡고 싶으면
      ``OIDC_LEGACY_REQUIRE_ALLOWLIST=true`` 를 설정한다.
    """
    if not _oidc_issuers():
        return
    if not os.environ.get(_OIDC_AUDIENCE_ENV, "").strip():
        raise RuntimeError(
            "OIDC_ISSUER is set but OIDC_AUDIENCE is missing; "
            "refusing to start (fail-closed, ADR 0009)."
        )
    try:
        import jwt  # noqa: F401
    except ImportError as e:  # pragma: no cover - dev 환경에서 extra 누락 시
        raise RuntimeError(
            "OIDC is enabled but pyjwt is not installed; install with: uv sync --extra auth"
        ) from e
    # 허용 목록은 선택이다(공개 가입이 기본). 제한 배포만 이 스위치로 필수화한다.
    hd, subs, emails = _oidc_allowlists()
    open_signup = not (hd or subs or emails)
    if open_signup and os.environ.get(_OIDC_REQUIRE_ALLOWLIST_ENV) == "true":
        raise RuntimeError(
            "OIDC_ISSUER is set but no allowlist is configured "
            "(OIDC_ALLOWED_HD/SUBJECTS/EMAILS) while "
            f"{_OIDC_REQUIRE_ALLOWLIST_ENV}=true; refusing to start "
            "(fail-closed, ADR 0009, #386)."
        )
    # 두 기본값이 각각은 의도된 것이지만, 겹치면 "아무 Google 계정이나 로그인해
    # 모든 run 과 query 에 접근한다" 가 된다. 둘 다 기본값이라 아무도 그 조합을
    # 고른 적이 없고, 지금까지는 기동 로그에도 흔적이 없었다. 거부하지는 않는다 —
    # 단일 사용자 배포에서는 정상적인 구성이다.
    if open_signup and os.environ.get(_ENFORCE_OWNERSHIP_ENV, "").lower() not in ("true", "1"):
        _logger.warning(
            "OIDC signup is open (no OIDC_ALLOWED_HD/SUBJECTS/EMAILS) and %s is off: "
            "any account that can obtain a token from this issuer will be able to read "
            "and overwrite every run. Set an allowlist, or %s=true, or both.",
            _ENFORCE_OWNERSHIP_ENV,
            _ENFORCE_OWNERSHIP_ENV,
        )


def validate_dev_mode() -> None:
    """서버 기동 시 호출 (serve). dev-mode의 운영 배포 사고를 막는다.

    dev-mode는 ``authenticate()`` 의 첫 분기로, API 키도 Bearer 토큰도 검사하지 않고
    모든 요청을 통과시킨다 (#321, ADR 0006). 로컬 개발 전용이므로:

    - dev-mode가 켜져 있으면 기동 로그에 경고를 남긴다 — 서비스가 무인증으로 열려
      있다는 사실이 로그만 봐도 드러나야 한다.
    - dev-mode와 OIDC 설정이 동시에 있으면 기동을 거부한다. 사용자 인증을 구성해두고
      인증을 통째로 우회하는 것은 어떤 환경에서도 의도일 수 없으며, 운영 배포에
      dev 플래그가 남아 있는 전형적인 사고 형태다 (fail-closed).
    """
    if not _is_dev_mode():
        return
    if _oidc_issuers():
        raise RuntimeError(
            f"{_DEV_MODE_ENV} is enabled while {_OIDC_ISSUER_ENV} is configured; "
            "refusing to start — dev-mode bypasses authentication entirely, so a "
            "deployment that configures user authentication must not set it "
            "(fail-closed, ADR 0006)."
        )
    _logger.warning(
        "%s is enabled: every request is accepted without authentication. "
        "This is for local development only — never set it in a deployment.",
        _DEV_MODE_ENV,
    )
    if os.environ.get(_API_KEY_ENV):
        _logger.warning("%s is set but ignored while dev-mode is enabled.", _API_KEY_ENV)


def _get_jwks_client() -> object:
    """PyJWKClient 를 지연 생성·캐시한다 (thread-safe)."""
    global _jwks_client, _jwks_url_cached
    with _jwks_lock:
        url = _oidc_jwks_url()
        if _jwks_client is None or _jwks_url_cached != url:
            from jwt import PyJWKClient

            ttl = int(os.environ.get(_OIDC_JWKS_TTL_ENV, "") or _DEFAULT_JWKS_TTL_SECONDS)
            _jwks_client = PyJWKClient(url, cache_jwk_set=True, lifespan=ttl)
            _jwks_url_cached = url
    return _jwks_client


#: PyJWT 가 "JWKS 에 이 kid 가 없다" 를 알릴 때 쓰는 문구. 예외 타입이 하나뿐이라
#: 메시지로 가를 수밖에 없다 — 못 알아보면 기존처럼 503 이므로 안전한 쪽으로 진다.
_UNKNOWN_KEY_MARKERS = ("unable to find a signing key", "no matching key")


def _is_unknown_signing_key(exc: Exception) -> bool:
    """JWKS 는 받았는데 그 안에 이 토큰의 kid 가 없는 경우인지 판정한다."""
    message = str(exc).casefold()
    return any(marker in message for marker in _UNKNOWN_KEY_MARKERS)


def _verify_bearer_token(token: str) -> Principal | AuthError:
    """Google ID token을 JWKS로 오프라인 검증한다 (#385, ADR 0009).

    - RS256 고정 (alg:none / HS* 거부).
    - iss/aud/exp/nbf/iat 검증(60s leeway), email_verified 강제.
    - JWKS 조회 실패 → 503(401이 아님, 일시적 인프라 장애).
    """
    import jwt
    from jwt import PyJWKClientError

    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)  # type: ignore[attr-defined]
    except PyJWKClientError as exc:
        # "서명 키를 못 찾았다" 와 "JWKS 에 닿지 못했다" 는 전혀 다른 사건인데
        # 하나로 묶여 있었다. 전자를 503 으로 돌려주면 (a) 스로틀이 그것을 세지
        # 않고 — 503 은 클라이언트 잘못이 아니므로 일부러 제외한다 — (b)
        # PyJWKClient 가 캐시 미스마다 JWKS 를 새로 받으므로, 임의의 kid 를 단
        # 토큰을 반복해 보내면 요청마다 IdP 아웃바운드 한 건이 나간다.
        # 알 수 없는 kid 는 그냥 잘못된 자격증명이다.
        if _is_unknown_signing_key(exc):
            return AuthError(reason="invalid token")
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)
    except (ConnectionError, OSError):
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)
    except jwt.PyJWTError:
        # PyJWKClient parses the unverified JWT header before selecting a key.
        # Malformed compact serialization/header errors are invalid credentials,
        # not JWKS infrastructure failures.
        return AuthError(reason="invalid token")

    audience = os.environ.get(_OIDC_AUDIENCE_ENV, "").strip()
    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=_oidc_issuers(),
            leeway=_TOKEN_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        return AuthError(reason=f"invalid token: {type(exc).__name__}")

    if not payload.get("email_verified", False):
        return AuthError(reason="email not verified")

    # 허용 목록 검사 (Google 공개 IdP, #386). 설정된 목록 중 하나라도 매칭되면 통과.
    hd_set, sub_set, email_set = _oidc_allowlists()
    if hd_set or sub_set or email_set:
        matched = (
            (bool(hd_set) and str(payload.get("hd", "")) in hd_set)
            or (bool(sub_set) and str(payload.get("sub", "")) in sub_set)
            or (bool(email_set) and str(payload.get("email", "")) in email_set)
        )
        if not matched:
            return AuthError(reason="principal not in allowlist", status_code=403)

    sub = str(payload.get("sub", ""))
    if not sub:
        # jwt.decode의 require=["sub"]는 claim의 "존재"만 강제하고 값이 비어있지
        # 않음을 보장하지 않는다. 빈 sub를 허용하면 서로 다른 토큰이 모두 같은
        # (issuer, "") owner_id로 수렴해 ownership이 섞일 수 있다 (#505).
        return AuthError(reason="missing subject claim")
    issuer = str(payload.get("iss", ""))
    # owner_id는 트렁케이션 없는 전체 issuer+subject를 해시한다(#505) — issuer와
    # sub를 별도 필드로 전달해 구분자 없이도 충돌이 불가능하다(length-prefix
    # framing). 아래 identifier(로그/표시용, sub 앞 8자)와 달리 충돌 방지가
    # 필요한 persistent ownership 판정에 쓰인다.
    owner_id = compute_owner_id("oidc", issuer, sub)
    # 로그 추적용 식별자로 sub 앞 8자만(전체 sub 노출 최소화).
    #
    # 관리자 판정은 **전체 sub** 로 한다. 앞 8자로 비교하면 접두사가 같은 다른
    # 계정이 관리자가 된다.
    return Principal(
        kind="oidc",
        identifier=sub[:8],
        owner_id=owner_id,
        is_admin=sub in _admin_subjects(),
    )


def authenticate(
    *, api_key: str | None = None, bearer_token: str | None = None
) -> Principal | AuthError:
    """요청을 인증해 ``Principal`` 또는 ``AuthError`` 를 반환한다 (B2/B3).

    우선순위:
    - dev-mode → ``Principal(kind='dev')`` (로컬 개발 편의).
    - Bearer 토큰 + OIDC 활성 → Bearer 검증 (사람 사용자).
    - 그 외 → X-API-Key 검증 (서비스 계정).

    OIDC 비활성 시 Bearer는 무시되어 기존 배포에 영향이 없다.
    """
    if _is_dev_mode():
        # dev principal owner_id는 실행마다 바뀌지 않는 고정 local 식별자다(#505) —
        # OIDC principal의 owner_id는 항상 "oidc:" 로 시작해 namespace가 겹치지 않는다.
        return Principal(kind="dev", owner_id=compute_owner_id("dev", "local"), is_admin=True)

    if bearer_token and _oidc_issuers():
        if bearer_token.lower().startswith("bearer "):
            return _verify_bearer_token(bearer_token[7:].strip())
        return AuthError(reason="malformed authorization header")

    return _verify_api_key(api_key)


__all__ = [
    "AuthError",
    "Principal",
    "authenticate",
    "compute_owner_id",
    "principal_owns",
    "validate_dev_mode",
    "validate_oidc_config",
]

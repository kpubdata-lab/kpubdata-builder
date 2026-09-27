"""OIDC Bearer 인증 테스트 (#385, B3).

로컬 RSA 키쌍으로 서명한 ID token을 검증한다 — 외부 IdP에 의존하지 않는다.
JWKS 조회는 _get_jwks_client를 mock해 네트워크 없이 검증 로직만 테스트한다.
"""

from __future__ import annotations

import logging
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kpubdata_builder.service.auth import (
    AuthError,
    Principal,
    authenticate,
    compute_owner_id,
    principal_owns,
    validate_dev_mode,
    validate_oidc_config,
)

_ISSUER = "https://accounts.google.com"
_AUDIENCE = "builder-test-client"


@pytest.fixture(autouse=True)
def _clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """각 테스트 전에 인증 관련 환경변수를 초기화한다 — dev-mode 등 누출 차단."""
    for key in (
        "KPUBDATA_BUILDER_DEV_MODE",
        "KPUBDATA_BUILDER_API_KEY",
        "KPUBDATA_BUILDER_ADMIN_SUBJECTS",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_JWKS_URL",
        "OIDC_JWKS_TTL",
        "OIDC_LEGACY_REQUIRE_ALLOWLIST",
    ):
        monkeypatch.delenv(key, raising=False)


class _FakeSigningKey:
    """PyJWKClient.get_signing_key_from_jwt의 반환을 흉내낸다."""

    def __init__(self, key: object) -> None:
        self.key = key


class _FakeJWKSClient:
    """JWKS 조회 mock — 네트워크 없이 public key를 반환한다."""

    def __init__(self, public_key: object) -> None:
        self._pubkey = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._pubkey)


class _FailingJWKSClient:
    """JWKS 조회 실패를 시뮬레이션 (503 검증용)."""

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        raise ConnectionError("jwks endpoint unreachable")


class _ParsingJWKSClient(_FakeJWKSClient):
    """PyJWKClient처럼 키 선택 전에 JWT header를 파싱한다."""

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        jwt.get_unverified_header(token)
        return super().get_signing_key_from_jwt(token)


@pytest.fixture()
def rsa_keypair() -> tuple[bytes, object]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return private_pem, private_key.public_key()


@pytest.fixture()
def oidc_env(monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[bytes, object]) -> bytes:
    """OIDC 활성 + JWKS mock. 토큰 서명용 private PEM을 반환한다."""
    private_pem, public_key = rsa_keypair
    monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
    import kpubdata_builder.service.auth as auth_module

    monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FakeJWKSClient(public_key))
    return private_pem


def _make_token(private_pem: bytes, **overrides: object) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": "user-1234567890",
        "email": "user@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": "test-key"})


class TestValidBearerToken:
    def test_valid_token_returns_oidc_principal(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"
        # 식별자는 sub 앞 8자
        assert result.identifier == "user-123"

    def test_case_insensitive_bearer_prefix(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"


class TestAdminRole:
    """OIDC principal 의 관리자 역할 (#679).

    예전에는 ``kind in ("dev", "service")`` 가 곧 관리자였다 — 즉 **관리자가
    되는 유일한 방법이 OIDC 로 로그인하지 않는 것**이었다.
    """

    def test_oidc_principal_is_not_admin_by_default(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.is_admin is False

    def test_listed_subject_becomes_admin(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", "user-1234567890")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.is_admin is True

    def test_unlisted_subject_stays_non_admin(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", "someone-else")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.is_admin is False

    def test_match_uses_the_full_subject_not_the_truncated_identifier(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``identifier`` 는 sub 앞 8자다. 그것으로 비교하면 접두사가 같은 다른
        계정이 관리자가 된다 — 이 토큰의 sub 는 ``user-1234567890`` 이고
        identifier 는 ``user-123`` 이다."""
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", "user-123")
        result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env)}")
        assert isinstance(result, Principal)
        assert result.identifier == "user-123"
        assert result.is_admin is False

    def test_admin_list_accepts_comma_and_space_separators(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "KPUBDATA_BUILDER_ADMIN_SUBJECTS", "first-admin, user-1234567890 second-admin"
        )
        result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env)}")
        assert isinstance(result, Principal)
        assert result.is_admin is True

    def test_dev_principal_is_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        result = authenticate()
        assert isinstance(result, Principal)
        assert result.kind == "dev"
        assert result.is_admin is True

    def test_service_principal_is_admin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret-key")
        result = authenticate(api_key="secret-key")
        assert isinstance(result, Principal)
        assert result.kind == "service"
        assert result.is_admin is True


class TestInvalidTokens:
    @pytest.mark.parametrize(
        "token",
        [
            "invalid-token",
            "one.two",
            "not-base64.payload.signature",
        ],
    )
    def test_malformed_jwt_returns_401(
        self,
        token: str,
        monkeypatch: pytest.MonkeyPatch,
        oidc_env: bytes,
        rsa_keypair: tuple[bytes, object],
    ) -> None:
        import kpubdata_builder.service.auth as auth_module

        _, public_key = rsa_keypair
        monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _ParsingJWKSClient(public_key))

        result = authenticate(bearer_token=f"Bearer {token}")

        assert result == AuthError(reason="invalid token", status_code=401)

    def test_invalid_signature(self, oidc_env: bytes) -> None:
        # 다른 키로 서명 → fixture의 public key로 검증 실패
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        other_pem = other.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        token = _make_token(other_pem)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 401

    def test_expired_token(self, oidc_env: bytes) -> None:
        now = int(time.time())
        token = _make_token(oidc_env, exp=now - 120)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_wrong_audience(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env, aud="wrong-client")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_wrong_issuer(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env, iss="https://evil.example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_email_not_verified(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env, email_verified=False)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert "email" in result.reason

    def test_malformed_authorization_header(self, oidc_env: bytes) -> None:
        result = authenticate(bearer_token="not-a-bearer-scheme")
        assert isinstance(result, AuthError)


class TestJWKSFailure:
    def test_jwks_unavailable_returns_503(
        self, monkeypatch: pytest.MonkeyPatch, oidc_env: bytes
    ) -> None:
        token = _make_token(oidc_env)
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FailingJWKSClient())
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 503


class TestFallbackToApiKey:
    def test_oidc_enabled_falls_back_to_api_key_when_no_bearer(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        result = authenticate(api_key="secret")
        assert isinstance(result, Principal)
        assert result.kind == "service"

    def test_oidc_disabled_ignores_bearer(
        self, monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[bytes, object]
    ) -> None:
        # OIDC_ISSUER 미설정 → Bearer 무시, API key 경로로
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        result = authenticate(api_key="secret", bearer_token="Bearer some.jwt.token")
        assert isinstance(result, Principal)
        assert result.kind == "service"

    def test_dev_mode_short_circuits_bearer(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        result = authenticate(bearer_token="Bearer anything")
        assert isinstance(result, Principal)
        assert result.kind == "dev"


class TestValidateOidcConfig:
    def test_no_op_when_oidc_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        validate_oidc_config()  # 예외 없음

    def test_rejects_when_audience_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_AUDIENCE"):
            validate_oidc_config()

    def test_accepts_no_allowlist_for_valid_oidc_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 공개 가입이 기본 정책이다 — 허용 목록이 없어도 기동한다.
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        validate_oidc_config()

    def test_require_allowlist_switch_rejects_empty_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 제한 배포는 이 스위치로 허용 목록 누락을 기동 실패로 잡는다.
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_LEGACY_REQUIRE_ALLOWLIST", "true")
        with pytest.raises(RuntimeError, match="OIDC_LEGACY_REQUIRE_ALLOWLIST"):
            validate_oidc_config()

    def test_require_allowlist_switch_accepts_configured_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_LEGACY_REQUIRE_ALLOWLIST", "true")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "person@example.com")
        validate_oidc_config()


class TestValidateDevMode:
    """dev-mode 기동 가드 — 인증을 통째로 끄는 플래그가 배포로 새지 않게 한다."""

    def test_no_op_when_dev_mode_disabled(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        with caplog.at_level(logging.WARNING):
            validate_dev_mode()
        assert caplog.records == []

    def test_warns_that_every_request_is_unauthenticated(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        with caplog.at_level(logging.WARNING):
            validate_dev_mode()
        assert any("without authentication" in r.getMessage() for r in caplog.records)

    def test_warns_that_a_configured_api_key_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        with caplog.at_level(logging.WARNING):
            validate_dev_mode()
        assert any("ignored while dev-mode" in r.getMessage() for r in caplog.records)

    def test_refuses_to_start_when_oidc_is_also_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 사용자 인증을 구성해두고 인증을 우회하는 조합 — 배포에 dev 플래그가 남은 전형적 사고.
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        with pytest.raises(RuntimeError, match="KPUBDATA_BUILDER_DEV_MODE"):
            validate_dev_mode()


class TestAllowlistGate:
    """허용 목록 게이트 (#386) — 설정된 배포에서만 적용되는 선택적 2차 인가."""

    def test_hd_allowlist_match(self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "example.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"

    def test_hd_allowlist_miss(self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "other.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 403

    def test_subject_allowlist_match(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_SUBJECTS", "user-1234567890")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)

    def test_email_allowlist_match(self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)

    def test_hd_missing_rejected_when_hd_required(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "example.com")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)
        assert result.status_code == 403

    def test_multiple_lists_match_any(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "other.com")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)


class _FakeDiscoveryResp:
    """urllib urlopen 반환 흉내 (context manager + read)."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeDiscoveryResp:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class TestJwksDiscovery:
    """OIDC discovery (RFC 8414) 기반 JWKS URL 해석 (#435).

    기존 ``issuer + /.well-known/jwks.json`` 추정이 Google에서 404 → 503 실패를
    일으킨 문제 수정. discovery 문서의 jwks_uri를 읽고 TTL 캐시한다.
    """

    def _clear_cache(self) -> None:
        import kpubdata_builder.service.auth as auth_module

        auth_module._discovery_cache.clear()

    def test_discover_reads_jwks_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """discovery 문서의 jwks_uri 필드를 반환한다 (Google 경로)."""
        import urllib.request

        import kpubdata_builder.service.auth as auth_module

        self._clear_cache()
        captured: list[str] = []
        doc = b'{"issuer":"https://accounts.google.com","jwks_uri":"https://www.googleapis.com/oauth2/v3/certs"}'

        def _fake_urlopen(url: str, timeout: float) -> _FakeDiscoveryResp:
            captured.append(url)
            return _FakeDiscoveryResp(doc)

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        result = auth_module._discover_jwks_uri("https://accounts.google.com")
        assert result == "https://www.googleapis.com/oauth2/v3/certs"
        assert "accounts.google.com/.well-known/openid-configuration" in captured[0]

    def test_oidc_jwks_url_explicit_bypasses_discovery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OIDC_JWKS_URL 명시 시 discovery를 건너뛴다 (override)."""
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setenv("OIDC_JWKS_URL", "http://explicit/jwks.json")
        assert auth_module._oidc_jwks_url() == "http://explicit/jwks.json"

    def test_discover_caches_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """동일 issuer는 TTL 내 재조회 시 urlopen을 한 번만 부른다."""
        import urllib.request

        import kpubdata_builder.service.auth as auth_module

        self._clear_cache()
        call_count = [0]

        def _fake_urlopen(url: str, timeout: float) -> _FakeDiscoveryResp:
            call_count[0] += 1
            return _FakeDiscoveryResp(b'{"jwks_uri":"https://cached/certs"}')

        monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

        first = auth_module._discover_jwks_uri("https://idp.example.com")
        second = auth_module._discover_jwks_uri("https://idp.example.com")
        assert first == second == "https://cached/certs"
        assert call_count[0] == 1, "캐시 hit면 urlopen을 다시 부르지 않는다"

    def test_discover_raises_when_jwks_uri_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """discovery 문서에 jwks_uri 필드가 없으면 RuntimeError."""
        import urllib.request

        import kpubdata_builder.service.auth as auth_module

        self._clear_cache()
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            lambda url, timeout: _FakeDiscoveryResp(b'{"issuer":"https://idp.example.com"}'),
        )

        with pytest.raises(RuntimeError, match="jwks_uri"):
            auth_module._discover_jwks_uri("https://idp.example.com")

    def test_oidc_jwks_url_raises_when_no_issuer_no_explicit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OIDC_ISSUER 미설정 + OIDC_JWKS_URL 미설정 → RuntimeError (방어)."""
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_ISSUER"):
            auth_module._oidc_jwks_url()


def test_pyjwt_supports_issuer_list() -> None:
    """pyjwt >=2.9만 issuer=list 지원 (#434). auth.py:_verify_bearer_token 참조.

    auth.py 가 ``jwt.decode(..., issuer=_oidc_issuers())`` 로 list를 넘기는데,
    2.8.x 는 ``payload["iss"] != issuer`` 단순 비교라 모든 토큰이 거부된다.
    하한을 ``>=2.9`` 로 올린 것(#434)을 설치 환경에서 재확인한다 (#431 교집합 패턴).
    """
    import jwt

    parts = jwt.__version__.split(".")
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (2, 9), (
        f"pyjwt {jwt.__version__} < 2.9 — issuer list 미지원, auth.py 가 깨짐 (#434)"
    )


class TestStableOwnerId:
    """canonical owner_id 계산 (#505).

    display identity(identifier/label)와 persistent ownership identity(owner_id)를
    분리하고, OIDC subject 트렁케이션/concatenation collision이 owner_id에는
    영향을 주지 않음을 검증한다.
    """

    def test_same_issuer_same_subject_same_owner_id(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        r1 = authenticate(bearer_token=f"Bearer {token}")
        r2 = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(r1, Principal)
        assert isinstance(r2, Principal)
        assert r1.owner_id is not None
        assert r1.owner_id == r2.owner_id

    def test_different_issuer_same_subject_different_owner_id(
        self, monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[bytes, object]
    ) -> None:
        """동일 subject라도 issuer가 다르면 owner_id는 달라야 한다 (#505 완료 조건)."""
        private_pem, public_key = rsa_keypair
        other_issuer = "https://other-issuer.example.com"
        monkeypatch.setenv("OIDC_ISSUER", f"{_ISSUER},{other_issuer}")
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FakeJWKSClient(public_key))

        token_a = _make_token(private_pem, iss=_ISSUER)
        token_b = _make_token(private_pem, iss=other_issuer)
        ra = authenticate(bearer_token=f"Bearer {token_a}")
        rb = authenticate(bearer_token=f"Bearer {token_b}")
        assert isinstance(ra, Principal)
        assert isinstance(rb, Principal)
        # 동일 subject → 표시용 identifier(트렁케이션)는 같지만
        assert ra.identifier == rb.identifier
        # owner_id는 issuer가 다르므로 반드시 달라야 한다.
        assert ra.owner_id != rb.owner_id

    def test_same_issuer_different_subject_different_owner_id(self, oidc_env: bytes) -> None:
        token_a = _make_token(oidc_env, sub="user-aaaaaaaaaa")
        token_b = _make_token(oidc_env, sub="user-bbbbbbbbbb")
        ra = authenticate(bearer_token=f"Bearer {token_a}")
        rb = authenticate(bearer_token=f"Bearer {token_b}")
        assert isinstance(ra, Principal)
        assert isinstance(rb, Principal)
        assert ra.owner_id != rb.owner_id

    def test_concatenation_collision_prevented(
        self, monkeypatch: pytest.MonkeyPatch, rsa_keypair: tuple[bytes, object]
    ) -> None:
        """issuer="ab"+subject="c" 와 issuer="a"+subject="bc" 는 구분자 없이
        이어붙이면 같은 문자열이 되지만, ``\\0`` 구분자 덕분에 owner_id가
        달라야 한다 (#505: prefix/concatenation collision 불가)."""
        private_pem, public_key = rsa_keypair
        monkeypatch.setenv("OIDC_ISSUER", "ab,a")
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FakeJWKSClient(public_key))

        token1 = _make_token(private_pem, iss="ab", sub="c", aud=_AUDIENCE)
        token2 = _make_token(private_pem, iss="a", sub="bc", aud=_AUDIENCE)
        r1 = authenticate(bearer_token=f"Bearer {token1}")
        r2 = authenticate(bearer_token=f"Bearer {token2}")
        assert isinstance(r1, Principal)
        assert isinstance(r2, Principal)
        assert r1.owner_id != r2.owner_id

    def test_owner_id_does_not_contain_raw_subject_or_email(self, oidc_env: bytes) -> None:
        """owner_id/로그에 raw claim을 직접 노출하지 않는다 (#505)."""
        token = _make_token(oidc_env, sub="super-secret-subject-value", email="victim@example.com")
        principal = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(principal, Principal)
        assert principal.owner_id is not None
        assert "super-secret-subject-value" not in principal.owner_id
        assert "victim@example.com" not in principal.owner_id
        # 표시용 identifier는 sub 앞 8자만 담아 로그 노출을 최소화한다 (기존 동작 불변).
        assert principal.identifier == "super-se"

    def test_display_identifier_change_does_not_affect_owner_id_matching(
        self, oidc_env: bytes
    ) -> None:
        """display 라벨(identifier)이 바뀌어도(예: 향후 프로필 이름 갱신) 동일
        owner_id를 가진 principal은 여전히 같은 owner로 판정되어야 한다 (#505)."""
        token = _make_token(oidc_env)
        principal = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(principal, Principal)
        renamed = Principal(
            kind="oidc", identifier="totally-different-label", owner_id=principal.owner_id
        )
        assert principal_owns(created_by=None, owner_id=principal.owner_id, principal=renamed)

    def test_empty_subject_rejected(self, oidc_env: bytes) -> None:
        """빈 sub claim은 거부한다 — 여러 토큰이 같은 (issuer, "") owner_id로
        수렴해 ownership이 섞이는 것을 막는다 (#505, fail-closed)."""
        token = _make_token(oidc_env, sub="")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, AuthError)

    def test_service_owner_id_stable_across_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        r1 = authenticate(api_key="secret")
        r2 = authenticate(api_key="secret")
        assert isinstance(r1, Principal)
        assert isinstance(r2, Principal)
        assert r1.owner_id is not None
        assert r1.owner_id == r2.owner_id

    def test_dev_owner_id_stable_and_namespaced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        r1 = authenticate()
        r2 = authenticate()
        assert isinstance(r1, Principal)
        assert isinstance(r2, Principal)
        assert r1.owner_id is not None
        assert r1.owner_id == r2.owner_id
        assert r1.owner_id.startswith("dev:")

    def test_cross_kind_owner_id_never_collides(self) -> None:
        """dev/service/oidc owner_id는 동일 material이어도 kind로 domain
        separation되어 절대 같아지지 않는다 (#505)."""
        dev_id = compute_owner_id("dev", "local")
        service_id = compute_owner_id("service", "local")
        oidc_id = compute_owner_id("oidc", "local")
        assert len({dev_id, service_id, oidc_id}) == 3

    def test_owner_id_field_boundary_is_unambiguous(self) -> None:
        """필드에 "\\0"이 포함돼도 결합이 모호해지지 않는다 (#505 review).

        구분자 기반 결합이라면 (issuer="a", sub="b\\0c")와 (issuer="a\\0b",
        sub="c")가 같은 material로 수렴해 owner_id가 같아진다 — length-prefix
        framing은 필드 경계를 고정하므로 충돌하지 않는다."""
        assert compute_owner_id("oidc", "a", "b\0c") != compute_owner_id("oidc", "a\0b", "c")


class TestPrincipalOwns:
    """principal_owns() — 모든 ownership consumer가 공유하는 단일 canonical 판정 (#505)."""

    def test_matches_by_owner_id_when_both_present(self) -> None:
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert principal_owns(
            created_by="oidc:mismatched-label", owner_id="oidc:deadbeef", principal=principal
        )

    def test_mismatched_owner_id_denied_even_if_label_matches(self) -> None:
        """label이 같아도(트렁케이션 충돌 등) owner_id가 다르면 거부한다."""
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert not principal_owns(created_by="oidc:a", owner_id="oidc:other", principal=principal)

    def test_legacy_record_falls_back_to_label(self) -> None:
        """owner_id가 없는(#505 이전) 레코드는 created_by/label로 폴백한다."""
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert principal_owns(created_by="oidc:a", owner_id=None, principal=principal)

    def test_legacy_principal_falls_back_to_label(self) -> None:
        """owner_id가 없는 principal(예: 구성 경로)도 label 폴백으로 동작한다."""
        principal = Principal(kind="oidc", identifier="a")
        assert principal_owns(created_by="oidc:a", owner_id="oidc:deadbeef", principal=principal)

    def test_ambiguous_record_with_neither_field_fails_closed(self) -> None:
        """owner_id도 created_by도 없는 레코드는 "누구나 접근 가능"이 아니라 거부한다."""
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert not principal_owns(created_by=None, owner_id=None, principal=principal)

    def test_non_owner_denied_via_legacy_path(self) -> None:
        principal = Principal(kind="oidc", identifier="b", owner_id="oidc:deadbeef")
        assert not principal_owns(created_by="oidc:a", owner_id=None, principal=principal)

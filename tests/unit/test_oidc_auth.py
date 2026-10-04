"""OIDC Bearer authentication test (#385, B3).

Verify ID token signed with local RSA keypair — no external IdP dependency.
JWKS lookup is mocked to test verification logic without network.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

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
    """Initialize auth-related env vars before each test — prevent dev-mode leaks."""
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
    """Mock PyJWKClient.get_signing_key_from_jwt return."""

    def __init__(self, key: object) -> None:
        self.key = key


class _FakeJWKSClient:
    """JWKS lookup mock — return public key without network."""

    def __init__(self, public_key: object) -> None:
        self._pubkey = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._pubkey)


class _FailingJWKSClient:
    """Simulate JWKS lookup failure (for 503 verification)."""

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        raise ConnectionError("jwks endpoint unreachable")


class _ParsingJWKSClient(_FakeJWKSClient):
    """Parse JWT header before key selection, like PyJWKClient."""

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
    """OIDC active + JWKS mock. Return private PEM for token signing."""
    private_pem, public_key = rsa_keypair
    monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
    monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
    # An allowlist is mandatory with OIDC (#635); these are the test tokens' emails.
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com,victim@example.com")
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
        # Identifier is first 8 chars of sub
        assert result.identifier == "user-123"

    def test_case_insensitive_bearer_prefix(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"bearer {token}")
        assert isinstance(result, Principal)
        assert result.kind == "oidc"


class TestAdminRole:
    """OIDC principal admin role (#679).

    Previously ``kind in ("dev", "service")`` meant admin — i.e., the **only way
    to become admin was NOT to log in with OIDC**.
    """

    def test_oidc_principal_is_not_admin_by_default(self, oidc_env: bytes) -> None:
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.is_admin is False

    def test_listed_identity_becomes_admin(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", f"{_ISSUER}|user-1234567890")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.is_admin is True

    def test_unlisted_subject_stays_non_admin(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", f"{_ISSUER}|someone-else")
        token = _make_token(oidc_env)
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)
        assert result.is_admin is False

    def test_the_same_subject_from_another_issuer_is_not_admin(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An OIDC ``sub`` is only unique within its issuer, and OIDC_ISSUER
        accepts a comma-separated list. Comparing the subject alone would let an
        account from issuer B match an entry meant for issuer A -- which is why
        ``owner_id`` binds issuer and subject together."""
        monkeypatch.setenv(
            "KPUBDATA_BUILDER_ADMIN_SUBJECTS", "https://other-idp.example|user-1234567890"
        )
        result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env)}")
        assert isinstance(result, Principal)
        assert result.is_admin is False

    def test_a_bare_subject_entry_grants_nothing(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An entry without an issuer is dropped with a warning, not honoured.
        Granting administrator rights must not happen by typo."""
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", "user-1234567890")
        with caplog.at_level(logging.WARNING):
            result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env)}")
        assert isinstance(result, Principal)
        assert result.is_admin is False
        assert "<issuer>|<subject>" in caplog.text

    def test_match_uses_the_full_subject_not_the_truncated_identifier(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``identifier`` is first 8 chars of sub. Comparing by it means different
        accounts with same prefix become admin — this token's sub is ``user-1234567890`` and
        identifier is ``user-123``."""
        monkeypatch.setenv("KPUBDATA_BUILDER_ADMIN_SUBJECTS", "user-123")
        result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env)}")
        assert isinstance(result, Principal)
        assert result.identifier == "user-123"
        assert result.is_admin is False

    def test_admin_list_accepts_several_comma_separated_identities(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "KPUBDATA_BUILDER_ADMIN_SUBJECTS",
            f"https://other.example|someone, {_ISSUER}|user-1234567890",
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
        # signed with different key → verification fails with fixture's public key
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
        # A code of its own (#1000): the client gets a new token instead of reading the reason.
        assert result.code == "token_expired"

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
        # OIDC_ISSUER not set → ignore Bearer, API key path
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
        validate_oidc_config()  # no exception

    def test_rejects_when_audience_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.delenv("OIDC_AUDIENCE", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_AUDIENCE"):
            validate_oidc_config()

    def test_rejects_oidc_without_an_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Negative (#635): open sign-up is refused at startup, not warned about."""
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        for name in ("OIDC_ALLOWED_HD", "OIDC_ALLOWED_SUBJECTS", "OIDC_ALLOWED_EMAILS"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(RuntimeError, match="no allowlist"):
            validate_oidc_config()

    def test_the_old_opt_in_switch_no_longer_matters(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_LEGACY_REQUIRE_ALLOWLIST", "false")
        with pytest.raises(RuntimeError, match="no allowlist"):
            validate_oidc_config()

    def test_accepts_a_configured_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "person@example.com")
        validate_oidc_config()


class TestValidateDevMode:
    """dev-mode startup guard — prevent auth-off flag from leaking into deploy."""

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
        # Configured auth + auth bypass combo — classic incident of forgotten dev flag in deploy.
        monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
        monkeypatch.setenv("OIDC_ISSUER", _ISSUER)
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        with pytest.raises(RuntimeError, match="KPUBDATA_BUILDER_DEV_MODE"):
            validate_dev_mode()


class TestAllowlistGate:
    """Allowlist gate (#386) — mandatory with OIDC since #635."""

    @pytest.fixture(autouse=True)
    def _only_the_lists_each_test_sets(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OIDC_ALLOWED_EMAILS", raising=False)

    def test_no_allowlist_admits_nobody(self, oidc_env: bytes) -> None:
        """Negative (#635, #785): with no list nobody is admitted by sign-in alone."""
        result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env)}")
        # Not refused at sign-in any more (#785): not admitted, so the sign-up ledger
        # decides — pending until an administrator approves.
        assert isinstance(result, Principal)
        assert result.admitted is False

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
        # Not refused at sign-in any more (#785): not admitted, so the sign-up ledger
        # decides — pending until an administrator approves.
        assert isinstance(result, Principal)
        assert result.admitted is False

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
        # Not refused at sign-in any more (#785): not admitted, so the sign-up ledger
        # decides — pending until an administrator approves.
        assert isinstance(result, Principal)
        assert result.admitted is False

    def test_multiple_lists_match_any(
        self, oidc_env: bytes, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OIDC_ALLOWED_HD", "other.com")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
        token = _make_token(oidc_env, hd="example.com")
        result = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(result, Principal)


class _FakeDiscoveryResp:
    """Mock urllib urlopen return (context manager + read)."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeDiscoveryResp:
        return self

    def __exit__(self, *args: object) -> None:
        pass


class TestJwksDiscovery:
    """OIDC discovery (RFC 8414) based JWKS URL resolution (#435).

    Old assumption ``issuer + /.well-known/jwks.json`` failed 404 → 503 on Google;
    fixed by reading jwks_uri from discovery doc and TTL caching.
    """

    def _clear_cache(self) -> None:
        import kpubdata_builder.service.auth as auth_module

        auth_module._discovery_cache.clear()

    def test_discover_reads_jwks_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Return jwks_uri field from discovery doc (Google path)."""
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
        """When OIDC_JWKS_URL explicit, skip discovery (override)."""
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setenv("OIDC_JWKS_URL", "http://explicit/jwks.json")
        assert auth_module._oidc_jwks_url() == "http://explicit/jwks.json"

    def test_discover_caches_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Same issuer calls urlopen only once within TTL on re-lookup."""
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
        """RuntimeError if discovery doc lacks jwks_uri field."""
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
        """OIDC_ISSUER not set + OIDC_JWKS_URL not set → RuntimeError (defensive)."""
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.delenv("OIDC_ISSUER", raising=False)
        monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
        with pytest.raises(RuntimeError, match="OIDC_ISSUER"):
            auth_module._oidc_jwks_url()


def test_pyjwt_supports_issuer_list() -> None:
    """Only pyjwt >=2.9 supports issuer=list (#434). See auth.py:_verify_bearer_token.

    auth.py passes list to ``jwt.decode(..., issuer=_oidc_issuers())``, but
    2.8.x uses simple ``payload["iss"] != issuer`` comparison rejecting all tokens.
    Verify floor raised to ``>=2.9`` (#434) in installed environment (#431 intersection pattern).
    """
    import jwt

    parts = jwt.__version__.split(".")
    major, minor = int(parts[0]), int(parts[1])
    assert (major, minor) >= (2, 9), (
        f"pyjwt {jwt.__version__} < 2.9 — issuer list 미지원, auth.py 가 깨짐 (#434)"
    )


class TestStableOwnerId:
    """Canonical owner_id calculation (#505).

    Separate display identity (identifier/label) from persistent ownership identity (owner_id),
    and verify OIDC subject truncation/concatenation collision doesn't affect owner_id.

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
        """Same subject but different issuer must have different owner_id (#505)."""
        private_pem, public_key = rsa_keypair
        other_issuer = "https://other-issuer.example.com"
        monkeypatch.setenv("OIDC_ISSUER", f"{_ISSUER},{other_issuer}")
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
        import kpubdata_builder.service.auth as auth_module

        monkeypatch.setattr(auth_module, "_get_jwks_client", lambda: _FakeJWKSClient(public_key))

        token_a = _make_token(private_pem, iss=_ISSUER)
        token_b = _make_token(private_pem, iss=other_issuer)
        ra = authenticate(bearer_token=f"Bearer {token_a}")
        rb = authenticate(bearer_token=f"Bearer {token_b}")
        assert isinstance(ra, Principal)
        assert isinstance(rb, Principal)
        # same subject → display identifier (truncation) is same but
        assert ra.identifier == rb.identifier
        # owner_id must differ because issuer differs.
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
        """issuer="ab"+subject="c" and issuer="a"+subject="bc" concatenate to same string
        without delimiter, but ``\0`` separator prevents collision (#505)."""
        private_pem, public_key = rsa_keypair
        monkeypatch.setenv("OIDC_ISSUER", "ab,a")
        monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
        monkeypatch.setenv("OIDC_JWKS_URL", "http://localhost:0/jwks.json")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
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
        """Don't expose raw claims directly in owner_id/logs (#505)."""
        token = _make_token(oidc_env, sub="super-secret-subject-value", email="victim@example.com")
        principal = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(principal, Principal)
        assert principal.owner_id is not None
        assert "super-secret-subject-value" not in principal.owner_id
        assert "victim@example.com" not in principal.owner_id
        # Display identifier: first 8 chars of sub to minimize log exposure (behavior unchanged).
        assert principal.identifier == "super-se"

    def test_display_identifier_change_does_not_affect_owner_id_matching(
        self, oidc_env: bytes
    ) -> None:
        """Even if display label (identifier) changes (e.g., future profile name update), principal
        with same owner_id must still be judged as same owner (#505)."""
        token = _make_token(oidc_env)
        principal = authenticate(bearer_token=f"Bearer {token}")
        assert isinstance(principal, Principal)
        renamed = Principal(
            kind="oidc", identifier="totally-different-label", owner_id=principal.owner_id
        )
        assert principal_owns(created_by=None, owner_id=principal.owner_id, principal=renamed)

    def test_empty_subject_rejected(self, oidc_env: bytes) -> None:
        """Empty sub claim rejected — multiple tokens converge to same (issuer, "") owner_id,
        mixing ownership (#505, fail-closed)."""
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
        """dev/service/oidc owner_ids differ by kind even with same material
        separation ensures they never coincide (#505)."""
        dev_id = compute_owner_id("dev", "local")
        service_id = compute_owner_id("service", "local")
        oidc_id = compute_owner_id("oidc", "local")
        assert len({dev_id, service_id, oidc_id}) == 3

    def test_owner_id_field_boundary_is_unambiguous(self) -> None:
        """Combination is unambiguous even with "\0" in field (#505 review).

        With separator-based combination, (issuer="a", sub="b\0c") and (issuer="a\0b",
        sub="c") converge to same material making owner_id identical — length-prefix
        framing fixes field boundaries so no collision occurs."""
        assert compute_owner_id("oidc", "a", "b\0c") != compute_owner_id("oidc", "a\0b", "c")


class TestPrincipalOwns:
    """principal_owns() — single canonical judgment shared by all ownership consumers (#505)."""

    def test_matches_by_owner_id_when_both_present(self) -> None:
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert principal_owns(
            created_by="oidc:mismatched-label", owner_id="oidc:deadbeef", principal=principal
        )

    def test_mismatched_owner_id_denied_even_if_label_matches(self) -> None:
        """Even if labels match (truncation collision etc), reject if owner_id differs."""
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert not principal_owns(created_by="oidc:a", owner_id="oidc:other", principal=principal)

    def test_legacy_record_falls_back_to_label(self) -> None:
        """Records without owner_id (#505 pre) fall back to created_by/label."""
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert principal_owns(created_by="oidc:a", owner_id=None, principal=principal)

    def test_legacy_principal_falls_back_to_label(self) -> None:
        """Principals without owner_id (e.g., config path) also work via label fallback."""
        principal = Principal(kind="oidc", identifier="a")
        assert principal_owns(created_by="oidc:a", owner_id="oidc:deadbeef", principal=principal)

    def test_ambiguous_record_with_neither_field_fails_closed(self) -> None:
        """Records with neither owner_id nor created_by are rejected, not publicly accessible."""
        principal = Principal(kind="oidc", identifier="a", owner_id="oidc:deadbeef")
        assert not principal_owns(created_by=None, owner_id=None, principal=principal)

    def test_non_owner_denied_via_legacy_path(self) -> None:
        principal = Principal(kind="oidc", identifier="b", owner_id="oidc:deadbeef")
        assert not principal_owns(created_by="oidc:a", owner_id=None, principal=principal)


class TestStableAuthCodes:
    """An authentication failure says what kind it is without its sentence (#1000)."""

    def test_a_refused_credential_is_unauthorized(self, oidc_env: bytes) -> None:
        result = authenticate(bearer_token=f"Bearer {_make_token(oidc_env, aud='wrong-client')}")

        assert isinstance(result, AuthError)
        assert (result.status_code, result.code) == (401, "unauthorized")

    def test_an_unreachable_jwks_is_auth_unavailable(self) -> None:
        assert AuthError(reason="auth service unavailable (jwks)", status_code=503).code == (
            "auth_unavailable"
        )

    def test_the_response_body_carries_the_code(
        self, oidc_env: bytes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch

        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kw: None)
        expired = _make_token(oidc_env, exp=int(time.time()) - 120)

        response = dispatch(service, "GET", "/datasets", None, bearer_token=f"Bearer {expired}")

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 401
        assert response.body == {
            "error": "invalid token: ExpiredSignatureError",
            "code": "token_expired",
        }

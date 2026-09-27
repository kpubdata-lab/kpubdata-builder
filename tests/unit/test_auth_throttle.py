"""Authentication failure throttle test.

Verify both the sliding window counter itself (AuthFailureThrottle) and
behavior when attached to dispatch auth gate (401 accumulation → 429,
reset on success).
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path

import pytest

from kpubdata_builder.service.app import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth_throttle import AuthFailureThrottle
from kpubdata_builder.service.http import make_handler

from .test_service import _FakeClient

_CLIENT = "203.0.113.7"


class _FakeClock:
    """Test advances time directly — validates window expiration without actual wait."""

    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _throttle(clock: _FakeClock, *, limit: int = 3, window: float = 60.0) -> AuthFailureThrottle:
    return AuthFailureThrottle(limit=limit, window_seconds=window, time_source=clock)


class TestAuthFailureThrottle:
    def test_allows_failures_below_the_limit(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(2):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None

    def test_blocks_once_the_limit_is_reached(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) == 60

    def test_retry_after_shrinks_as_the_window_slides(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        clock.advance(45)
        assert throttle.retry_after(_CLIENT) == 15

    def test_releases_the_client_after_the_window_expires(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        clock.advance(61)
        assert throttle.retry_after(_CLIENT) is None

    def test_successful_authentication_clears_the_failures(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        throttle.record_success(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None

    def test_failures_are_counted_per_client(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(3):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after("198.51.100.4") is None

    def test_unidentifiable_client_is_never_throttled(self) -> None:
        clock = _FakeClock()
        throttle = _throttle(clock)
        for _ in range(5):
            throttle.record_failure(None)
        assert throttle.retry_after(None) is None

    def test_disabled_when_limit_is_not_positive(self) -> None:
        clock = _FakeClock()
        throttle = AuthFailureThrottle(limit=0, window_seconds=60.0, time_source=clock)
        assert not throttle.enabled
        for _ in range(10):
            throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None

    def test_tracked_clients_stay_bounded(self) -> None:
        # Even if failures spread from different IPs, tracking dict must not grow infinitely.
        clock = _FakeClock()
        throttle = AuthFailureThrottle(
            limit=3, window_seconds=60.0, max_clients=8, time_source=clock
        )
        for index in range(200):
            throttle.record_failure(f"198.51.100.{index}")
        assert len(throttle._failures) <= 8

    def test_reads_limit_and_window_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "2")
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS", "30")
        clock = _FakeClock()
        throttle = AuthFailureThrottle(time_source=clock)
        throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) is None
        throttle.record_failure(_CLIENT)
        assert throttle.retry_after(_CLIENT) == 30

    def test_malformed_env_falls_back_to_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Configuration typos must not break service startup.
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "many")
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS", "-5")
        throttle = AuthFailureThrottle(time_source=_FakeClock())
        assert throttle.enabled


class TestDispatchAuthThrottle:
    """Throttle attached to auth gate — check if repeated 401 becomes 429."""

    @pytest.fixture
    def service(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
        # Turn off conftest's dev-mode and run real auth gate.
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "correct-key")
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)
        service._auth_throttle = AuthFailureThrottle(limit=3, window_seconds=60.0)
        return service

    def _version(self, service: BuilderService, *, api_key: str, client_id: str | None) -> int:
        response = dispatch(service, "GET", "/version", None, api_key=api_key, client_id=client_id)
        assert isinstance(response, ServiceResponse)
        if response.status_code == 429:
            assert response.body["code"] == "auth_throttled"
            assert isinstance(response.body["retry_after_seconds"], int)
        return response.status_code

    def test_repeated_bad_credentials_become_429(self, service: BuilderService) -> None:
        for _ in range(3):
            assert self._version(service, api_key="wrong", client_id=_CLIENT) == 401
        assert self._version(service, api_key="wrong", client_id=_CLIENT) == 429
        # Even with correct key, blocked during throttle window — does not consume validation cost.
        assert self._version(service, api_key="correct-key", client_id=_CLIENT) == 429

    def test_other_clients_are_unaffected(self, service: BuilderService) -> None:
        for _ in range(4):
            self._version(service, api_key="wrong", client_id=_CLIENT)
        assert self._version(service, api_key="correct-key", client_id="198.51.100.9") == 200

    def test_success_resets_the_counter(self, service: BuilderService) -> None:
        for _ in range(2):
            assert self._version(service, api_key="wrong", client_id=_CLIENT) == 401
        assert self._version(service, api_key="correct-key", client_id=_CLIENT) == 200
        # Counter is empty, so again has failure allowance up to limit.
        for _ in range(3):
            assert self._version(service, api_key="wrong", client_id=_CLIENT) == 401
        assert self._version(service, api_key="wrong", client_id=_CLIENT) == 429

    def test_healthz_stays_reachable_while_throttled(self, service: BuilderService) -> None:
        for _ in range(4):
            self._version(service, api_key="wrong", client_id=_CLIENT)
        response = dispatch(service, "GET", "/healthz", None, client_id=_CLIENT)
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200


class TestHttpAuthThrottle:
    """Verify HTTP layer passes peer address as throttle identifier — check with real socket."""

    @pytest.fixture
    def server(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[str]:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        # Lower limit before service creation so throttle triggers on two failures.
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "2")
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)
        httpd = HTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1.0)

    def test_repeated_unauthenticated_requests_get_429(self, server: str) -> None:
        def _status(api_key: str) -> tuple[int, dict[str, object]]:
            request = urllib.request.Request(f"{server}/version", headers={"X-API-Key": api_key})
            try:
                with urllib.request.urlopen(request, timeout=2.0) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        assert _status("wrong")[0] == 401
        assert _status("wrong")[0] == 401
        status, body = _status("wrong")
        assert status == 429
        assert body["code"] == "auth_throttled"
        assert isinstance(body["retry_after_seconds"], int)


class TestUnknownSigningKeyIsNotAnOutage:
    """kid not in JWKS is invalid credential, not infrastructure failure.

    Returning 503 breaks both. Throttle doesn't count 503 (sees it as not
    client error), allowing unlimited retries. PyJWKClient fetches JWKS on
    every cache miss, so repeating tokens with arbitrary kid produces
    **one outbound per request to IdP**.
    """

    def test_a_missing_signing_key_is_classified_as_a_bad_token(self) -> None:
        from jwt import PyJWKClientError

        from kpubdata_builder.service.auth import _is_unknown_signing_key

        assert _is_unknown_signing_key(
            PyJWKClientError('Unable to find a signing key that matches: "abc123"')
        )

    def test_an_unrecognised_jwks_failure_stays_an_outage(self) -> None:
        from jwt import PyJWKClientError

        from kpubdata_builder.service.auth import _is_unknown_signing_key

        # If not recognized, 503 as before — err on safe side.
        assert not _is_unknown_signing_key(PyJWKClientError("Fail to fetch data from the url"))


class TestApiKeyComparisonAcceptsNonAscii:
    """Non-ASCII API key header must not become 500.

    `hmac.compare_digest` accepts only ASCII with two str args. One
    `X-API-Key: clé` made TypeError into 500, and that path was not recorded
    as auth failure, so throttle passed through.
    """

    def test_a_non_ascii_key_is_an_ordinary_mismatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from kpubdata_builder.service.auth import AuthError, authenticate

        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "correct-key")

        result = authenticate(api_key="clé", bearer_token=None)

        assert isinstance(result, AuthError)
        assert result.status_code == 401

    def test_a_correct_non_ascii_key_still_authenticates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kpubdata_builder.service.auth import Principal, authenticate

        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "clé-secrète")

        assert isinstance(authenticate(api_key="clé-secrète", bearer_token=None), Principal)


class TestOpenSignupWithoutOwnershipWarns:
    """If two defaults overlap, access is effectively unlimited — log at startup."""

    def test_the_combination_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kpubdata_builder.service.auth import validate_oidc_config

        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.setenv("OIDC_AUDIENCE", "client-id")
        for name in ("OIDC_ALLOWED_HD", "OIDC_ALLOWED_SUBJECTS", "OIDC_ALLOWED_EMAILS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)

        with caplog.at_level("WARNING"):
            validate_oidc_config()

        assert any("signup is open" in r.message for r in caplog.records)

    def test_an_allowlist_silences_the_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kpubdata_builder.service.auth import validate_oidc_config

        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.setenv("OIDC_AUDIENCE", "client-id")
        monkeypatch.setenv("OIDC_ALLOWED_HD", "example.com")
        monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)

        with caplog.at_level("WARNING"):
            validate_oidc_config()

        assert not any("signup is open" in r.message for r in caplog.records)

    def test_enforced_ownership_silences_the_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from kpubdata_builder.service.auth import validate_oidc_config

        monkeypatch.setenv("OIDC_ISSUER", "https://accounts.google.com")
        monkeypatch.setenv("OIDC_AUDIENCE", "client-id")
        for name in ("OIDC_ALLOWED_HD", "OIDC_ALLOWED_SUBJECTS", "OIDC_ALLOWED_EMAILS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")

        with caplog.at_level("WARNING"):
            validate_oidc_config()

        assert not any("signup is open" in r.message for r in caplog.records)

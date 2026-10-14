"""Bearer verification while the identity provider is slow, down or answering nonsense (#1323).

A fake identity provider runs on a local port and serves the discovery document and
the keys; each test decides how it misbehaves. Waiting is real but short (a fraction
of a second); the passage of minutes is a fake clock.

The answer a client gets while the provider cannot be reached stays what it was: 503
``auth_unavailable``. Studio keeps the session on a 5xx and signs out on a repeated
401, so an outage must never turn into a 401.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import kpubdata_builder.service.auth as auth_module
from kpubdata_builder.service.auth import AuthError, Principal, authenticate

_AUDIENCE = "builder-test-client"
_UNAVAILABLE = ("auth service unavailable (jwks)", 503, "auth_unavailable")


def _keypair() -> tuple[bytes, rsa.RSAPublicKey]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem, private.public_key()


@dataclass
class _Endpoint:
    """How one endpoint of the fake provider answers, and how often it was asked."""

    #: "ok", an HTTP status code, or a raw body to send with 200.
    answer: str | int | bytes = "ok"
    delay: float = 0.0
    hits: int = 0


@dataclass
class _FakeIdp:
    issuer: str = ""
    public_key: rsa.RSAPublicKey | None = None
    kid: str = "k1"
    discovery: _Endpoint = field(default_factory=_Endpoint)
    jwks: _Endpoint = field(default_factory=_Endpoint)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def body(self, path: str) -> tuple[_Endpoint, bytes]:
        if path.endswith("/.well-known/openid-configuration"):
            doc = {"issuer": self.issuer, "jwks_uri": self.issuer + "/certs"}
            return self.discovery, json.dumps(doc).encode()
        assert self.public_key is not None
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.public_key))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return self.jwks, json.dumps({"keys": [jwk]}).encode()


def _serve(idp: _FakeIdp) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - the stdlib names it
            endpoint, body = idp.body(self.path)
            with idp.lock:
                endpoint.hits += 1
            if endpoint.delay:
                time.sleep(endpoint.delay)
            answer = endpoint.answer
            status = answer if isinstance(answer, int) else 200
            if isinstance(answer, bytes):
                body = answer
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(auth_module, "_clock", fake)
    return fake


@pytest.fixture()
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[_FakeIdp, bytes]]:
    """One configured issuer, served by a fake provider; yields it and its signing key."""
    for key in ("KPUBDATA_BUILDER_DEV_MODE", "KPUBDATA_BUILDER_API_KEY", "OIDC_JWKS_URL"):
        monkeypatch.delenv(key, raising=False)
    pem, public = _keypair()
    fake = _FakeIdp(public_key=public)
    server = _serve(fake)
    fake.issuer = f"http://127.0.0.1:{server.server_address[1]}/realms/test"
    monkeypatch.setenv("OIDC_ISSUER", fake.issuer)
    monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", "user@example.com")
    monkeypatch.setenv("OIDC_JWKS_TTL", "60")
    _reset_caches(monkeypatch)
    yield fake, pem
    server.shutdown()
    server.server_close()


def _reset_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    auth_module._discovery_cache.clear()
    jwks_cache = getattr(auth_module, "_jwks_cache", None)
    if jwks_cache is not None:
        jwks_cache.clear()
    monkeypatch.setattr(auth_module, "_jwks_client", None)
    monkeypatch.setattr(auth_module, "_jwks_url_cached", None)
    monkeypatch.setattr(auth_module, "_issuer_jwks_clients", {})


def _token(pem: bytes, issuer: str, *, kid: str = "k1", **overrides: object) -> str:
    now = int(time.time())
    payload: dict[str, object] = {
        "iss": issuer,
        "aud": _AUDIENCE,
        "sub": "user-1234567890",
        "email": "user@example.com",
        "email_verified": True,
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(overrides)
    return jwt.encode(payload, pem, algorithm="RS256", headers={"kid": kid})


def _summary(result: Principal | AuthError) -> tuple[str, int, str] | str:
    if isinstance(result, Principal):
        return "principal"
    return (result.reason, result.status_code, result.code)


def _concurrently(count: int, call: Callable[[], Principal | AuthError]) -> list[object]:
    results: list[object] = [None] * count
    start = threading.Barrier(count)

    def run(index: int) -> None:
        start.wait()
        results[index] = _summary(call())

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


class TestOneSlowProviderDoesNotQueueEveryRequest:
    """Concurrent requests do not wait one network timeout each, in turn (criterion 1)."""

    _DELAY = 0.4
    _REQUESTS = 6

    def test_discovery_is_asked_once_and_every_request_answers_within_one_delay(
        self, idp: tuple[_FakeIdp, bytes]
    ) -> None:
        fake, pem = idp
        fake.discovery = _Endpoint(answer=503, delay=self._DELAY)
        token = _token(pem, fake.issuer)

        began = time.monotonic()
        results = _concurrently(
            self._REQUESTS, lambda: authenticate(bearer_token=f"Bearer {token}")
        )
        elapsed = time.monotonic() - began

        assert results == [_UNAVAILABLE] * self._REQUESTS
        assert fake.discovery.hits == 1
        # Serialised, six requests took six delays (2.4 s); in one flight, about one.
        assert elapsed < 3 * self._DELAY

    def test_the_keys_are_fetched_once_for_a_burst(self, idp: tuple[_FakeIdp, bytes]) -> None:
        fake, pem = idp
        fake.jwks = _Endpoint(answer=500, delay=self._DELAY)
        token = _token(pem, fake.issuer)

        began = time.monotonic()
        results = _concurrently(
            self._REQUESTS, lambda: authenticate(bearer_token=f"Bearer {token}")
        )
        elapsed = time.monotonic() - began

        assert results == [_UNAVAILABLE] * self._REQUESTS
        assert fake.jwks.hits == 1
        assert elapsed < 3 * self._DELAY

    def test_a_request_with_keys_in_hand_does_not_wait_for_a_refresh(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        fake, pem = idp
        token = _token(pem, fake.issuer)
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == "principal"
        fake.discovery = _Endpoint(delay=self._DELAY)
        fake.jwks = _Endpoint(delay=self._DELAY)
        clock.now += 61  # past OIDC_JWKS_TTL

        began = time.monotonic()
        results = _concurrently(
            self._REQUESTS, lambda: authenticate(bearer_token=f"Bearer {token}")
        )
        elapsed = time.monotonic() - began

        assert results == ["principal"] * self._REQUESTS
        assert fake.discovery.hits == 1
        assert fake.jwks.hits <= 1
        assert elapsed < 3 * self._DELAY


class TestFailuresBackOff:
    """A failure burst asks the provider a bounded number of times, then recovers (criterion 2)."""

    def test_a_failed_discovery_is_not_retried_until_the_backoff_passes(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        fake, pem = idp
        fake.discovery = _Endpoint(answer=500)
        token = _token(pem, fake.issuer)

        for _ in range(20):
            assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE
        assert fake.discovery.hits == 1

        clock.now += auth_module._BACKOFF_INITIAL_SECONDS
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE
        assert fake.discovery.hits == 2

        # The wait doubles: one initial interval later the provider is not asked again.
        clock.now += auth_module._BACKOFF_INITIAL_SECONDS
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE
        assert fake.discovery.hits == 2

    def test_the_backoff_is_bounded_and_the_service_recovers(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        fake, pem = idp
        fake.discovery = _Endpoint(answer=500)
        token = _token(pem, fake.issuer)

        # Many failures in a row: every attempt waits at most the ceiling.
        for attempt in range(1, 12):
            assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE
            assert fake.discovery.hits == attempt
            clock.now += auth_module._BACKOFF_MAX_SECONDS

        fake.discovery = _Endpoint()
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == "principal"

    def test_the_keys_back_off_too(self, idp: tuple[_FakeIdp, bytes], clock: _Clock) -> None:
        fake, pem = idp
        fake.jwks = _Endpoint(answer=502)
        token = _token(pem, fake.issuer)

        for _ in range(10):
            assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE
        assert fake.jwks.hits == 1

        fake.jwks = _Endpoint()
        clock.now += auth_module._BACKOFF_INITIAL_SECONDS
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == "principal"


class TestLastGoodAnswer:
    def test_known_keys_keep_verifying_while_the_provider_is_down(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        fake, pem = idp
        token = _token(pem, fake.issuer)
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == "principal"

        fake.discovery = _Endpoint(answer=503)
        fake.jwks = _Endpoint(answer=503)
        clock.now += 61

        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == "principal"

    def test_known_keys_stop_verifying_once_the_grace_is_over(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        """Negative: a key the provider may have revoked is not trusted for ever."""
        fake, pem = idp
        token = _token(pem, fake.issuer)
        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == "principal"

        fake.jwks = _Endpoint(answer=503)
        clock.now += 61 + auth_module._JWKS_STALE_GRACE_SECONDS + 1

        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE


class TestUnknownKeyId:
    """An unknown ``kid`` is a bad credential, and asks the provider at most once a cooldown."""

    def test_a_made_up_kid_is_refused_without_a_fetch_per_request(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        fake, pem = idp
        assert _summary(authenticate(bearer_token=f"Bearer {_token(pem, fake.issuer)}")) == (
            "principal"
        )

        for _ in range(10):
            result = authenticate(bearer_token=f"Bearer {_token(pem, fake.issuer, kid='nope')}")
            assert isinstance(result, AuthError)
            assert (result.status_code, result.code) == (401, "unauthorized")
        assert fake.jwks.hits == 1

        clock.now += auth_module._JWKS_REFRESH_COOLDOWN_SECONDS
        authenticate(bearer_token=f"Bearer {_token(pem, fake.issuer, kid='nope')}")
        assert fake.jwks.hits == 2

    def test_a_rotated_key_is_picked_up_after_the_cooldown(
        self, idp: tuple[_FakeIdp, bytes], clock: _Clock
    ) -> None:
        fake, pem = idp
        assert _summary(authenticate(bearer_token=f"Bearer {_token(pem, fake.issuer)}")) == (
            "principal"
        )
        new_pem, new_public = _keypair()
        fake.public_key, fake.kid = new_public, "k2"
        clock.now += auth_module._JWKS_REFRESH_COOLDOWN_SECONDS

        rotated = _token(new_pem, fake.issuer, kid="k2")
        assert _summary(authenticate(bearer_token=f"Bearer {rotated}")) == "principal"


class TestMalformedAnswersAreTemporaryUnavailability:
    """Each failure is the one structured 503, naming no URL, body or exception (criterion 3)."""

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(b"{not json", id="invalid-json"),
            pytest.param(b"[1, 2]", id="not-an-object"),
            pytest.param(b'{"issuer": "x"}', id="no-jwks-uri"),
            pytest.param(b'{"jwks_uri": 7}', id="jwks-uri-not-a-string"),
            pytest.param(b'{"jwks_uri": "file:///etc/passwd"}', id="jwks-uri-not-http"),
            pytest.param(404, id="http-404"),
            pytest.param(500, id="http-500"),
        ],
    )
    def test_a_bad_discovery_answer(self, idp: tuple[_FakeIdp, bytes], answer: bytes | int) -> None:
        fake, pem = idp
        fake.discovery = _Endpoint(answer=answer)
        token = _token(pem, fake.issuer)

        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(b"{not json", id="invalid-json"),
            pytest.param(b"[1, 2]", id="not-an-object"),
            pytest.param(b'{"keys": []}', id="no-keys"),
            pytest.param(b'{"keys": [{"kty": "nonsense"}]}', id="no-usable-keys"),
            pytest.param(403, id="http-403"),
            pytest.param(503, id="http-503"),
        ],
    )
    def test_a_bad_keys_answer(self, idp: tuple[_FakeIdp, bytes], answer: bytes | int) -> None:
        fake, pem = idp
        fake.jwks = _Endpoint(answer=answer)
        token = _token(pem, fake.issuer)

        assert _summary(authenticate(bearer_token=f"Bearer {token}")) == _UNAVAILABLE

    def test_a_timeout(self, idp: tuple[_FakeIdp, bytes], monkeypatch: pytest.MonkeyPatch) -> None:
        fake, pem = idp
        monkeypatch.setattr(auth_module, "_IDP_TIMEOUT_SECONDS", 0.2)
        fake.discovery = _Endpoint(delay=0.6)
        token = _token(pem, fake.issuer)

        began = time.monotonic()
        result = authenticate(bearer_token=f"Bearer {token}")

        assert _summary(result) == _UNAVAILABLE
        assert time.monotonic() - began < 0.6

    def test_an_https_issuer_whose_keys_are_plain_http_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovery document may not downgrade the key fetch from TLS to plain HTTP."""
        import urllib.request

        auth_module._discovery_cache.clear()

        class _Response:
            def read(self) -> bytes:
                return b'{"jwks_uri": "http://idp.example.com/certs"}'

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *args: object) -> None:
                pass

        monkeypatch.setattr(urllib.request, "urlopen", lambda url, timeout: _Response())

        with pytest.raises(RuntimeError) as raised:
            auth_module._discover_jwks_uri("https://idp.example.com")
        assert isinstance(raised.value, auth_module._IdpUnavailableError)

    def test_the_response_body_names_nothing_of_the_provider(
        self, idp: tuple[_FakeIdp, bytes], tmp_path: Path
    ) -> None:
        from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch

        fake, pem = idp
        fake.discovery = _Endpoint(answer=b"{not json")
        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kw: None)

        response = dispatch(
            service, "GET", "/datasets", None, bearer_token=f"Bearer {_token(pem, fake.issuer)}"
        )

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 503
        assert response.body == {
            "error": "auth service unavailable (jwks)",
            "code": "auth_unavailable",
        }


class TestIssuersStayApart:
    """Keys held for one issuer never verify a token claiming another (criterion 4)."""

    def test_a_token_claiming_an_unreachable_issuer_is_not_verified_by_anothers_keys(
        self, idp: tuple[_FakeIdp, bytes], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake, pem = idp
        second = "http://127.0.0.1:1/realms/other"  # nothing listens there
        monkeypatch.setenv("OIDC_ISSUER", f"{fake.issuer},{second}")
        monkeypatch.setenv("OIDC_ALLOWED_EMAILS", f"{fake.issuer}|user@example.com")
        assert _summary(authenticate(bearer_token=f"Bearer {_token(pem, fake.issuer)}")) == (
            "principal"
        )

        # The first issuer's key, now cached, signs a token that claims the second.
        forged = _token(pem, second)
        assert _summary(authenticate(bearer_token=f"Bearer {forged}")) == _UNAVAILABLE

    def test_a_token_claiming_another_reachable_issuer_needs_that_issuers_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in ("KPUBDATA_BUILDER_DEV_MODE", "KPUBDATA_BUILDER_API_KEY", "OIDC_JWKS_URL"):
            monkeypatch.delenv(key, raising=False)
        first_pem, first_public = _keypair()
        _second_pem, second_public = _keypair()
        first, second = _FakeIdp(public_key=first_public), _FakeIdp(public_key=second_public)
        servers = [_serve(first), _serve(second)]
        try:
            first.issuer = f"http://127.0.0.1:{servers[0].server_address[1]}/r"
            second.issuer = f"http://127.0.0.1:{servers[1].server_address[1]}/r"
            monkeypatch.setenv("OIDC_ISSUER", f"{first.issuer},{second.issuer}")
            monkeypatch.setenv("OIDC_AUDIENCE", _AUDIENCE)
            monkeypatch.setenv(
                "OIDC_ALLOWED_EMAILS",
                f"{first.issuer}|user@example.com,{second.issuer}|user@example.com",
            )
            _reset_caches(monkeypatch)
            ok = _token(first_pem, first.issuer)
            assert _summary(authenticate(bearer_token=f"Bearer {ok}")) == "principal"

            forged = _token(first_pem, second.issuer)
            result = authenticate(bearer_token=f"Bearer {forged}")

            assert isinstance(result, AuthError)
            assert (result.status_code, result.code) == (401, "unauthorized")
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()

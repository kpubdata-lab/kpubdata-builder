"""The failure throttle behind a reverse proxy, and expired tokens (#1031).

The throttle keyed every request on the TCP peer. Behind the proxy the compose ships,
that is the proxy's address for every user, so they shared one bucket; and an expired
token — verified, only old — counted as a failure. A few users whose tokens had expired
could lock everyone out.
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

from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.app import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import AuthError
from kpubdata_builder.service.auth_throttle import (
    TRUSTED_PROXIES_ENV,
    AuthFailureThrottle,
    client_identity,
    parse_trusted_proxies,
)
from kpubdata_builder.service.http import make_handler

from .test_service import _CLIENT_TIMEOUT, _FakeClient

_PROXY = "172.18.0.2"
_TRUSTED = parse_trusted_proxies("172.18.0.0/16")


class TestClientIdentity:
    def test_without_a_trusted_proxy_the_header_is_never_read(self) -> None:
        assert client_identity(_PROXY, ["203.0.113.1"], ()) == _PROXY

    def test_a_peer_that_is_not_the_proxy_cannot_name_itself(self) -> None:
        # Someone reaching Builder directly sends whatever header they like.
        assert client_identity("198.51.100.9", ["203.0.113.1"], _TRUSTED) == "198.51.100.9"

    def test_the_proxy_names_the_client(self) -> None:
        assert client_identity(_PROXY, ["203.0.113.1"], _TRUSTED) == "203.0.113.1"

    def test_what_the_client_put_in_the_header_is_not_reached(self) -> None:
        # The client sent "10.9.9.9"; the proxy appended the address it saw.
        assert client_identity(_PROXY, ["10.9.9.9, 203.0.113.1"], _TRUSTED) == "203.0.113.1"
        assert client_identity(_PROXY, ["10.9.9.9", "203.0.113.1"], _TRUSTED) == "203.0.113.1"

    def test_a_chain_of_trusted_proxies_is_skipped_from_the_right(self) -> None:
        assert client_identity(_PROXY, ["203.0.113.1, 172.18.0.7"], _TRUSTED) == "203.0.113.1"

    @pytest.mark.parametrize("header", [[], [""], ["172.18.0.7"], ["203.0.113.1, not-an-address"]])
    def test_a_header_that_names_no_client_leaves_the_peer(self, header: list[str]) -> None:
        assert client_identity(_PROXY, header, _TRUSTED) == _PROXY

    def test_an_ipv4_mapped_peer_is_matched_as_ipv4(self) -> None:
        assert client_identity(f"::ffff:{_PROXY}", ["203.0.113.1"], _TRUSTED) == "203.0.113.1"

    def test_no_peer_stays_unidentified(self) -> None:
        assert client_identity(None, ["203.0.113.1"], _TRUSTED) is None


class TestTrustedProxySetting:
    def test_addresses_and_blocks_are_read(self) -> None:
        networks = parse_trusted_proxies(" 172.18.0.2 , 10.0.0.0/8,::1 ")
        assert [str(network) for network in networks] == ["172.18.0.2/32", "10.0.0.0/8", "::1/128"]

    def test_an_entry_that_is_not_an_address_is_dropped_and_trusts_nothing(self) -> None:
        assert parse_trusted_proxies("caddy, 172.18.0.2") == parse_trusted_proxies("172.18.0.2")
        assert parse_trusted_proxies("*") == ()

    def test_a_dropped_entry_is_named_by_position_not_by_its_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING"):
            parse_trusted_proxies("172.18.0.2, not-an-address-CANARY")

        assert "entry 2" in caplog.text
        assert "CANARY" not in caplog.text

    def test_the_throttle_reads_it_from_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert AuthFailureThrottle().client_id(_PROXY, ["203.0.113.1"]) == _PROXY
        monkeypatch.setenv(TRUSTED_PROXIES_ENV, "172.18.0.0/16")
        assert AuthFailureThrottle().client_id(_PROXY, ["203.0.113.1"]) == "203.0.113.1"


def _status(server: str, *, forwarded_for: str | None) -> int:
    headers = {"X-API-Key": "wrong"}
    if forwarded_for is not None:
        headers["X-Forwarded-For"] = forwarded_for
    request = urllib.request.Request(f"{server}/version", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=_CLIENT_TIMEOUT) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        json.loads(exc.read())
        return exc.code


class TestBehindAProxy:
    """A real socket: the test client is the "proxy" (peer 127.0.0.1)."""

    @pytest.fixture
    def serve(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
        monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "2")
        self._servers: list[tuple[HTTPServer, threading.Thread]] = []
        self._tmp_path = tmp_path
        yield
        for httpd, thread in self._servers:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=1.0)

    def _start(self) -> str:
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(
            output_root=self._tmp_path, client_factory=lambda **_kwargs: client
        )
        httpd = HTTPServer(("127.0.0.1", 0), make_handler(service))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self._servers.append((httpd, thread))
        return f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"

    def test_two_clients_behind_one_configured_proxy_are_throttled_separately(
        self, serve: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRUSTED_PROXIES_ENV, "127.0.0.1")
        server = self._start()

        assert _status(server, forwarded_for="203.0.113.1") == 401
        assert _status(server, forwarded_for="203.0.113.1") == 401
        assert _status(server, forwarded_for="203.0.113.1") == 429
        # The other user, through the same proxy, has their own allowance.
        assert _status(server, forwarded_for="203.0.113.2") == 401

    def test_a_forged_prefix_does_not_buy_a_new_bucket(
        self, serve: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(TRUSTED_PROXIES_ENV, "127.0.0.1")
        server = self._start()

        assert _status(server, forwarded_for="10.0.0.1, 203.0.113.1") == 401
        assert _status(server, forwarded_for="10.0.0.2, 203.0.113.1") == 401
        assert _status(server, forwarded_for="10.0.0.3, 203.0.113.1") == 429

    def test_without_the_setting_the_header_changes_nothing(self, serve: None) -> None:
        server = self._start()

        assert _status(server, forwarded_for="203.0.113.1") == 401
        assert _status(server, forwarded_for="203.0.113.2") == 401
        # Every request is the peer's, whatever the header says — as before.
        assert _status(server, forwarded_for="203.0.113.3") == 429
        assert _status(server, forwarded_for=None) == 429


class TestExpiredTokensAreNotFailures:
    @pytest.fixture
    def service(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
        monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: client)
        service._auth_throttle = AuthFailureThrottle(limit=3, window_seconds=60.0)
        return service

    def _version(self, service: BuilderService) -> ServiceResponse:
        response = dispatch(service, "GET", "/version", None, client_id="203.0.113.7")
        assert isinstance(response, ServiceResponse)
        return response

    def test_an_expired_token_does_not_move_the_failure_count(
        self, service: BuilderService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        expired = AuthError(reason="invalid token: ExpiredSignatureError", expired=True)
        monkeypatch.setattr(app_module, "authenticate", lambda **_: expired)

        for _ in range(10):
            response = self._version(service)
            assert response.status_code == 401
            assert response.body["code"] == "token_expired"
        assert service._auth_throttle.retry_after("203.0.113.7") is None

    def test_a_token_that_does_not_verify_still_counts(
        self, service: BuilderService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        forged = AuthError(reason="invalid token: InvalidSignatureError")
        monkeypatch.setattr(app_module, "authenticate", lambda **_: forged)

        assert [self._version(service).status_code for _ in range(4)] == [401, 401, 401, 429]

    def test_expired_tokens_do_not_hide_guesses_made_alongside_them(
        self, service: BuilderService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        answers = [
            AuthError(reason="invalid api key"),
            AuthError(reason="invalid token: ExpiredSignatureError", expired=True),
            AuthError(reason="invalid api key"),
            AuthError(reason="invalid token: ExpiredSignatureError", expired=True),
            AuthError(reason="invalid api key"),
        ]
        monkeypatch.setattr(app_module, "authenticate", lambda **_: answers.pop(0))

        assert [self._version(service).status_code for _ in range(5)] == [401] * 5
        # Three real failures reached the limit; the expired ones neither added nor cleared.
        assert self._version(service).status_code == 429

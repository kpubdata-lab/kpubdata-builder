"""ingestion.url_fetch: url source SSRF-safe fetch  (#498).

``safe_fetch_get`` hostname  DNS resolve (global-routable) IP
,  IP  TLS .      .

    -   (``_validate_url_shape``/``_resolve_and_validate``) IP
       hostname       (IP
       DNS   ).
    -  fetch (redirect ,  ,   )
      ``http.server``  ``_resolve_and_validate``/``_PinnedHTTPSConnection``
       seam     monkeypatch  —  SSRF
       (redirect-loop,  )  .
"""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.url_fetch import (
    _PinnedHTTPSConnection,
    _read_bounded,
    _resolve_and_validate,
    _validate_url_shape,
    safe_fetch_get,
)

# ---    ( ) --------------------------------------------


@pytest.mark.parametrize("scheme_url", ["http://example.org/data", "ftp://example.org/data"])
def test_validate_url_shape_rejects_non_https_scheme(scheme_url: str) -> None:
    with pytest.raises(IngestionError, match="https"):
        _validate_url_shape(scheme_url)


def test_validate_url_shape_rejects_file_scheme() -> None:
    with pytest.raises(IngestionError, match="https"):
        _validate_url_shape("file:///etc/passwd")


def test_validate_url_shape_rejects_userinfo() -> None:
    with pytest.raises(IngestionError, match="userinfo"):
        _validate_url_shape("https://user:pass@example.org/data")


def test_validate_url_shape_rejects_missing_host() -> None:
    with pytest.raises(IngestionError, match="host"):
        _validate_url_shape("https:///data")


def test_validate_url_shape_accepts_valid_https_url() -> None:
    scheme, host, port, path = _validate_url_shape("https://example.org:8443/data?x=1")

    assert scheme == "https"
    assert host == "example.org"
    assert port == 8443
    assert path == "/data?x=1"


def test_validate_url_shape_defaults_to_port_443() -> None:
    _scheme, _host, port, path = _validate_url_shape("https://example.org/data")

    assert port == 443
    assert path == "/data"


@pytest.mark.parametrize(
    "loopback_or_private_ip",
    [
        "127.0.0.1",  # loopback
        "10.0.0.1",  # private
        "172.16.0.1",  # private
        "192.168.1.1",  # private
        "169.254.169.254",  # link-local (cloud metadata endpoint)
        "0.0.0.0",  # unspecified
        "::1",  # IPv6 loopback
    ],
)
def test_resolve_and_validate_rejects_non_public_ip_literal(loopback_or_private_ip: str) -> None:
    # IP   DNS   (getaddrinfo   )
    #      .
    with pytest.raises(IngestionError, match="SSRF policy"):
        _resolve_and_validate(loopback_or_private_ip, 443)


def test_resolve_and_validate_accepts_public_ip_literal() -> None:
    resolved = _resolve_and_validate("8.8.8.8", 443)

    assert resolved == "8.8.8.8"


def test_resolve_and_validate_raises_for_unresolvable_host() -> None:
    with pytest.raises(IngestionError, match="failed to resolve host"):
        _resolve_and_validate("this-host-does-not-resolve.invalid", 443)


# ---    --------------------------------------------------------------


class _FakeHTTPResponse:
    """``http.client.HTTPResponse``  read()    ."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_read_bounded_returns_full_content_under_limit() -> None:
    response = _FakeHTTPResponse([b"hello", b" ", b"world", b""])

    result = _read_bounded(response, max_bytes=100)  # type: ignore[arg-type]

    assert result == b"hello world"


def test_read_bounded_rejects_content_over_limit() -> None:
    response = _FakeHTTPResponse([b"x" * 10, b"y" * 10, b""])

    with pytest.raises(IngestionError, match="exceeds max size"):
        _read_bounded(response, max_bytes=15)  # type: ignore[arg-type]


# ---  fetch  ( HTTP  +  seam ) -------------------------


class _Handler(BaseHTTPRequestHandler):
    #: Header names each request arrived with, in order — one entry per hop.
    seen_headers: list[list[str]] = []

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib
        type(self).seen_headers.append([name.casefold() for name in self.headers])
        if self.path == "/ok":
            body = b'[{"id": 1}]'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-once":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-with-body":
            # redirect(3xx) body    —  body amt
            # read() max_bytes cap  unbounded read
            # BLOCKER(#538 review)   non-empty body.
            body = b"ignored redirect body " * 10
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/redirect-to-private":
            # redirect target private  hop  SSRF
            #   ( , #538 review).
            self.send_response(302)
            self.send_header("Location", "https://10.0.0.1/private")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect-loop":
            self.send_response(302)
            self.send_header("Location", "/redirect-loop")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/big":
            body = b"x" * 1000
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/not-found":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()


@pytest.fixture
def _raw_local_server() -> Iterator[HTTPServer]:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _LoopbackHTTPConnection:
    """``_PinnedHTTPSConnection``    HTTP    test double.

     TLS ,  fixture
     HTTP  — url_fetch   connection
    test double  redirect/ /
        .
    """

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        self._delegate = http.client.HTTPConnection(pinned_ip, port, timeout=connect_timeout)
        self.host = host
        self._read_timeout = read_timeout

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        self._delegate.request(method, path, headers=headers)

    def getresponse(self) -> object:
        return self._delegate.getresponse()

    def close(self) -> None:
        self._delegate.close()


@pytest.fixture
def local_server(
    monkeypatch: pytest.MonkeyPatch, _raw_local_server: HTTPServer
) -> Iterator[HTTPServer]:
    """HTTP  ,  seam      .

     SSRF (scheme/userinfo/redirect-loop / )
    , "hostname →   "  (``_resolve_and_validate``,
    ``_PinnedHTTPSConnection``)  loopback   .
    fixture     —
     .
    """
    import kpubdata_builder.ingestion.url_fetch as url_fetch_module

    port = _raw_local_server.server_address[1]
    monkeypatch.setattr(url_fetch_module, "_resolve_and_validate", lambda host, _port: "127.0.0.1")
    monkeypatch.setattr(
        url_fetch_module,
        "_PinnedHTTPSConnection",
        lambda host, pinned_ip, _port, *, connect_timeout, read_timeout: _LoopbackHTTPConnection(
            host, pinned_ip, port, connect_timeout=connect_timeout, read_timeout=read_timeout
        ),
    )
    yield _raw_local_server


def test_safe_fetch_get_returns_content_and_content_type(local_server: HTTPServer) -> None:
    del local_server
    result = safe_fetch_get("https://example.org/ok")

    assert result.content == b'[{"id": 1}]'
    assert result.content_type == "application/json"


def test_safe_fetch_get_follows_redirect(local_server: HTTPServer) -> None:
    del local_server
    result = safe_fetch_get("https://example.org/redirect-once")

    assert result.content == b'[{"id": 1}]'


def test_safe_fetch_get_rejects_redirect_loop(local_server: HTTPServer) -> None:
    del local_server
    with pytest.raises(IngestionError, match="too many redirects"):
        safe_fetch_get("https://example.org/redirect-loop", max_redirects=3)


def test_safe_fetch_get_enforces_max_bytes(local_server: HTTPServer) -> None:
    del local_server
    with pytest.raises(IngestionError, match="exceeds max size"):
        safe_fetch_get("https://example.org/big", max_bytes=100)


def test_safe_fetch_get_rejects_non_200_status(local_server: HTTPServer) -> None:
    del local_server
    with pytest.raises(IngestionError, match="unexpected HTTP status"):
        safe_fetch_get("https://example.org/not-found")


def test_safe_fetch_get_rejects_non_https_before_any_connection() -> None:
    with pytest.raises(IngestionError, match="https"):
        safe_fetch_get("http://example.org/ok")


# --- redirect body read  unbounded   (BLOCKER, #538 review) ------


def test_safe_fetch_get_does_not_perform_unbounded_read_on_redirect_body(
    local_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """redirect(3xx)  body amt  read()    .

     read  200   ``_read_bounded()`` max_bytes
    cap redirect hop   .
    """
    del local_server
    calls: list[int | None] = []
    original_read = http.client.HTTPResponse.read

    def tracking_read(self: http.client.HTTPResponse, amt: int | None = None) -> bytes:
        calls.append(amt)
        return original_read(self, amt)

    monkeypatch.setattr(http.client.HTTPResponse, "read", tracking_read)

    result = safe_fetch_get("https://example.org/redirect-with-body")

    assert result.content == b'[{"id": 1}]'  #   redirect  .
    # redirect hop   read()  amt=None()
    #  —  200  _read_bounded()  chunk size .
    assert all(amt is not None for amt in calls)


def test_safe_fetch_get_rejects_redirect_to_private_address(
    _raw_local_server: HTTPServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """redirect target private  hop     .

    redirect body      SSRF (hop )
       — ``local_server`` fixture  host
    127.0.0.1    hop(example.org)
    redirect target(10.0.0.1)  ``_resolve_and_validate``
     .
    """
    import kpubdata_builder.ingestion.url_fetch as url_fetch_module

    port = _raw_local_server.server_address[1]
    real_resolve_and_validate = url_fetch_module._resolve_and_validate

    def fake_resolve(host: str, resolve_port: int) -> str:
        if host == "example.org":
            return "127.0.0.1"
        return real_resolve_and_validate(host, resolve_port)

    monkeypatch.setattr(url_fetch_module, "_resolve_and_validate", fake_resolve)
    monkeypatch.setattr(
        url_fetch_module,
        "_PinnedHTTPSConnection",
        lambda host, pinned_ip, _port, *, connect_timeout, read_timeout: _LoopbackHTTPConnection(
            host, pinned_ip, port, connect_timeout=connect_timeout, read_timeout=read_timeout
        ),
    )

    with pytest.raises(IngestionError, match="SSRF policy"):
        safe_fetch_get("https://example.org/redirect-to-private")


# --- connect/read timeout  (SHOULD FIX, #538 review) -------------------------


class _FakeTLSSocket:
    """``ssl.SSLSocket``  ``settimeout``   test double."""

    def __init__(self) -> None:
        self.settimeout_calls: list[float] = []

    def settimeout(self, value: float) -> None:
        self.settimeout_calls.append(value)


def test_pinned_https_connection_uses_connect_timeout_for_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TCP connect connect_timeout , DNS  resolve ."""
    captured_addresses: list[tuple[str, int]] = []
    captured_timeouts: list[float] = []
    fake_raw_socket = object()
    fake_tls_socket = _FakeTLSSocket()

    def fake_create_connection(address: tuple[str, int], timeout: float) -> object:
        captured_addresses.append(address)
        captured_timeouts.append(timeout)
        return fake_raw_socket

    def fake_wrap_socket(
        self: ssl.SSLContext, sock: object, *, server_hostname: str
    ) -> _FakeTLSSocket:
        assert sock is fake_raw_socket
        assert server_hostname == "example.org"
        return fake_tls_socket

    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", fake_wrap_socket)

    connection = _PinnedHTTPSConnection(
        "example.org", "8.8.8.8", 443, connect_timeout=1.5, read_timeout=9.0
    )
    connection.connect()

    #  IP   — hostname  resolve (DNS
    # rebinding  ).
    assert captured_addresses == [("8.8.8.8", 443)]
    assert captured_timeouts == [1.5]


def test_pinned_https_connection_switches_to_read_timeout_after_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect   socket read timeout read_timeout ."""
    fake_tls_socket = _FakeTLSSocket()

    monkeypatch.setattr(socket, "create_connection", lambda address, timeout: object())
    monkeypatch.setattr(
        ssl.SSLContext,
        "wrap_socket",
        lambda self, sock, *, server_hostname: fake_tls_socket,
    )

    connection = _PinnedHTTPSConnection(
        "example.org", "8.8.8.8", 443, connect_timeout=1.5, read_timeout=9.0
    )
    connection.connect()

    assert fake_tls_socket.settimeout_calls == [9.0]


class _TimeoutHTTPConnection:
    """connect   timeout  ``_PinnedHTTPSConnection`` test double."""

    def __init__(
        self,
        host: str,
        pinned_ip: str,
        port: int,
        *,
        connect_timeout: float,
        read_timeout: float,
    ) -> None:
        del host, pinned_ip, port, connect_timeout, read_timeout

    def request(self, method: str, path: str, headers: dict[str, str]) -> None:
        del method, path, headers
        raise TimeoutError("simulated connect timeout")

    def getresponse(self) -> object:
        raise AssertionError("must not be reached after a connect timeout")

    def close(self) -> None:
        return


def test_safe_fetch_get_maps_timeout_to_ingestion_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """connect/read timeout    IngestionError  mapping."""
    import kpubdata_builder.ingestion.url_fetch as url_fetch_module

    monkeypatch.setattr(url_fetch_module, "_resolve_and_validate", lambda host, _port: "8.8.8.8")
    monkeypatch.setattr(url_fetch_module, "_PinnedHTTPSConnection", _TimeoutHTTPConnection)

    with pytest.raises(IngestionError, match="failed to fetch"):
        safe_fetch_get("https://example.org/ok")


def test_no_hop_of_a_redirect_carries_a_credential_header(local_server: HTTPServer) -> None:
    """#685: a url source sends no credential, so no redirect can re-send one.

    Every hop — the first request and each one a redirect leads to — carries only the
    fixed headers the fetcher sets itself and what http.client adds. Nothing a spec or
    a user supplies can reach a request, and there is no Authorization to forward.
    """
    del local_server
    _Handler.seen_headers.clear()

    safe_fetch_get("https://example.org/redirect-once")

    assert len(_Handler.seen_headers) == 2  # the redirect and its target
    for hop in _Handler.seen_headers:
        assert set(hop) <= {"host", "accept-encoding", "accept", "user-agent", "connection"}

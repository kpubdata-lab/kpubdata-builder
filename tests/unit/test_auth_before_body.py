"""A request is authenticated before its body is read (#1069).

The HTTP layer read the whole body first — an upload is up to 20 MiB — and authenticated
afterwards, so a request with no token cost the server that much, and the failure
throttle applied after the cost was paid.

These talk to a real server over a socket and send the headers only: the declared body
never comes. A server that waits for it does not answer within the wait these allow.
"""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import pytest

from kpubdata_builder import cli
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.http import make_handler

_KEY = "expected-api-key-value"
_DECLARED = 8 * 1024 * 1024
#: A server that tries to read the body waits for its socket timeout (30 s).
_ANSWER_WITHIN = 5.0


@pytest.fixture()
def address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int]]:
    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", _KEY)
    monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", "3")
    service = BuilderService(output_root=tmp_path, client_factory=cli._create_client)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _answer(
    address: tuple[str, int], path: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, dict[str, object]]:
    """Send the request line and headers — and ``body`` only if given — and read the answer."""
    lines = [f"POST {path} HTTP/1.1", f"Host: {address[0]}", "Connection: close"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    with socket.create_connection(address, timeout=_ANSWER_WITHIN) as connection:
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        if body is not None:
            connection.sendall(body)
        received = b""
        while chunk := connection.recv(65536):
            received += chunk
    head, _, payload = received.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, json.loads(payload)


def _upload_headers(length: int, **extra: str) -> dict[str, str]:
    return {
        "Content-Type": "text/csv",
        "Content-Length": str(length),
        "X-Upload-Filename": "rows.csv",
        **extra,
    }


def test_an_upload_with_no_token_is_refused_without_its_body(address: tuple[str, int]) -> None:
    status, body = _answer(address, "/uploads", _upload_headers(_DECLARED), None)

    assert status == 401
    assert body["code"] == "unauthorized"


def test_a_json_request_with_a_wrong_key_is_refused_without_its_body(
    address: tuple[str, int],
) -> None:
    headers = {"Content-Type": "application/json", "Content-Length": "4096", "X-API-Key": "wrong"}

    status, body = _answer(address, "/build", headers, None)

    assert status == 401
    assert body["code"] == "unauthorized"


def test_a_throttled_client_is_refused_without_its_body(address: tuple[str, int]) -> None:
    for _ in range(3):
        assert _answer(address, "/uploads", _upload_headers(_DECLARED), None)[0] == 401

    # Even with the right key now: the client is cut off, and its body is still not read.
    status, body = _answer(
        address, "/uploads", _upload_headers(_DECLARED, **{"X-API-Key": _KEY}), None
    )

    assert status == 429
    assert body["code"] == "auth_throttled"


def test_each_refusal_is_counted_once(address: tuple[str, int]) -> None:
    """Negative: the gate runs before the body and again in dispatch — a refused request
    must not reach the second, or two failures would be counted for one."""
    for _ in range(2):
        assert _answer(address, "/uploads", _upload_headers(_DECLARED), None)[0] == 401

    # Two failures with a limit of three: the third request is still answered 401.
    assert _answer(address, "/uploads", _upload_headers(_DECLARED), None)[0] == 401
    assert _answer(address, "/uploads", _upload_headers(_DECLARED), None)[0] == 429


def test_an_authenticated_request_has_its_body_read(address: tuple[str, int]) -> None:
    """Negative: with the key, the body is read and the request is handled as before."""
    payload = json.dumps({"spec": "not: a valid spec"}).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload)),
        "X-API-Key": _KEY,
    }

    status, body = _answer(address, "/validate", headers, payload)

    # The answer is about the spec in the body, so the body was read.
    assert status in (200, 400)
    assert body.get("code") not in ("unauthorized", "auth_throttled")
    assert "valid" in body or "error" in body

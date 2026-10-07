"""A client that disconnects mid-response leaves no traceback on stderr (#1132).

socketserver's default ``handle_error`` prints the whole traceback for any exception a
handler raises, and writing a response to a client that has gone raises
``BrokenPipeError`` or ``ConnectionResetError``. That is ordinary client behaviour, so it
is logged at debug level; every other exception still reaches the default handler.
"""

from __future__ import annotations

import socket
import struct
import threading
import unittest.mock
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder.service.app import BuilderService
from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler


def _no_client(**_kwargs: Any) -> Any:
    raise AssertionError("these tests make no provider call")


def _server(tmp_path: Path) -> BoundedThreadingHTTPServer:
    return BoundedThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(BuilderService(output_root=tmp_path, client_factory=_no_client)),
        max_workers=2,
    )


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_a_client_that_left_is_logged_at_debug_level_only(
    tmp_path: Path,
    error: type[OSError],
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server(tmp_path)
    try:
        with caplog.at_level("DEBUG", logger="kpubdata_builder.service.http"):
            try:
                raise error(32, "gone")
            except OSError:
                server.handle_error(None, ("127.0.0.1", 50000))
    finally:
        server.server_close()

    assert capsys.readouterr().err == ""
    assert [r.levelname for r in caplog.records] == ["DEBUG"]
    assert "closed the connection before the response was written" in caplog.text
    assert error.__name__ in caplog.text


@pytest.mark.parametrize("error", [RuntimeError("boom"), TimeoutError(), OSError(28, "No space")])
def test_any_other_error_still_prints_its_traceback(
    tmp_path: Path, error: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    server = _server(tmp_path)
    try:
        try:
            raise error
        except Exception:
            server.handle_error(None, ("127.0.0.1", 50000))
    finally:
        server.server_close()

    err = capsys.readouterr().err
    assert "Traceback (most recent call last)" in err
    assert type(error).__name__ in err


def test_a_real_disconnect_mid_response_is_quiet_and_the_server_keeps_serving(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    # The handler waits until the client has reset its connection, then writes a body
    # large enough that the write must fail.
    client_gone = threading.Event()
    big = {"rows": ["x" * 1024] * 4096}

    def slow_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        client_gone.wait(timeout=5)
        response = unittest.mock.Mock()
        response.status_code = 200
        response.body = big
        return response

    server = _server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        with unittest.mock.patch("kpubdata_builder.service.http.dispatch", slow_dispatch):
            client = socket.create_connection((host, port))
            client.sendall(b"GET /healthz-slow HTTP/1.1\r\nHost: x\r\n\r\n")
            # SO_LINGER 0: close with RST, as a browser does when it abandons a request.
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            client.close()
            client_gone.set()

        # The worker handled the failed write and is free for the next request.
        with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    err = capfd.readouterr().err
    assert "Traceback" not in err
    assert "Exception occurred during processing of request" not in err

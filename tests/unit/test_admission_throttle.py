"""A token refused at admission does not clear the failure throttle (#1325).

A token that verifies cleared the client's failure count before the sign-up ledger was
consulted, so a pending, rejected or deleted user's valid token wiped out the bad
attempts made from the same client — one valid-but-refused token every few guesses and
the throttle never engaged. The count is cleared only once a request is authenticated
*and* admitted.

A refusal at admission is not itself counted as a failure: the token is not a guess,
and Studio sends every screen's requests for a pending user and reads the 403
``signup_pending`` to show the sign-up notice. Counting it would turn that notice into
429 for a waiting user, and behind a shared proxy for everyone else too (#1031).
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.app import refuse_before_body
from kpubdata_builder.service.auth import AuthError, Principal, compute_owner_id
from kpubdata_builder.service.http import make_handler
from kpubdata_builder.spec import JsonValue

_ISSUER = "https://idp.example"
_CLIENT = "203.0.113.7"
_LIMIT = 3


def _user(sub: str, *, admitted: bool = False, admin: bool = False) -> Principal:
    return Principal(
        kind="oidc",
        identifier=sub[:8],
        owner_id=compute_owner_id("oidc", _ISSUER, sub),
        is_admin=admin,
        admitted=admitted or admin,
        display_name=f"{sub}@example.com",
    )


_PENDING = _user("pending")
_LISTED = _user("listed", admitted=True)
_ADMIN = _user("admin", admin=True)
_API_KEY = Principal(kind="api_key", identifier="api-key")
_BAD = AuthError("invalid credentials")


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    monkeypatch.setenv("KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT", str(_LIMIT))
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: object())


def _send(
    service: BuilderService,
    monkeypatch: pytest.MonkeyPatch,
    who: Principal | AuthError,
    method: str = "GET",
    path: str = "/builds",
) -> ServiceResponse:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: who)
    response = dispatch(service, method, path, None, client_id=_CLIENT)
    assert isinstance(response, ServiceResponse)
    return response


def _code(response: ServiceResponse) -> JsonValue:
    return cast(dict[str, JsonValue], response.body).get("code")


def _reject(service: BuilderService, monkeypatch: pytest.MonkeyPatch, who: Principal) -> None:
    _send(service, monkeypatch, who)
    decided = _send(service, monkeypatch, _ADMIN, "POST", f"/admin/users/{who.owner_id}/reject")
    assert decided.status_code == 200


def _two_failures_then(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, who: Principal
) -> ServiceResponse:
    for _ in range(_LIMIT - 1):
        assert _send(service, monkeypatch, _BAD).status_code == 401
    return _send(service, monkeypatch, who)


def test_a_pending_users_token_does_not_clear_the_failures(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = _two_failures_then(service, monkeypatch, _PENDING)
    assert (refused.status_code, _code(refused)) == (403, "signup_pending")

    # One more guess reaches the limit: the pending token did not reset the count.
    assert _send(service, monkeypatch, _BAD).status_code == 401
    throttled = _send(service, monkeypatch, _BAD)
    assert (throttled.status_code, _code(throttled)) == (429, "auth_throttled")


def test_a_rejected_users_token_does_not_clear_the_failures(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    rejected = _user("rejected", admitted=True)
    _reject(service, monkeypatch, rejected)

    refused = _two_failures_then(service, monkeypatch, rejected)
    assert (refused.status_code, _code(refused)) == (403, "signup_rejected")

    assert _send(service, monkeypatch, _BAD).status_code == 401
    assert _send(service, monkeypatch, _BAD).status_code == 429


def test_a_deleted_users_token_does_not_clear_the_failures(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "owner_deleted", lambda owner_id: owner_id == _LISTED.owner_id)

    refused = _two_failures_then(service, monkeypatch, _LISTED)
    assert (refused.status_code, _code(refused)) == (403, "signup_rejected")

    assert _send(service, monkeypatch, _BAD).status_code == 401
    assert _send(service, monkeypatch, _BAD).status_code == 429


def test_a_refused_token_never_clears_the_failures_under_concurrency(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pending requests interleaved with failures: every failure stays counted."""
    principals = {"bad": _BAD, "pending": _PENDING}
    local = threading.local()
    _send(service, monkeypatch, _PENDING)  # recorded once, so threads only read the ledger
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principals[local.who])
    statuses: list[int] = []
    lock = threading.Lock()

    def worker(who: str, times: int) -> None:
        local.who = who
        for _ in range(times):
            response = dispatch(service, "GET", "/builds", None, client_id=_CLIENT)
            with lock:
                statuses.append(response.status_code)
            time.sleep(0.005)  # interleave the two kinds of request

    threads = [threading.Thread(target=worker, args=("pending", 40)) for _ in range(4)]
    threads.append(threading.Thread(target=worker, args=("bad", _LIMIT)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert statuses.count(401) == _LIMIT
    assert set(statuses) <= {401, 403, 429}
    assert service._auth_throttle.retry_after(_CLIENT) is not None


@pytest.mark.parametrize("who", [_LISTED, _ADMIN, _API_KEY], ids=["listed", "admin", "api_key"])
def test_an_admitted_principal_still_clears_the_failures(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, who: Principal
) -> None:
    """Negative: recovery after genuine bad attempts is unchanged."""
    assert _two_failures_then(service, monkeypatch, who).status_code == 200

    for _ in range(_LIMIT - 1):
        assert _send(service, monkeypatch, _BAD).status_code == 401
    assert _send(service, monkeypatch, _BAD).status_code == 401


def test_a_refusal_at_admission_is_not_itself_a_failure(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: a waiting user's screens keep getting the sign-up notice, never 429."""
    for _ in range(_LIMIT * 4):
        response = _send(service, monkeypatch, _PENDING)
        assert (response.status_code, _code(response)) == (403, "signup_pending")
    assert service._auth_throttle.retry_after(_CLIENT) is None


def test_a_refusal_before_the_body_is_the_admission_refusal(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _PENDING)
    before = refuse_before_body(service, api_key=None, bearer_token="t", client_id=_CLIENT)
    after = _send(service, monkeypatch, _PENDING, "POST", "/build")

    assert before is not None
    # The same answer either way: nothing about the account beyond what dispatch says.
    assert (before.status_code, before.body) == (after.status_code, after.body)


def test_an_admitted_principal_passes_the_gate_before_the_body(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: an admitted user's body is read as before."""
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _LISTED)
    assert refuse_before_body(service, api_key=None, bearer_token="t", client_id=_CLIENT) is None


def test_a_new_and_a_known_pending_user_get_the_same_answer(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: the refusal does not say whether the ledger knew the account."""
    first = _send(service, monkeypatch, _user("first-sight"))
    again = _send(service, monkeypatch, _user("first-sight"))
    other = _send(service, monkeypatch, _PENDING)

    assert first.body == again.body == other.body
    assert first.status_code == again.status_code == other.status_code == 403


@pytest.fixture()
def address(service: BuilderService) -> Iterator[tuple[str, int]]:
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def test_a_pending_users_upload_is_refused_without_its_body(
    address: tuple[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the headers are sent: a server that waits for the body does not answer."""
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _PENDING)
    lines = [
        "POST /uploads HTTP/1.1",
        f"Host: {address[0]}",
        "Connection: close",
        "Authorization: Bearer t",
        "Content-Type: text/csv",
        f"Content-Length: {8 * 1024 * 1024}",
        "X-Upload-Filename: rows.csv",
    ]
    with socket.create_connection(address, timeout=5.0) as connection:
        connection.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        received = b""
        while chunk := connection.recv(65536):
            received += chunk
    head, _, payload = received.partition(b"\r\n\r\n")

    assert int(head.split(b" ", 2)[1]) == 403
    assert json.loads(payload)["code"] == "signup_pending"

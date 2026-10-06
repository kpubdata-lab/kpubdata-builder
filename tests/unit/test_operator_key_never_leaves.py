"""In own-credential mode the operator's key appears nowhere (#990).

The other tests for this mode check what the client is built with. This one checks what
leaves: a real kpubdata ``Client`` and its real transport, with only the socket replaced.
The operator's key sits in the environment under every spelling kpubdata reads; the
requester sends their own. After the build the operator's key must be in no request, no
log record and no file under the run — and the requester's key must be what was sent,
or the test would pass with nothing sent at all.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from kpubdata_builder.cli import _create_client, client_keeps_environment_keys_out
from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal

pytestmark = pytest.mark.skipif(
    not client_keeps_environment_keys_out(),
    reason="needs a kpubdata whose Client takes env_keys (0.9.0 or later)",
)

_OPERATOR = "OPERATOR-canary-0f3a"
_OWN = "requester-canary-7c1e"
_SEND = "kpubdata.transport.http.httpx.Client.send"

# apt_trade: a spec-backed dataset whose terms allow reading, so nothing but the key
# decides whether the build goes through.
_SPEC = """\
dataset_id: leak.check
title: Leak check
description: d
sources:
  - provider: datago
    dataset: apt_trade
    params:
      LAWD_CD: "11680"
      DEAL_YMD: "202401"
exports:
  - kind: jsonl
    output_path: data.jsonl
"""

_HAS_KEY = Principal("oidc", "has-key", "oidc:has-key")
_NO_KEY = Principal("oidc", "no-key", "oidc:no-key")

_BODY = {
    "response": {
        "header": {"resultCode": "00", "resultMsg": "OK"},
        "body": {
            "items": {"item": [{"aptNm": "A", "dealAmount": "1,000", "excluUseAr": "84.9"}]},
            "totalCount": 1,
            "pageNo": 1,
            "numOfRows": 100,
        },
    }
}


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    for name in ("KPUBDATA_DATAGO_API_KEY", "DATAGO_API_KEY", "KPUBDATA_API_KEY"):
        monkeypatch.setenv(name, _OPERATOR)
    monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", "true")
    # The cross-repo job replays recorded fixtures instead of calling HTTP; this test
    # needs the HTTP path it observes.
    monkeypatch.delenv("KPUBDATA_REPLAY_DIR", raising=False)
    repository = SQLiteCredentialRepository(
        tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    )
    repository.put("oidc:has-key", "datago", _OWN)
    runs = tmp_path / "runs"
    runs.mkdir()
    return BuilderService(
        output_root=runs, client_factory=_create_client, credential_repository=repository
    )


def _everything_under(root: Path) -> str:
    """The text of every file under ``root`` that reads as text."""
    parts = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            parts.append(path.read_bytes().decode("utf-8", errors="ignore"))
    return "\n".join(parts)


def test_the_operators_key_is_in_no_request_log_or_file(
    service: BuilderService, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sent: list[str] = []

    def send(request: httpx.Request, **_kwargs: object) -> httpx.Response:
        sent.append(str(request.url) + " " + json.dumps(dict(request.headers)))
        return httpx.Response(
            200, json=_BODY, headers={"content-type": "application/json"}, request=request
        )

    with patch(_SEND, side_effect=send), caplog.at_level(logging.DEBUG):
        response = service.build(_SPEC, run_id="own-key-run", principal=_HAS_KEY)

    assert response.status_code == 200, response.body
    # The build did call the provider, with the requester's key and nobody else's.
    assert sent
    assert all(_OWN in request for request in sent)
    assert all(_OPERATOR not in request for request in sent)
    # Nothing the run left behind carries the operator's key — or the requester's.
    assert _OPERATOR not in json.dumps(response.body)
    records = caplog.text + "".join(str(record.__dict__) for record in caplog.records)
    assert _OPERATOR not in records
    on_disk = _everything_under(tmp_path / "runs")
    assert "own-key-run" in on_disk
    assert _OPERATOR not in on_disk
    assert _OWN not in on_disk
    assert _OWN not in records


def test_a_requester_without_a_key_sends_nothing_at_all(
    service: BuilderService, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def send(request: httpx.Request, **_kwargs: object) -> httpx.Response:
        raise AssertionError(f"no request may leave without the requester's own key: {request.url}")

    with patch(_SEND, side_effect=send), caplog.at_level(logging.DEBUG):
        response = service.build(_SPEC, run_id="no-key-run", principal=_NO_KEY)

    assert response.status_code == 403
    assert response.body["code"] == "provider_credential_required"
    assert _OPERATOR not in json.dumps(response.body)
    assert _OPERATOR not in caplog.text
    assert _OPERATOR not in _everything_under(tmp_path / "runs")


def test_the_measurement_sees_the_operators_key_when_the_switch_is_off(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate itself: with the mode off the same keyless build does go out on the
    operator's key, and the capture above is what would show it."""
    monkeypatch.delenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL")
    sent: list[str] = []

    def send(request: httpx.Request, **_kwargs: object) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(
            200, json=_BODY, headers={"content-type": "application/json"}, request=request
        )

    with patch(_SEND, side_effect=send):
        response = service.build(_SPEC, run_id="fallback-run", principal=_NO_KEY)

    assert response.status_code == 200, response.body
    assert sent
    assert all(_OPERATOR in request for request in sent)

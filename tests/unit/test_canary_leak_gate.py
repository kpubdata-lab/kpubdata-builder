"""A canary key must appear nowhere a build leaves traces (#686).

Individual fixes have had individual tests, and those tests have pinned leaks rather
than caught them (kpubdata#664). This gate works the other way round: a user stores a
canary as their provider key, the service is driven through every path it has —
success, each class of upstream failure, a timeout, a redirect, an upstream that
echoes the request, cancellation and a restart — and then everything the process
left behind is searched for the canary in every encoding it could take.

The client is the **real** kpubdata ``Client``; only its HTTP layer is replaced by
``httpx.MockTransport``. So URL construction, error messages and retries are the ones
production runs.

Destinations searched here, and why the rest are not:

    logs            every record at DEBUG, formatted with its arguments and traceback
    files           every byte under the workspace — manifests, artifacts, stage
                    outputs, the warehouse, and every SQLite file including -wal/-shm
                    (build index, events, credentials, uploads, publish receipts)
    responses       every response body the service returned
    job registry    each async job's snapshot and repr

    Not here: browser storage, HAR and screenshots belong to Studio; proxy logs,
    traces, error-tracker payloads and GitHub Actions logs belong to a deployment.
    docs/CREDENTIAL_SURFACE.md records them.

The last test plants a leak on purpose and requires the gate to fail — a gate that
has never been seen to fail is not known to work.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote, quote_plus

import httpx
import kpubdata.core.spec as kpubdata_spec
import kpubdata.transport.http as kpubdata_http
import kpubdata.transport.retry as kpubdata_retry
import pytest

from kpubdata_builder import logging_redaction
from kpubdata_builder.cli import _create_client
from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse

# Shaped like a data.go.kr key: base64 alphabet, so URL and JSON encodings differ
# from the raw form and a raw-only search would miss them.
CANARY = "CanaryK3y7f3a9c1e5b2d8046q+/Zx0Aa=="

_PRINCIPAL = Principal("oidc", "canary-user", "oidc:canary-owner")

_SPEC = """\
dataset_id: canary.gate
title: Canary gate
description: Every path a build takes, with a canary for a key
sources:
  - provider: datago
    dataset: air_quality
    params:
      sidoName: 서울
exports:
  - kind: jsonl
    output_path: out/data.jsonl
  - kind: huggingface
    output_path: out/hf
    options:
      format: jsonl
"""

_ITEMS = [
    {"stationName": "중구", "pm10Value": "20", "dataTime": "2026-09-09 19:00"},
    {"stationName": "종로구", "pm10Value": "23", "dataTime": "2026-09-09 19:00"},
]


def encodings(value: str) -> dict[str, str]:
    """Every form in which a value could be written down without being the value."""
    raw = value.encode("utf-8")
    return {
        "raw": value,
        "url": quote(value, safe=""),
        "url_plus": quote_plus(value),
        "double_url": quote(quote(value, safe=""), safe=""),
        "base64": base64.b64encode(raw).decode("ascii"),
        "base64_url": base64.urlsafe_b64encode(raw).decode("ascii"),
        "json": json.dumps(value)[1:-1],
    }


def hits_in(text: str, secret: str = CANARY) -> list[str]:
    return [name for name, form in encodings(secret).items() if form in text]


_cached_spec_file = lru_cache(maxsize=None)(kpubdata_spec.load_spec_file)


class _NoSleep:
    """``time`` for kpubdata's HTTP transport, minus the waiting between retries."""

    def __getattr__(self, name: str) -> object:
        return getattr(time, name)

    @staticmethod
    def sleep(_seconds: float) -> None:
        return None


@dataclass
class _Upstream:
    """What the mocked provider does with each request."""

    mode: str = "success"
    requests: list[httpx.Request] = field(default_factory=list)
    release: threading.Event = field(default_factory=threading.Event)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "apis.data.go.kr":
            return httpx.Response(200, json={"note": "redirect target"})
        if self.mode == "slow":
            self.release.wait(timeout=10)
        if self.mode == "timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        if self.mode == "redirect":
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/moved"})
        if self.mode in {"400", "403", "429", "500"}:
            # A provider that quotes the request back in its error page, as some do.
            return httpx.Response(int(self.mode), text=f"rejected: {request.url}")
        items = list(_ITEMS)
        if self.mode == "echo":
            items = [{**item, "requestUrl": str(request.url)} for item in items]
        body = {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL_CODE"},
                "body": {"items": items, "numOfRows": 100, "pageNo": 1, "totalCount": len(items)},
            }
        }
        return httpx.Response(200, json=body)


@dataclass
class _World:
    root: Path
    upstream: _Upstream
    responses: list[ServiceResponse]
    service: Callable[[], BuilderService]
    caplog: pytest.LogCaptureFixture

    def call(self, response: ServiceResponse) -> ServiceResponse:
        self.responses.append(response)
        return response

    def leaks(self) -> dict[str, list[str]]:
        """Every destination where the canary appears, with the encodings found."""
        found: dict[str, list[str]] = {}
        log_text = "\n".join(
            logging.Formatter("%(message)s").format(record) for record in self.caplog.records
        )
        if hits := hits_in(log_text):
            found["logs"] = hits
        for path in sorted(p for p in self.root.rglob("*") if p.is_file()):
            text = path.read_bytes().decode("utf-8", errors="replace")
            if hits := hits_in(text):
                found[f"file:{path.relative_to(self.root)}"] = hits
        for index, response in enumerate(self.responses):
            text = json.dumps(response.body, ensure_ascii=False, default=str)
            if hits := hits_in(text):
                found[f"response[{index}]:{response.status_code}"] = hits
        return found


@pytest.fixture
def world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[_World]:
    # KPUBDATA_REPLAY_DIR (set by the cross-repo job) swaps HTTP for recorded fixtures;
    # the gate has to drive the HTTP path it inspects.
    for name in (
        "KPUBDATA_DATAGO_API_KEY",
        "KPUBDATA_CACHE",
        "KPUBDATA_REPLAY_DIR",
        "OIDC_ISSUER",
        "ENFORCE_OWNERSHIP",
    ):
        monkeypatch.delenv(name, raising=False)
    caplog.set_level(logging.DEBUG)
    # Retries run for real; only their waiting is skipped.
    monkeypatch.setitem(kpubdata_retry.with_retry.__kwdefaults__, "sleep", lambda _s: None)
    monkeypatch.setattr(kpubdata_http, "time", _NoSleep())
    # Every service call makes a new Client, and each one parses ~1,000 spec files.
    # The definitions are frozen, so parsing each once keeps the gate fast without
    # changing what any Client sees.
    monkeypatch.setattr(kpubdata_spec, "load_spec_file", _cached_spec_file)

    upstream = _Upstream()
    real_client = httpx.Client

    def mocked_client(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(upstream.handle), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(kpubdata_http.httpx, "Client", mocked_client)

    runs = tmp_path / "runs"
    runs.mkdir()

    def make_service() -> BuilderService:
        return BuilderService(
            output_root=runs,
            client_factory=_create_client,
            credential_repository=SQLiteCredentialRepository(
                tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
            ),
            warehouse_root=tmp_path / "warehouse",
            async_max_workers=1,
        )

    service = make_service()
    stored = service.put_provider_credential("datago", {"credential": CANARY}, principal=_PRINCIPAL)
    assert stored.status_code == 200
    world = _World(tmp_path, upstream, [stored], lambda: service, caplog)
    world.make_service = make_service  # type: ignore[attr-defined]
    yield world


def _wait_terminal(world: _World, service: BuilderService, run_id: str) -> ServiceResponse:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        status = service.build_status(run_id)
        if status.body.get("status") in {"succeeded", "failed", "cancelled"}:
            return world.call(status)
        time.sleep(0.02)
    raise AssertionError(f"job {run_id} did not finish")


SCENARIOS = ["success", "400", "403", "429", "500", "timeout", "redirect", "echo"]


@pytest.mark.parametrize("mode", SCENARIOS)
def test_no_trace_of_the_key_after_a_synchronous_build(world: _World, mode: str) -> None:
    world.upstream.mode = mode
    service = world.service()

    world.call(service.preview(_SPEC, principal=_PRINCIPAL))
    built = world.call(service.build(_SPEC, run_id=f"sync-{mode}", principal=_PRINCIPAL))
    world.call(service.provider_status("datago", principal=_PRINCIPAL))
    for read in (service.manifest, service.artifacts, service.spec, service.list_run_stages):
        world.call(read(built.body.get("run_id", f"sync-{mode}")))

    # The canary did go upstream — otherwise finding nothing proves nothing.
    assert any(hits_in(str(r.url)) for r in world.upstream.requests)
    assert world.leaks() == {}
    # And no client's key is still held for log scrubbing once the calls are done.
    assert logging_redaction.active_count() == 0


def test_a_redirect_does_not_carry_the_key_to_another_host(world: _World) -> None:
    world.upstream.mode = "redirect"
    world.call(world.service().build(_SPEC, run_id="redirect", principal=_PRINCIPAL))

    elsewhere = [r for r in world.upstream.requests if r.url.host != "apis.data.go.kr"]
    for request in elsewhere:
        assert hits_in(str(request.url)) == []
        assert hits_in(json.dumps(dict(request.headers))) == []


def test_no_trace_after_an_async_build_is_cancelled(world: _World) -> None:
    world.upstream.mode = "slow"
    service = world.service()

    world.call(service.submit_build(_SPEC, run_id="cancel-me", owner_id=_PRINCIPAL.owner_id))
    deadline = time.monotonic() + 5
    while not world.upstream.requests and time.monotonic() < deadline:
        time.sleep(0.01)
    world.call(service.cancel_build("cancel-me"))
    world.upstream.release.set()
    final = _wait_terminal(world, service, "cancel-me")

    snapshot = service._async_builds.get("cancel-me")
    assert snapshot is not None
    assert hits_in(repr(snapshot)) == []
    assert hits_in(json.dumps(snapshot.to_body(), default=str)) == []
    assert final.body["status"] in {"cancelled", "succeeded"}
    assert world.leaks() == {}


def test_no_trace_after_an_async_build_and_a_restart(world: _World) -> None:
    service = world.service()
    world.call(service.submit_build(_SPEC, run_id="before-restart", owner_id=_PRINCIPAL.owner_id))
    _wait_terminal(world, service, "before-restart")

    restarted: BuilderService = world.make_service()  # type: ignore[attr-defined]
    for read in (
        restarted.build_status,
        restarted.manifest,
        restarted.artifacts,
        restarted.spec,
    ):
        world.call(read("before-restart"))
    world.call(restarted.get_build_events("before-restart", limit=500, tail=False))
    world.call(restarted.list_builds())
    world.call(restarted.provider_credential("datago", principal=_PRINCIPAL))

    assert any(hits_in(str(r.url)) for r in world.upstream.requests)
    assert world.leaks() == {}


def test_the_gate_fails_on_a_planted_leak(world: _World) -> None:
    """The gate itself: every encoding of a leak, in every destination, is caught."""
    logging.getLogger("kpubdata_builder.planted").debug("oops %s", quote(CANARY, safe=""))
    (world.root / "runs" / "planted.txt").write_text(
        base64.b64encode(CANARY.encode()).decode(), encoding="utf-8"
    )
    world.call(ServiceResponse(500, {"error": json.dumps({"key": CANARY})}))

    leaks = world.leaks()

    assert "url" in leaks["logs"]
    assert "base64" in leaks["file:runs/planted.txt"]
    assert "raw" in leaks[f"response[{len(world.responses) - 1}]:500"]
    for name, form in encodings(CANARY).items():
        assert hits_in(f"prefix {form} suffix") == [name] or name in hits_in(form)

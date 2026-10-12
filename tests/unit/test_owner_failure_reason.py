"""A failed run's owner reads why it failed after the registry has let the job go (#1120).

``GET /builds/{run_id}`` answers from the job registry while it holds the job, and from
the run's manifest afterwards: after a restart, after the registry evicted an old job,
and always for a synchronous ``POST /build``, which is never a job. The manifest path
carried no ``error``, so the owner read ``failed`` with ``error: null`` — the report
this issue began with — while ``GET /admin/runs`` gave an administrator the reason.

The owner now reads the line the index and the admin list hold: the first entry of the
manifest's ``failures``, a fixed sentence. The manifest's own ``errors`` are still not
copied, and a canary in the key, the spec, the provider's error and an old manifest's
raw error text reaches neither the answer nor a log.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import kpubdata
import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.routes import admin
from kpubdata_builder.spec import JsonValue

_CANARY = "canary-value-1120-owner-reason"
_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")
_ADMIN = Principal(kind="oidc", identifier="admin123", owner_id="oidc:admin", is_admin=True)
_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    """A provider whose answer the test sets."""

    def __init__(self) -> None:
        self.answer: list[dict[str, JsonValue]] | Exception = [{"id": "1", "v": 1}]

    def list(self, **_params: object) -> _Result:
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Result(list(self.answer))

    def dataset(self, _key: str) -> _Source:
        return self


def _factory(source: _Source) -> Callable[..., object]:
    def create(
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> object:
        return source

    return create


def _spec(schema: str = "", *, title: str = "Air") -> str:
    return (
        f"dataset_id: air\ntitle: {title}\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: m\n"
        + schema
        + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )


def _failing(answer: Exception | None = None) -> _Source:
    source = _Source()
    source.answer = answer or kpubdata.AuthError("refused", provider="datago", provider_code="30")
    return source


def _service(tmp_path: Path, source: _Source) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=_factory(source))


def _restarted(tmp_path: Path) -> BuilderService:
    """Another service over the same files: its registry knows none of the earlier jobs."""
    return _service(tmp_path, _Source())


def _stop(*services: BuilderService) -> None:
    for service in services:
        service._async_builds._executor.shutdown(wait=True)


def _status(service: BuilderService, run_id: str) -> dict[str, Any]:
    response = service.build_status(run_id)
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.body)


def _finished(service: BuilderService, run_id: str) -> dict[str, Any]:
    done = threading.Event()
    for _ in range(400):
        body = _status(service, run_id)
        if body["status"] in _TERMINAL:
            return body
        done.wait(0.05)
    raise AssertionError(f"{run_id} did not end")


def _admin_line(service: BuilderService, run_id: str) -> str | None:
    response = admin.route(service, "GET", "/admin/runs", None, "", _ADMIN)
    assert isinstance(response, ServiceResponse) and response.status_code == 200, response
    runs = cast(list[dict[str, Any]], cast(dict[str, Any], response.body)["runs"])
    return cast("str | None", next(run for run in runs if run["run_id"] == run_id)["error"])


def _as(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)


def _request(
    service: BuilderService,
    method: str,
    path: str,
    body: dict[str, JsonValue] | None = None,
    *,
    key: str | None = None,
) -> ServiceResponse:
    response = dispatch(
        service,
        method,
        path,
        body,
        provider_key_headers=[f"datago={key}"] if key is not None else (),
    )
    assert isinstance(response, ServiceResponse)
    return response


def test_an_async_failure_keeps_its_reason_after_a_restart(tmp_path: Path) -> None:
    first = _service(tmp_path, _failing())
    try:
        assert first.submit_build(_spec(), run_id="r1").status_code == 202
        held = _finished(first, "r1")
    finally:
        _stop(first)
    assert held["status"] == "failed"
    assert held["error"]

    second = _restarted(tmp_path)
    try:
        after = _status(second, "r1")
        line = _admin_line(second, "r1")
    finally:
        _stop(second)

    assert after["status"] == "failed"
    # The line an administrator reads for the same run, and it says what the job said.
    assert after["error"] == line
    assert held["error"] in after["error"]


def test_a_synchronous_failure_has_its_reason_without_any_restart(tmp_path: Path) -> None:
    """A synchronous build is never in the registry: its status is the manifest's."""
    service = _service(tmp_path, _failing())
    try:
        built = service.build(_spec(), run_id="r1")
        assert built.status_code == 502
        status = _status(service, "r1")
    finally:
        _stop(service)

    assert status["status"] == "failed"
    assert cast(dict[str, Any], built.body)["error"] in status["error"]


def test_three_failures_read_three_lines_each_the_administrators(tmp_path: Path) -> None:
    """The reason is read from the run, not a constant."""
    lines = set()
    cases: list[tuple[Exception | None, str]] = [
        (kpubdata.AuthError("refused", provider="datago", provider_code="30"), ""),
        (kpubdata.RateLimitError("slow", provider="datago", provider_code="22"), ""),
        (None, "    schema:\n      required: [not_there]\n"),
    ]
    for index, (answer, schema) in enumerate(cases):
        source = _Source()
        if answer is not None:
            source.answer = answer
        root = tmp_path / str(index)
        root.mkdir()
        service = _service(root, source)
        try:
            assert service.build(_spec(schema), run_id="r1").status_code == 502
            reason = _status(service, "r1")["error"]
            assert reason == _admin_line(service, "r1")
            lines.add(reason)
        finally:
            _stop(service)

    assert len(lines) == 3


def test_a_successful_run_has_no_error(tmp_path: Path) -> None:
    service = _service(tmp_path, _Source())
    try:
        assert service.build(_spec(), run_id="r1").status_code == 200
        status = _status(service, "r1")
    finally:
        _stop(service)

    assert status["status"] == "succeeded"
    assert "error" not in status


def test_an_old_manifest_gives_a_fixed_line_and_never_its_raw_errors(tmp_path: Path) -> None:
    """A manifest from before failures were recorded holds raw exception text in ``errors``."""
    service = _service(tmp_path, _failing())
    try:
        assert service.build(_spec(), run_id="r1").status_code == 502
        path = tmp_path / "r1" / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        del manifest["failures"]
        manifest["errors"] = [f"Traceback: /srv/data/{_CANARY}/x.py serviceKey={_CANARY}"]
        path.write_text(json.dumps(manifest), encoding="utf-8")
        status = _status(_restarted(tmp_path), "r1")
    finally:
        _stop(service)

    assert status["status"] == "failed"
    assert status["error"]
    assert _CANARY not in json.dumps(status)
    assert "/srv/data" not in status["error"]


def test_no_canary_reaches_the_owners_answer_or_a_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The key, a provider URL, a raw SQL error and the spec's title each hold a canary."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    caplog.set_level(logging.DEBUG)
    answer = RuntimeError(
        f"GET https://apis.data.go.kr/x?serviceKey={_CANARY} failed: "
        f"Binder Error: SELECT * FROM t WHERE v = '{_CANARY}'"
    )
    first = _service(tmp_path, _failing(answer))
    _as(monkeypatch, _ALICE)
    try:
        built = _request(
            first,
            "POST",
            "/build",
            {"spec": _spec(title=f"Air {_CANARY}"), "run_id": "r1"},
            key=_CANARY,
        )
        assert built.status_code == 502, built.body
    finally:
        _stop(first)

    second = _restarted(tmp_path)
    try:
        owners = _request(second, "GET", "/builds/r1")
        _as(monkeypatch, _BOB)
        others = _request(second, "GET", "/builds/r1")
    finally:
        _stop(second)

    assert owners.status_code == 200, owners.body
    body = cast(dict[str, Any], owners.body)
    assert body["status"] == "failed"
    assert body["error"]
    assert _CANARY not in json.dumps(body)
    # Another user is told nothing, as before: the reason is the owner's to read.
    assert others.status_code == 404, others.body
    assert _CANARY not in json.dumps(others.body)
    leaked = [record.getMessage() for record in caplog.records if _CANARY in record.getMessage()]
    assert leaked == []

"""A synchronous ``POST /build`` runs as a job and is held to a job's limits (#1242).

The synchronous route ran the pipeline on its request thread, outside the job
registry. The safety rails the async path has — a user's cancel
(``POST /builds/{run_id}/cancel``, #481), the running-time limit (#1119), the
per-owner limit on active builds (#1189) and the shutdown drain (#1118) — all work
through that registry, so none of them could reach a synchronous build. It now
submits a job and waits for it; its request and response stay what they were.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.build_limits import (
    BUILD_TIME_LIMIT_ENV,
    MAX_ACTIVE_BUILDS_PER_OWNER_ENV,
)
from kpubdata_builder.service.build_runs_api import TIME_LIMIT_CANCELLED_MESSAGE
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.service.providers import ProviderDescriptor
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

_COMBINATIONS = 40


def _spec(combinations: int = _COMBINATIONS) -> str:
    grid = ", ".join(str(i) for i in range(combinations))
    return (
        "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: m\n"
        f"    param_grid: {{nx: [{grid}]}}\n"
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _SlowSource:
    """Each combination takes ``delay`` seconds, so the whole grid takes a while."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0
        self.entered = threading.Event()

    def list(self, **params: object) -> _Result:
        self.calls += 1
        self.entered.set()
        time.sleep(self.delay)
        return _Result([{"nx": cast(JsonValue, params.get("nx")), "v": 1}])

    def dataset(self, _key: str) -> _SlowSource:
        return self


def _post_build_in_background(
    service: BuilderService, body: dict[str, JsonValue]
) -> tuple[threading.Thread, list[ServiceResponse]]:
    answers: list[ServiceResponse] = []

    def call() -> None:
        response = dispatch(service, "POST", "/build", body)
        assert isinstance(response, ServiceResponse)
        answers.append(response)

    thread = threading.Thread(target=call, daemon=True)
    thread.start()
    return thread, answers


def _assert_declared(response: ServiceResponse) -> None:
    """The answer is one ``POST /build`` declares for its status."""
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/build", "post", response.status_code)
    assert schema is not None
    assert validate(response.body, schema, contract) == []


def _cancel_messages(service: BuilderService, run_id: str) -> list[str]:
    """The run's ``run_cancelled`` messages; the event is appended just after the job ends."""
    pause = threading.Event()
    for _ in range(100):
        events = cast(dict[str, Any], service.get_build_events(run_id, limit=500, tail=False).body)[
            "events"
        ]
        found = [e["message"] for e in events if e["event"] == "run_cancelled"]
        if found:
            return found
        pause.wait(0.02)
    return []


def test_a_synchronous_build_can_be_cancelled(tmp_path: Path) -> None:
    source = _SlowSource(delay=0.05)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    thread, answers = _post_build_in_background(service, {"spec": _spec(), "run_id": "sync-1"})
    try:
        assert source.entered.wait(10)

        cancel = dispatch(service, "POST", "/builds/sync-1/cancel", None)

        assert isinstance(cancel, ServiceResponse)
        assert cancel.status_code == 200, cancel.body
        thread.join(timeout=20)
        assert not thread.is_alive()
        [answer] = answers
        assert answer.status_code == 409
        assert answer.body["status"] == "cancelled"
        assert answer.body["run_id"] == "sync-1"
        assert answer.body["code"] == "cancelled"
        _assert_declared(answer)
        # It stopped at a combination boundary, long before the grid was done.
        assert source.calls < _COMBINATIONS
    finally:
        service._async_builds.shutdown()


def test_a_synchronous_build_past_the_time_limit_is_cancelled_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "0.3")
    source = _SlowSource(delay=0.05)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    try:
        answer = dispatch(service, "POST", "/build", {"spec": _spec(), "run_id": "sync-slow"})

        assert isinstance(answer, ServiceResponse)
        assert answer.status_code == 409
        assert answer.body["status"] == "cancelled"
        assert answer.body["code"] == "time_limit_exceeded"
        assert answer.body["error"] == TIME_LIMIT_CANCELLED_MESSAGE
        _assert_declared(answer)
        assert source.calls < _COMBINATIONS
        assert _cancel_messages(service, "sync-slow") == [TIME_LIMIT_CANCELLED_MESSAGE]
    finally:
        service._async_builds.shutdown()


def test_the_shutdown_drain_reaches_a_running_synchronous_build(tmp_path: Path) -> None:
    source = _SlowSource(delay=0.05)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    thread, answers = _post_build_in_background(service, {"spec": _spec(), "run_id": "sync-2"})
    try:
        assert source.entered.wait(10)

        service.begin_shutdown()
        cancelled, still_running = service.drain_builds(0.05, 10.0)

        assert cancelled == ("sync-2",)
        assert still_running == ()
        thread.join(timeout=20)
        [answer] = answers
        assert answer.status_code == 409
        assert answer.body["status"] == "cancelled"
        assert answer.body["code"] == "server_shutdown"
    finally:
        service._async_builds.shutdown()


class _HeldJobs(BuilderService):
    """An async job holds its owner's place until ``release`` is set."""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(output_root=tmp_path, client_factory=lambda **_: _SlowSource(0))
        self.release = threading.Event()

    def _run_build_job(self, spec_yaml: str, run_id: str, *args: Any, **kwargs: Any) -> Any:
        if run_id.startswith("held-"):
            self.release.wait(timeout=10)
            return ServiceResponse(200, {"status": "ok", "run_id": run_id})
        return super()._run_build_job(spec_yaml, run_id, *args, **kwargs)


def test_a_synchronous_build_counts_toward_the_owners_active_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    monkeypatch.setenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "1")
    alice = Principal("oidc", "alice", "oidc:alice")
    monkeypatch.setattr(app_module, "authenticate", lambda **_: alice)
    service = _HeldJobs(tmp_path)
    monkeypatch.setattr(
        service._providers_service,
        "runtime_providers",
        lambda: (ProviderDescriptor("datago", False),),
    )
    try:
        held = dispatch(service, "POST", "/builds", {"spec": _spec(1), "run_id": "held-1"})
        assert isinstance(held, ServiceResponse)
        assert held.status_code == 202, held.body

        answer = dispatch(service, "POST", "/build", {"spec": _spec(1), "run_id": "sync-3"})

        assert isinstance(answer, ServiceResponse)
        assert answer.status_code == 429, answer.body
        assert answer.body["code"] == "build_owner_limit"
        _assert_declared(answer)
        # Turned away before anything ran: the run id is still free.
        assert not (tmp_path / "sync-3").exists()
    finally:
        service.release.set()
        service._async_builds.shutdown()


def test_a_refused_spec_records_nothing_and_leaves_the_run_id_free(tmp_path: Path) -> None:
    """What the build refuses before it runs is answered before any job is made, as the
    request thread answered it: the corrected request can use the same run id."""
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _SlowSource(0))
    try:
        refused = dispatch(service, "POST", "/build", {"spec": "not: [a spec", "run_id": "fix-me"})

        assert isinstance(refused, ServiceResponse)
        assert refused.status_code == 400, refused.body
        assert service._async_builds.get("fix-me") is None
        assert service._event_store.submission("fix-me") is None

        fixed = dispatch(service, "POST", "/build", {"spec": _spec(1), "run_id": "fix-me"})

        assert isinstance(fixed, ServiceResponse)
        assert fixed.status_code == 200, fixed.body
    finally:
        service._async_builds.shutdown()


def test_a_synchronous_build_keeps_its_admitted_slot_until_it_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _SlowSource(0)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    release = service._build_slots.release
    released_before_run: list[bool] = []

    def observed_release() -> None:
        released_before_run.append(not source.entered.is_set())
        release()

    monkeypatch.setattr(service._build_slots, "release", observed_release)
    try:
        answer = dispatch(service, "POST", "/build", {"spec": _spec(1), "run_id": "reserved"})

        assert isinstance(answer, ServiceResponse)
        assert answer.status_code == 200, answer.body
        assert released_before_run == [False]
    finally:
        service._async_builds.shutdown()


def test_a_job_cancelled_before_it_ran_answers_with_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job is cancelled while the request waits for it; the answer says so as the
    contract's 409 error does, with ``status`` and ``code``."""
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _SlowSource(0))
    original = service._run_build_job

    def cancelled_first(spec_yaml: str, run_id: str, *args: Any) -> ServiceResponse:
        service._async_builds.request_cancel(run_id)
        return original(spec_yaml, run_id, *args)

    monkeypatch.setattr(service, "_run_build_job", cancelled_first)
    try:
        answer = dispatch(service, "POST", "/build", {"spec": _spec(1), "run_id": "gone"})

        assert isinstance(answer, ServiceResponse)
        assert answer.status_code == 409
        assert answer.body["status"] == "cancelled"
        assert answer.body["code"] == "cancelled"
        _assert_declared(answer)
    finally:
        service._async_builds.shutdown()


def test_an_error_after_the_run_finished_does_not_turn_it_into_an_enqueue_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the dispatch is an enqueue failure. A failure while the finished job is
    being logged leaves its status, its events and the answer as the run made them."""
    import kpubdata_builder.service.jobs as jobs_module

    def broken_ended(*_args: object, **_kwargs: object) -> None:
        raise OSError("log stream is gone")

    monkeypatch.setattr(jobs_module.build_log, "ended", broken_ended)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _SlowSource(0))
    try:
        answer = dispatch(service, "POST", "/build", {"spec": _spec(1), "run_id": "after"})

        assert isinstance(answer, ServiceResponse)
        assert answer.status_code == 200, answer.body
        assert answer.body["status"] == "ok"
        snapshot = service._async_builds.get("after")
        assert snapshot is not None
        assert snapshot.status == "succeeded"
        events = cast(
            dict[str, Any], service.get_build_events("after", limit=500, tail=False).body
        )["events"]
        names = [e["event"] for e in events]
        assert "run_finished" in names
        assert "run_failed" not in names
        assert all(e.get("code") != "enqueue_failed" for e in events)
    finally:
        service._async_builds.shutdown()

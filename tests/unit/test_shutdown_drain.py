"""Stopping the server ends queued jobs unstarted and lets running ones finish (#1118).

On SIGTERM only the HTTP listener stopped. Queued jobs stayed queued, and each started
as soon as a running build returned its slot — after the server had stopped answering —
to be killed part-way when the container's stop timeout ran out.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service import http as http_module
from kpubdata_builder.service.providers import ProviderDescriptor

from .test_ephemeral_credentials import _ALICE, _CANARY, _SPEC, _Recorder
from .test_job_keys_at_submission import _service, _submit, _wait, multi_user  # noqa: F401

_KEY = (f"datago={_CANARY}",)


def _events(service: BuilderService, run_id: str) -> list[tuple[str, str]]:
    events = service._event_store.list_for_run(run_id, limit=50, tail=False)
    return [(event.event, event.message) for event in events]


def _status(service: BuilderService, run_id: str) -> str:
    snapshot = service._async_builds.get(run_id)
    assert snapshot is not None
    return snapshot.status


def _last_event(service: BuilderService, run_id: str, event: str) -> tuple[str, str]:
    """The run's last event, once it is ``event``.

    A job's status turns ``cancelled`` before its ``run_cancelled`` event is written —
    the registry finishes the job, then the worker calls the hook that records it — so a
    test that reads the events as soon as the status is terminal can see the one before.
    """
    for _ in range(500):
        events = _events(service, run_id)
        if events and events[-1][0] == event:
            return events[-1]
        threading.Event().wait(0.01)
    raise AssertionError(f"{run_id} did not record {event}: {_events(service, run_id)}")


def _until_running(service: BuilderService, run_id: str) -> None:
    for _ in range(500):
        if _status(service, run_id) == "running":
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"{run_id} did not start")


def test_queued_jobs_end_unstarted_and_the_running_one_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    gate = threading.Event()
    recorder = _Recorder(gate=gate)
    service = _service(tmp_path, monkeypatch, recorder)
    try:
        for run_id in ("running", "queued-1", "queued-2"):
            assert _submit(service, _SPEC, run_id, _KEY).status_code == 202
        _until_running(service, "running")

        ended = service.begin_shutdown()

        assert sorted(ended) == ["queued-1", "queued-2"]
        for run_id in ended:
            status = service.build_status(run_id).body
            assert status["status"] == "failed"
            assert str(status["error"]).startswith("interrupted:")
            assert "new run_id" in str(status["error"])
            assert _events(service, run_id)[-1][0] == "run_failed"
            assert not service._job_credentials.holds(run_id)
            assert not (tmp_path / run_id).exists()
        # The running job is not touched, and stopping twice ends nothing more.
        assert _status(service, "running") == "running"
        assert service.begin_shutdown() == ()
    finally:
        gate.set()

    assert service.drain_builds(10.0) == ((), ())
    assert _status(service, "running") == "succeeded"
    # One build ran. The two that were queued never called a provider.
    assert len(recorder.calls) == 1
    assert _status(service, "queued-1") == _status(service, "queued-2") == "failed"


def test_a_submission_during_shutdown_is_refused_and_leaves_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    recorder = _Recorder()
    service = _service(tmp_path, monkeypatch, recorder)
    service.begin_shutdown()

    refused = _submit(service, _SPEC, "late", _KEY)

    assert refused.status_code == 503
    assert refused.body["code"] == "shutting_down"
    assert service._async_builds.get("late") is None
    assert _events(service, "late") == []
    assert not service._job_credentials.holds("late")
    assert recorder.calls == []


def test_a_job_waiting_for_a_build_slot_does_not_start_when_the_slot_comes_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    """Two workers and one build slot: the second job is already on a worker thread,
    blocked on the slot. Dropping the pool's pending work does not reach it."""
    gate = threading.Event()
    recorder = _Recorder(gate=gate)
    service = BuilderService(
        output_root=tmp_path,
        client_factory=recorder,
        async_max_workers=2,
        max_concurrent_builds=1,
    )
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)
    monkeypatch.setattr(
        service._providers_service,
        "runtime_providers",
        lambda: (ProviderDescriptor("datago", True),),
    )
    try:
        assert _submit(service, _SPEC, "running", _KEY).status_code == 202
        _until_running(service, "running")
        assert _submit(service, _SPEC, "waiting", _KEY).status_code == 202

        assert service.begin_shutdown() == ("waiting",)
    finally:
        gate.set()

    assert service.drain_builds(10.0) == ((), ())
    assert _wait(service, "running") == "succeeded"
    assert _status(service, "waiting") == "failed"
    assert len(recorder.calls) == 1
    # The slot the waiting worker took was given straight back.
    assert service._build_slots is not None
    assert service._build_slots.acquire(timeout=5.0)
    service._build_slots.release()


def test_a_build_still_running_after_the_grace_is_asked_to_stop_and_says_why(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    gate = threading.Event()
    service = _service(tmp_path, monkeypatch, _Recorder(gate=gate))
    try:
        assert _submit(service, _SPEC, "slow", _KEY).status_code == 202
        _until_running(service, "slow")
        service.begin_shutdown()

        cancelled, still_running = service.drain_builds(0.05, 0.1)

        # It is inside a fetch and cannot stop yet: asked, and still there.
        assert cancelled == ("slow",)
        assert still_running == ("slow",)
        assert _status(service, "slow") == "cancelling"
    finally:
        gate.set()

    assert _wait(service, "slow") == "cancelled"
    _event, message = _last_event(service, "slow", "run_cancelled")
    assert "shutting down" in message and "new run_id" in message
    assert not service._job_credentials.holds("slow")


def test_a_users_own_cancel_keeps_its_plain_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    """Negative: only a build the shutdown stopped says the server was shutting down."""
    gate = threading.Event()
    service = _service(tmp_path, monkeypatch, _Recorder(gate=gate))
    try:
        assert _submit(service, _SPEC, "mine", _KEY).status_code == 202
        _until_running(service, "mine")
        service.cancel_build("mine")
    finally:
        gate.set()

    assert _wait(service, "mine") == "cancelled"
    assert _last_event(service, "mine", "run_cancelled") == (
        "run_cancelled",
        "build cancelled at a safe stage boundary",
    )


def test_stopping_an_idle_service_ends_nothing_and_does_not_wait(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert service.begin_shutdown() == ()
    assert service.drain_builds(60.0) == ((), ())


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [(None, 90.0), ("", 90.0), ("  ", 90.0), ("30", 30.0), ("0", 0.0), ("1.5", 1.5)],
)
def test_the_grace_period_setting(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, seconds: float
) -> None:
    if raw is None:
        monkeypatch.delenv(http_module.SHUTDOWN_GRACE_ENV, raising=False)
    else:
        monkeypatch.setenv(http_module.SHUTDOWN_GRACE_ENV, raw)

    assert http_module.shutdown_grace_seconds() == seconds


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "-5", "soon", "90s"])
def test_a_grace_period_that_cannot_be_used_is_refused(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """``inf`` would wait for the builds without end; a typo would silently become 90."""
    monkeypatch.setenv(http_module.SHUTDOWN_GRACE_ENV, raw)

    with pytest.raises(RuntimeError, match="finite number >= 0"):
        http_module.shutdown_grace_seconds()


def test_serve_refuses_to_start_with_such_a_grace_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the start, not when the stop comes: nothing is bound and no job is touched."""
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    monkeypatch.setenv(http_module.SHUTDOWN_GRACE_ENV, "inf")
    service = BuilderService(output_root=tmp_path, client_factory=_Recorder())

    with pytest.raises(RuntimeError, match=http_module.SHUTDOWN_GRACE_ENV):
        http_module.serve(service, port=0)

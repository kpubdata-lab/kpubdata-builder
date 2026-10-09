"""An async build that runs too long is stopped and says why (#1119).

Nothing bounded a build's running time, so a job that never finished held its build
slot for good. ``KPUBDATA_BUILDER_BUILD_TIME_LIMIT_SECONDS`` now asks a job to stop
once it has run that long, counted from when it left the queue; the pipeline honours
the request at its next safe boundary, as it does a user's cancel.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.build_limits import BUILD_TIME_LIMIT_ENV, resolve_build_time_limit
from kpubdata_builder.service.build_runs_api import TIME_LIMIT_CANCELLED_MESSAGE
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.spec import JsonValue

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

    def list(self, **params: object) -> _Result:
        self.calls += 1
        time.sleep(self.delay)
        return _Result([{"nx": cast(JsonValue, params.get("nx")), "v": 1}])

    def dataset(self, _key: str) -> _SlowSource:
        return self


def _wait(service: BuilderService, run_id: str, seconds: float = 20) -> dict[str, Any]:
    pause = threading.Event()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        body = cast(dict[str, Any], service.build_status(run_id).body)
        if body.get("status") in ("succeeded", "failed", "cancelled"):
            return body
        pause.wait(0.02)
    raise AssertionError(f"{run_id} did not end")


def _cancel_messages(service: BuilderService, run_id: str, *, expect: bool = True) -> list[str]:
    """The run's ``run_cancelled`` messages; the event is appended just after the job ends."""
    pause = threading.Event()
    for _ in range(100 if expect else 1):
        events = cast(dict[str, Any], service.get_build_events(run_id, limit=500, tail=False).body)[
            "events"
        ]
        found = [e["message"] for e in events if e["event"] == "run_cancelled"]
        if found:
            return found
        pause.wait(0.02)
    return []


def test_a_build_past_its_limit_is_cancelled_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "0.3")
    source = _SlowSource(delay=0.05)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)

    assert service.submit_build(_spec(), run_id="slow").status_code == 202
    job = _wait(service, "slow")

    assert job["status"] == "cancelled"
    # It stopped at a combination boundary, long before the grid was done.
    assert source.calls < _COMBINATIONS
    assert _cancel_messages(service, "slow") == [TIME_LIMIT_CANCELLED_MESSAGE]


def test_a_timer_that_fires_after_the_build_ended_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runner returns at once; the limit passes afterwards. The timer is stopped when
    the runner returns, so the finished job cannot be turned into a cancelled one."""
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "0.2")
    executor = AsyncBuildExecutor(max_workers=1)
    done = threading.Event()

    def runner(*_args: object) -> ServiceResponse:
        done.set()
        return ServiceResponse(200, {"status": "ok"})

    try:
        assert (
            executor.submit(spec_yaml="s", run_id="r", created_by=None, runner=runner).status
            == "accepted"
        )
        assert done.wait(5)
        pause = threading.Event()
        while (snapshot := executor.get("r")) is None or snapshot.status == "running":
            pause.wait(0.01)
        pause.wait(0.4)

        final = executor.get("r")
        assert final is not None and final.status == "succeeded"
        assert not executor.ran_past_time_limit("r")
    finally:
        executor.shutdown()


def _blocked_job(executor: AsyncBuildExecutor, release: threading.Event) -> None:
    """Submit a job whose runner waits for ``release``, and wait until it is running."""
    started = threading.Event()

    def runner(*_args: object) -> ServiceResponse:
        started.set()
        release.wait(5)
        return ServiceResponse(409, {"error": "cancelled"})

    assert (
        executor.submit(spec_yaml="s", run_id="r", created_by=None, runner=runner).status
        == "accepted"
    )
    assert started.wait(5)


def _status_becomes(executor: AsyncBuildExecutor, status: str) -> None:
    pause = threading.Event()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = executor.get("r")
        if snapshot is not None and snapshot.status == status:
            return
        pause.wait(0.01)
    raise AssertionError(f"the job did not become {status}")


def test_a_job_past_its_limit_reads_cancelling_until_it_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Between the limit and the next safe boundary the job is being cancelled, and says
    so, as it does after a user's cancel — not ``running``."""
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "0.05")
    executor = AsyncBuildExecutor(max_workers=1)
    release = threading.Event()
    try:
        _blocked_job(executor, release)

        _status_becomes(executor, "cancelling")
        assert executor.ran_past_time_limit("r")

        release.set()
        _status_becomes(executor, "cancelled")
    finally:
        release.set()
        executor.shutdown()


def test_a_limit_reached_after_a_user_cancel_does_not_claim_the_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user asked first and the job has not reached a boundary yet when the limit
    passes: the cancellation stays the user's."""
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "60")
    executor = AsyncBuildExecutor(max_workers=1)
    release = threading.Event()
    try:
        _blocked_job(executor, release)
        assert executor.request_cancel("r")[0] == "cancelling"

        executor._expire("r")  # the timer's callback, without waiting a minute for it

        assert not executor.ran_past_time_limit("r")
    finally:
        release.set()
        executor.shutdown()


def test_zero_turns_the_limit_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "0")
    source = _SlowSource(delay=0.01)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)

    assert service.submit_build(_spec(combinations=3), run_id="r").status_code == 202

    assert _wait(service, "r")["status"] == "succeeded"
    assert resolve_build_time_limit() is None


def test_a_user_cancel_keeps_its_own_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "60")
    source = _SlowSource(delay=0.05)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    assert service.submit_build(_spec(), run_id="mine").status_code == 202
    started = threading.Event()
    while source.calls == 0:
        started.wait(0.01)

    service.cancel_build("mine")

    assert _wait(service, "mine")["status"] == "cancelled"
    assert _cancel_messages(service, "mine") == ["build cancelled at a safe stage boundary"]


@pytest.mark.parametrize(
    ("raw", "limit"),
    [
        ("", 21600.0),
        ("90", 90.0),
        ("0", None),
        ("-5", 21600.0),
        ("inf", 21600.0),
        ("x", 21600.0),
        ("1e30", threading.TIMEOUT_MAX),
    ],
)
def test_the_setting_is_read_with_a_default(
    raw: str, limit: float | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, raw)

    assert resolve_build_time_limit() == limit

"""A cancelled run and a run a restart interrupted say why, where an admin reads it (#1120).

``test_run_failure_record.py`` covers a source, a composition and a table commit that
fail. Two endings were left out and are covered here:

- a **cancelled** run. Its partial manifest said ``cancelled`` and no more; it now has
  a ``failures`` entry with the stage it stopped at and why it was cancelled;
- a run that **left no manifest**: cancelled while queued, interrupted by a restart,
  still queued at a shutdown. It was in the event store only and in no list. Its ending
  is now recorded with the events and copied into the build index.

Both reach ``GET /admin/runs`` as one line of fixed phrases, and both survive a rebuild
of the index. A canary placed in the provider key, the spec and a provider's error is
in none of them, nor in a log line written on the way.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

import pytest

from kpubdata_builder.events.store import BuildEventStore, events_store_path, read_run_endings
from kpubdata_builder.manifest.endings import (
    RUN_ENDING_CODES,
    RUN_ENDING_STAGES,
    run_ending_summary,
)
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.build_limits import BUILD_TIME_LIMIT_ENV
from kpubdata_builder.service.providers import ProviderDescriptor
from kpubdata_builder.service.routes import admin
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.build_index import rebuild_index

_CANARY = "canary-secret-1120"
_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")
_ADMIN = Principal(kind="oidc", identifier="admin123", owner_id="oidc:admin", is_admin=True)

_CANCELLED_AT_BRONZE = "the run was cancelled on request at the bronze stage"
_CANCELLED_QUEUED = "the run was cancelled on request before it started"
_INTERRUPTED_AT_BRONZE = "the server restarted, and the run was interrupted at the bronze stage"


def _spec(*, composition: bool = False, title: str = "Air") -> str:
    second = "  - provider: datago\n    dataset: air_station\n    alias: b\n" if composition else ""
    join = (
        "composition:\n  name: combined\n  join: {left: m, right: b, left_key: id, right_key: id}\n"
        if composition
        else ""
    )
    return (
        f"dataset_id: air\ntitle: {title}\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: m\n"
        + second
        + join
        + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    """A provider that says when a fetch began and holds it until ``gate`` is set."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.fetching = threading.Event()

    def list(self, **_params: object) -> _Result:
        self.fetching.set()
        if self.gate is not None:
            self.gate.wait(timeout=20)
        return _Result([{"id": "1", "v": 1}])

    def dataset(self, _key: str) -> _Source:
        return self


def _factory(source: _Source) -> Callable[..., object]:
    """A client factory with the keywords the service asks one for (#683, #786)."""

    def create(
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> object:
        return source

    return create


class _CancelAt:
    """A probe that reports a cancellation from its ``after``-th check on.

    ``after=None`` never reports one at a stage boundary and refuses the run its
    normal ending: the request that arrives after the last source and before the run
    is made final.
    """

    def __init__(self, *, after: int | None) -> None:
        self._after = after
        self._checks = 0

    def cancel_requested(self) -> bool:
        self._checks += 1
        return self._after is not None and self._checks > self._after

    def commit(self) -> bool:
        return False


def _manifest(tmp_path: Path, run_id: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((tmp_path / run_id / "manifest.json").read_text()))


def _admin_rows(service: BuilderService) -> dict[str, dict[str, Any]]:
    response = admin.route(service, "GET", "/admin/runs", None, "", _ADMIN)
    assert response is not None and response.status_code == 200, response
    runs = cast(list[dict[str, Any]], cast(dict[str, Any], response.body)["runs"])
    return {run["run_id"]: run for run in runs}


def _indexed(service: BuilderService) -> dict[str, tuple[str, str | None]]:
    return {
        entry.run_id: (entry.status, entry.error)
        for entry in service._build_index.list_builds(limit=None)
    }


def _ending_rows(tmp_path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(events_store_path(tmp_path)) as conn:
        return conn.execute(
            "SELECT run_id, status, stage, code, summary FROM run_endings ORDER BY run_id"
        ).fetchall()


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


def _until(done: Callable[[], bool], *, seconds: float = 20) -> None:
    """Wait until ``done`` says so; fail when it never does."""
    pause = threading.Event()
    deadline = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < deadline, "the awaited state was never reached"
        pause.wait(0.02)


def _stop(*services: BuilderService) -> None:
    for service in services:
        service._async_builds._executor.shutdown(wait=True)


# ---------------------------------------------------------------------------
# a cancelled run that wrote a partial manifest
# ---------------------------------------------------------------------------


def test_a_run_cancelled_at_a_stage_boundary_records_the_stage_and_why(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())

    response = service.build(_spec(), run_id="r1", cancellation=_CancelAt(after=0))

    assert response.body["status"] == "cancelled"
    manifest = _manifest(tmp_path, "r1")
    assert manifest["status"] == "cancelled"
    assert manifest["failures"] == [
        {
            "source_key": "m",
            "stage": "bronze",
            "code": "cancelled",
            "summary": _CANCELLED_AT_BRONZE,
        }
    ]
    line = f"m: {_CANCELLED_AT_BRONZE}"
    assert _indexed(service)["r1"] == ("cancelled", line)
    row = _admin_rows(service)["r1"]
    assert (row["status"], row["error"]) == ("cancelled", line)


def test_a_later_boundary_names_the_stage_the_source_had_not_completed(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())
    stages: set[str] = set()

    for after in range(1, 8):
        run_id = f"r{after}"
        service.build(_spec(), run_id=run_id, cancellation=_CancelAt(after=after))
        (failure,) = _manifest(tmp_path, run_id)["failures"]
        done = _manifest(tmp_path, run_id)["outputs"]
        assert failure["code"] == "cancelled"
        assert failure["summary"] == run_ending_summary("cancelled", failure["stage"])
        # The stage recorded is one the run wrote nothing for.
        assert not any(f"/{failure['stage']}/" in str(path) for path in done), (after, done)
        stages.add(failure["stage"])

    assert stages == {"silver", "gold", "export", "warehouse"}


def test_a_cancellation_after_the_last_source_names_what_did_not_run(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())

    service.build(_spec(), run_id="plain", cancellation=_CancelAt(after=None))
    service.build(_spec(composition=True), run_id="joined", cancellation=_CancelAt(after=None))

    (plain,) = _manifest(tmp_path, "plain")["failures"]
    assert (plain["source_key"], plain["stage"], plain["code"]) == ("m", "warehouse", "cancelled")
    assert plain["summary"] == (
        "the run was cancelled on request before the run's results were committed"
    )
    (joined,) = _manifest(tmp_path, "joined")["failures"]
    assert (joined["source_key"], joined["stage"]) == ("combined", "composition")
    assert joined["summary"] == "the run was cancelled on request before the composition ran"
    assert _manifest(tmp_path, "joined")["status"] == "cancelled"


def test_a_run_that_was_not_cancelled_has_no_cancellation_recorded(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())

    assert service.build(_spec(), run_id="r1").status_code == 200

    assert "failures" not in _manifest(tmp_path, "r1")
    assert _indexed(service)["r1"] == ("ok", None)
    assert not events_store_path(tmp_path).exists() or _ending_rows(tmp_path) == []


def test_a_build_past_its_time_limit_records_that_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_TIME_LIMIT_ENV, "0.2")
    gate = threading.Event()
    source = _Source(gate)
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    try:
        assert service.submit_build(_spec(), run_id="slow").status_code == 202
        # The limit asks the job to stop while its fetch is held, or before it began;
        # released, the pipeline stops at its next boundary.
        _until(lambda: service.build_status("slow").body["status"] != "running")
        gate.set()
        _until(lambda: service.build_status("slow").body["status"] == "cancelled")
        (failure,) = _manifest(tmp_path, "slow")["failures"]
    finally:
        gate.set()
        _stop(service)

    assert failure["code"] == "time_limit_exceeded"
    assert failure["summary"] == run_ending_summary("time_limit_exceeded", failure["stage"])
    assert _indexed(service)["slow"] == ("cancelled", f"m: {failure['summary']}")


# ---------------------------------------------------------------------------
# a run that left no manifest
# ---------------------------------------------------------------------------


@pytest.fixture()
def queued_behind(tmp_path: Path) -> Iterator[tuple[BuilderService, threading.Event]]:
    """A one-worker service whose worker is held in a fetch, so the next job queues."""
    gate = threading.Event()
    source = _Source(gate)
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: source, async_max_workers=1
    )
    assert service.submit_build(_spec(), run_id="running").status_code == 202
    assert source.fetching.wait(10)
    try:
        yield service, gate
    finally:
        gate.set()
        _stop(service)


def test_a_job_cancelled_while_queued_is_recorded_and_listed(
    tmp_path: Path, queued_behind: tuple[BuilderService, threading.Event]
) -> None:
    service, _gate = queued_behind
    assert service.submit_build(_spec(), run_id="waiting").status_code == 202

    assert service.cancel_build("waiting").body["status"] == "cancelled"

    assert _ending_rows(tmp_path) == [
        ("waiting", "cancelled", "queued", "cancelled", _CANCELLED_QUEUED)
    ]
    assert _indexed(service)["waiting"] == ("cancelled", _CANCELLED_QUEUED)
    row = _admin_rows(service)["waiting"]
    assert (row["status"], row["error"]) == ("cancelled", _CANCELLED_QUEUED)
    assert not (tmp_path / "waiting").exists()


def test_the_cancelled_jobs_own_id_is_still_answered_with_the_job(
    queued_behind: tuple[BuilderService, threading.Event],
) -> None:
    """The run is in the index now, and is not a completed run: submitting its id again
    hands the job back, as it did, instead of ``run_id_completed``."""
    service, _gate = queued_behind
    assert service.submit_build(_spec(), run_id="waiting").status_code == 202
    service.cancel_build("waiting")

    again = service.submit_build(_spec(), run_id="waiting")

    assert again.status_code == 200
    assert again.body["status"] == "cancelled"


def test_jobs_still_queued_at_a_shutdown_are_recorded(
    tmp_path: Path, queued_behind: tuple[BuilderService, threading.Event]
) -> None:
    service, _gate = queued_behind
    assert service.submit_build(_spec(), run_id="waiting").status_code == 202

    assert service.begin_shutdown() == ("waiting",)

    summary = "the server was shutting down, and the run was stopped before it started"
    assert _ending_rows(tmp_path) == [("waiting", "failed", "queued", "server_shutdown", summary)]
    assert _indexed(service)["waiting"] == ("failed", summary)


def test_a_job_whose_keys_were_gone_when_it_could_start_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    gate = threading.Event()
    source = _Source(gate)
    service = BuilderService(
        output_root=tmp_path, client_factory=_factory(source), async_max_workers=1
    )
    _as(monkeypatch, _ALICE)
    monkeypatch.setattr(
        service._providers_service,
        "runtime_providers",
        lambda: (ProviderDescriptor("datago", True),),
    )
    try:
        # The one worker is held by job-1, so job-2 waits in the queue with its key.
        for run_id in ("job-1", "job-2"):
            body: dict[str, JsonValue] = {"spec": _spec(), "run_id": run_id}
            assert _request(service, "POST", "/builds", body, key="k").status_code == 202
        assert source.fetching.wait(10)
        # What its lifetime passing does: the key is dropped before the job starts.
        service._job_credentials.discard("job-2")
        gate.set()
        _until(lambda: "job-2" in _indexed(service))
    finally:
        gate.set()
        _stop(service)

    summary = "the run's provider keys were no longer held when it could start"
    assert _ending_rows(tmp_path) == [
        ("job-2", "failed", "queued", "credentials_required", summary)
    ]
    assert _indexed(service)["job-2"] == ("failed", summary)


def test_a_job_that_could_not_be_queued_is_recorded_without_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(f"worker pool rejected {_CANARY}")

    monkeypatch.setattr(service._async_builds._executor, "submit", refuse)

    assert service.submit_build(_spec(), run_id="r1").status_code == 500

    summary = "the run could not be queued for execution"
    assert _ending_rows(tmp_path) == [("r1", "failed", "queued", "enqueue_failed", summary)]
    assert _indexed(service)["r1"] == ("failed", summary)
    assert _admin_rows(service)["r1"]["error"] == summary
    assert _CANARY not in str(_admin_rows(service))


@pytest.fixture()
def restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> Iterator[BuilderService]:
    """A service started after another was stopped with Alice's job in a fetch.

    The first service's worker stays held until the test is over: a real restart
    kills it, and letting it run on would add a manifest behind the test.
    """
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()
    source = _Source(gate)
    first = BuilderService(output_root=tmp_path, client_factory=_factory(source))
    _as(monkeypatch, _ALICE)
    submitted = _request(
        first,
        "POST",
        "/builds",
        {"spec": _spec(title=f"Air {_CANARY}"), "run_id": "in-flight"},
        key=_CANARY,
    )
    assert submitted.status_code == 202, submitted.body
    assert source.fetching.wait(10)

    second = BuilderService(output_root=tmp_path, client_factory=_factory(_Source()))
    assert second.mark_interrupted_runs() == ("in-flight",)
    try:
        yield second
    finally:
        gate.set()
        _stop(first, second)


def test_a_run_a_restart_interrupted_is_recorded_with_the_stage_it_had_reached(
    tmp_path: Path, restarted: BuilderService
) -> None:
    assert _ending_rows(tmp_path) == [
        ("in-flight", "failed", "bronze", "interrupted", _INTERRUPTED_AT_BRONZE)
    ]
    assert _indexed(restarted)["in-flight"] == ("failed", _INTERRUPTED_AT_BRONZE)
    row = _admin_rows(restarted)["in-flight"]
    assert row["status"] == "failed"
    assert row["error"] == _INTERRUPTED_AT_BRONZE
    assert row["owner_id"] == _ALICE.owner_id
    assert row["finished_at"] is not None


def test_the_owner_still_reads_the_same_status_and_only_the_owner_lists_the_run(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)
    status = _request(restarted, "GET", "/builds/in-flight")
    assert status.status_code == 200
    assert (status.body["status"], status.body["code"]) == ("failed", "credentials_required")
    assert str(status.body["error"]).startswith("credentials_required: the server restarted")
    mine = cast(list[dict[str, Any]], _request(restarted, "GET", "/builds").body["builds"])
    assert [(build["run_id"], build["status"]) for build in mine] == [("in-flight", "failed")]
    assert "error" not in mine[0]
    again = _request(
        restarted, "POST", "/builds", {"spec": _spec(), "run_id": "in-flight"}, key="k"
    )
    assert (again.status_code, again.body["code"]) == (409, "run_id_ended")

    _as(monkeypatch, _BOB)
    assert _request(restarted, "GET", "/builds").body["builds"] == []
    assert _request(restarted, "GET", "/builds/in-flight").status_code == 404
    assert _request(restarted, "GET", "/admin/runs").status_code == 403


def test_a_second_restart_does_not_record_the_run_again(
    tmp_path: Path, restarted: BuilderService
) -> None:
    third = BuilderService(output_root=tmp_path, client_factory=_factory(_Source()))
    try:
        assert third.mark_interrupted_runs() == ()
    finally:
        _stop(third)

    assert len(_ending_rows(tmp_path)) == 1


def test_the_endings_survive_a_rebuild_of_the_index(
    tmp_path: Path, queued_behind: tuple[BuilderService, threading.Event]
) -> None:
    service, gate = queued_behind
    assert service.submit_build(_spec(), run_id="waiting").status_code == 202
    service.cancel_build("waiting")
    gate.set()
    _until(lambda: "running" in _indexed(service))
    service.build(_spec(), run_id="stopped", cancellation=_CancelAt(after=0))
    before = _indexed(service)
    assert before["waiting"] == ("cancelled", _CANCELLED_QUEUED)
    assert before["stopped"] == ("cancelled", f"m: {_CANCELLED_AT_BRONZE}")
    assert before["running"] == ("ok", None)
    service._build_index.close()
    (tmp_path / "_builds.sqlite").unlink()

    assert rebuild_index(tmp_path) == 3

    rebuilt = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())
    try:
        assert _indexed(rebuilt) == before
        rows = _admin_rows(rebuilt)
        assert rows["waiting"]["error"] == _CANCELLED_QUEUED
        assert rows["stopped"]["error"] == f"m: {_CANCELLED_AT_BRONZE}"
    finally:
        _stop(rebuilt)


def test_a_manifest_wins_over_a_recorded_ending_in_a_rebuild(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())
    assert service.build(_spec(), run_id="r1").status_code == 200
    store = BuildEventStore(tmp_path)
    assert store.record_ending(
        "r1",
        status="failed",
        stage="queued",
        code="interrupted",
        summary=run_ending_summary("interrupted", "queued"),
        at=datetime.now(tz=timezone.utc),
    )
    store.close()
    service._build_index.close()

    assert rebuild_index(tmp_path) == 1

    rebuilt = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source())
    assert _indexed(rebuilt)["r1"] == ("ok", None)


def test_an_event_store_from_before_the_record_rebuilds_as_it_did(tmp_path: Path) -> None:
    """A store an earlier release wrote has no ``run_endings`` table. The read-only
    reader finds nothing in it and leaves it as it was."""
    path = events_store_path(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (1)")
    before = path.read_bytes()

    assert read_run_endings(tmp_path) == ()
    assert rebuild_index(tmp_path) == 0

    assert path.read_bytes() == before


def test_the_first_recorded_ending_of_a_run_stays(tmp_path: Path) -> None:
    store = BuildEventStore(tmp_path)
    now = datetime.now(tz=timezone.utc)
    first = {"status": "cancelled", "stage": "queued", "code": "cancelled", "summary": "a"}
    second = {"status": "failed", "stage": "bronze", "code": "interrupted", "summary": "b"}

    assert store.record_ending("r1", at=now, **first) is True
    assert store.record_ending("r1", at=now, **second) is False

    ending = store.ending("r1")
    assert ending is not None
    assert (ending.status, ending.code, ending.summary) == ("cancelled", "cancelled", "a")
    assert ending.owner_id is None
    store.close()


# ---------------------------------------------------------------------------
# the sentences, and what does not reach them
# ---------------------------------------------------------------------------


def test_every_code_and_stage_has_a_sentence_of_its_own() -> None:
    sentences = {
        (code, stage): run_ending_summary(code, stage)
        for code in sorted(RUN_ENDING_CODES)
        for stage in sorted(RUN_ENDING_STAGES | {"composition", "warehouse"})
    }

    assert all(sentence.startswith("the ") for sentence in sentences.values())
    assert len({run_ending_summary(code, "bronze") for code in RUN_ENDING_CODES}) == len(
        RUN_ENDING_CODES
    )
    assert len({run_ending_summary("cancelled", stage) for stage in RUN_ENDING_STAGES}) == len(
        RUN_ENDING_STAGES
    )


def test_a_value_that_is_not_in_the_vocabulary_does_not_reach_the_sentence() -> None:
    assert _CANARY not in run_ending_summary(_CANARY, "bronze")
    assert _CANARY not in run_ending_summary("cancelled", _CANARY)
    assert run_ending_summary(_CANARY, _CANARY) == "the run did not finish"


def _nowhere(canary: str, tmp_path: Path, service: BuilderService, logged: str) -> None:
    """The canary is in no recorded ending, index row, admin answer or log line."""
    assert canary not in str(_ending_rows(tmp_path))
    with sqlite3.connect(tmp_path / "_builds.sqlite") as conn:
        assert canary not in str(conn.execute("SELECT * FROM builds").fetchall())
    listed = admin.route(service, "GET", "/admin/runs", None, "", _ADMIN)
    assert listed is not None and listed.status_code == 200
    assert canary not in json.dumps(listed.body)
    assert canary not in logged


def test_a_canary_reaches_nothing_recorded_or_logged_for_an_interrupted_run(
    tmp_path: Path, restarted: BuilderService, caplog: pytest.LogCaptureFixture
) -> None:
    """The canary is the provider key and part of the spec's title. The run id, which
    the record is kept under, is not a secret."""
    # The submission and the restart happened while the fixture was set up.
    logged = "\n".join(caplog.handler.format(record) for record in caplog.get_records("setup"))
    assert "run ended without a manifest (run_id=in-flight status=failed" in logged

    _nowhere(_CANARY, tmp_path, restarted, logged)


def test_a_canary_reaches_nothing_recorded_or_logged_for_a_cancelled_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One source fails with the canary in its error, then the run is cancelled; a
    second job, carrying the canary as its key, is cancelled while it waits."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    caplog.set_level(logging.DEBUG)
    gate = threading.Event()

    class _Failing(_Source):
        def list(self, **_params: object) -> _Result:
            self.fetching.set()
            assert self.gate is not None
            self.gate.wait(timeout=20)
            raise RuntimeError(f"connect to https://x.example/?key={_CANARY} failed")

    source = _Failing(gate)
    service = BuilderService(
        output_root=tmp_path, client_factory=_factory(source), async_max_workers=1
    )
    _as(monkeypatch, _ALICE)
    spec = _spec(title=f"Air {_CANARY}")
    try:
        first = _request(service, "POST", "/builds", {"spec": spec, "run_id": "r1"}, key=_CANARY)
        assert first.status_code == 202, first.body
        assert source.fetching.wait(10)
        second = _request(service, "POST", "/builds", {"spec": spec, "run_id": "r2"}, key=_CANARY)
        assert second.status_code == 202, second.body
        assert _request(service, "POST", "/builds/r2/cancel").body["status"] == "cancelled"
        assert _request(service, "POST", "/builds/r1/cancel").body["status"] == "cancelling"
        gate.set()
        _until(lambda: "r1" in _indexed(service))
    finally:
        gate.set()
        _stop(service)

    manifest = _manifest(tmp_path, "r1")
    assert manifest["status"] == "cancelled"
    assert _CANARY not in (tmp_path / "r1" / "manifest.json").read_text()
    assert {failure["code"] for failure in manifest["failures"]} <= {"pipeline_failed", "cancelled"}
    assert _indexed(service)["r2"] == ("cancelled", _CANCELLED_QUEUED)
    assert "run ended without a manifest (run_id=r2 status=cancelled" in caplog.text
    _nowhere(_CANARY, tmp_path, service, caplog.text)

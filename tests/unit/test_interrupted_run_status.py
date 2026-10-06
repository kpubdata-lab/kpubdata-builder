"""A run a restart interrupted is readable by its owner, and only by its owner (#996).

``mark_interrupted_runs`` fails such a run in the event store alone. After the restart
the job registry is empty and no manifest exists, so the status and events routes
answered 404: a client polling its build was told the run did not exist instead of to
submit it again — under a new run id (#1042).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal

_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")
_SPEC = """\
dataset_id: interrupted.run
title: Interrupted
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


class _Result:
    items = [{"id": "1"}]


def _factory(gate: threading.Event | None = None) -> Callable[..., object]:
    """A client factory with the keywords the service asks a factory for (#683, #786).

    The fetch waits on ``gate``, so the first process's job stays in flight.
    """

    class _Dataset:
        def list(self, **_params: object) -> _Result:
            if gate is not None:
                gate.wait(timeout=10)
            return _Result()

    class _Client:
        def dataset(self, _key: str) -> _Dataset:
            return _Dataset()

    def create(
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> object:
        return _Client()

    return create


def _as(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: principal)


def _get(service: BuilderService, path: str) -> ServiceResponse:
    response = dispatch(service, "GET", path, None)
    assert isinstance(response, ServiceResponse)
    return response


@pytest.fixture()
def restarted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BuilderService]:
    """A service started after another one was stopped with Alice's job in flight.

    The first service's worker stays blocked until the test is over: a real restart
    kills it, and letting it run on would add events and a manifest behind the test.
    """
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    gate = threading.Event()
    first = BuilderService(output_root=tmp_path, client_factory=_factory(gate))
    _as(monkeypatch, _ALICE)
    submitted = dispatch(
        first,
        "POST",
        "/builds",
        {"spec": _SPEC, "run_id": "in-flight"},
        provider_key_headers=["datago=alice-key"],
    )
    assert isinstance(submitted, ServiceResponse)
    assert submitted.status_code == 202

    second = BuilderService(output_root=tmp_path, client_factory=_factory())
    assert "in-flight" in second.mark_interrupted_runs()
    try:
        yield second
    finally:
        gate.set()
        # Wait for the released worker: left running, it allocates behind whichever
        # test comes next, and one of those measures peak memory.
        first._async_builds._executor.shutdown(wait=True)
        second._async_builds._executor.shutdown(wait=True)


def test_the_owner_reads_failed_with_a_stable_code(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    response = _get(restarted, "/builds/in-flight")

    assert response.status_code == 200
    assert response.body["status"] == "failed"
    assert response.body["code"] == "credentials_required"
    assert str(response.body["error"]).startswith("credentials_required: the server restarted")
    assert response.body["run_id"] == "in-flight"
    assert response.body["created_at"] <= response.body["updated_at"]


def test_the_owner_sees_the_failure_event(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    response = _get(restarted, "/builds/in-flight/events")

    assert response.status_code == 200
    events = cast(list[dict[str, object]], response.body["events"])
    names = [event["event"] for event in events]
    assert names[0] == "run_submitted"
    assert "run_failed" in names


@pytest.mark.parametrize("path", ["/builds/in-flight", "/builds/in-flight/events"])
def test_another_user_gets_what_a_missing_run_gets(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _as(monkeypatch, _BOB)

    theirs = _get(restarted, path)
    missing = _get(restarted, path.replace("in-flight", "never-existed"))

    assert theirs.status_code == missing.status_code == 404
    assert "credentials_required" not in str(theirs.body)


def test_another_user_cannot_build_under_the_interrupted_run_id(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The id has no manifest and no registry entry, but it is not free (#1025): a
    # build under it would read Alice's events, and could leave its failure to her.
    _as(monkeypatch, _BOB)

    response = dispatch(restarted, "POST", "/build", {"spec": _SPEC, "run_id": "in-flight"})

    assert isinstance(response, ServiceResponse)
    assert response.status_code == 403
    assert response.body == {"error": "forbidden: not run owner"}
    submission = restarted._event_store.submission("in-flight")
    assert submission is not None
    assert submission.owner_id == _ALICE.owner_id


def _submit(service: BuilderService, run_id: str) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        "/builds",
        {"spec": _SPEC, "run_id": run_id},
        provider_key_headers=["datago=key"],
    )
    assert isinstance(response, ServiceResponse)
    return response


def test_another_user_cannot_submit_under_the_interrupted_run_id(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The async path (#1025): #1008 routes POST /builds through the same guard. Before
    # the guard read the submission record, Bob's job was accepted, he read Alice's
    # events while it ran, and Alice would have read his failure.
    _as(monkeypatch, _BOB)

    response = _submit(restarted, "in-flight")

    assert response.status_code == 403
    assert response.body == {"error": "forbidden: not run owner"}
    assert restarted._async_builds.get("in-flight") is None
    assert _get(restarted, "/builds/in-flight/events").status_code == 404
    # No second submission was recorded on the run Alice still reads. (The whole list
    # is not compared: the first process's worker, which a real restart would have
    # killed, may still be writing its own events until it reaches the gate.)
    _as(monkeypatch, _ALICE)
    events = cast(
        list[dict[str, object]], _get(restarted, "/builds/in-flight/events").body["events"]
    )
    assert [event["event"] for event in events].count("run_submitted") == 1
    submission = restarted._event_store.submission("in-flight")
    assert submission is not None
    assert submission.owner_id == _ALICE.owner_id


_USED = {
    "error": "run_id already ended; submit the retry under a new run_id",
    "code": "run_id_ended",
    "run_id": "in-flight",
}


def test_the_submitter_cannot_submit_under_the_interrupted_run_id_either(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A run id is one attempt (#1042, kpubdata#812): a second build under it would
    # append its events after the first attempt's ``run_failed``. The retry takes a new
    # id — the answer a completed run's id already gets.
    _as(monkeypatch, _ALICE)
    before = _get(restarted, "/builds/in-flight").body

    response = _submit(restarted, "in-flight")

    assert response.status_code == 409
    assert response.body == _USED
    assert restarted._async_builds.get("in-flight") is None
    # The run still reads as it ended: failed, with the reason it was interrupted.
    after = _get(restarted, "/builds/in-flight").body
    assert (after["status"], after["code"]) == (before["status"], before["code"])
    assert (after["status"], after["code"]) == ("failed", "credentials_required")
    events = cast(
        list[dict[str, object]], _get(restarted, "/builds/in-flight/events").body["events"]
    )
    assert [event["event"] for event in events].count("run_submitted") == 1


def test_the_synchronous_route_refuses_the_submitter_the_same_way(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    response = dispatch(restarted, "POST", "/build", {"spec": _SPEC, "run_id": "in-flight"})

    assert isinstance(response, ServiceResponse)
    # 400, not 409: this route's 409 is a build response with another body (contract).
    assert response.status_code == 400
    assert response.body == _USED


def test_the_retry_goes_through_under_a_new_run_id(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    response = _submit(restarted, "in-flight-retry")

    assert response.status_code == 202
    assert response.body["run_id"] == "in-flight-retry"


def test_the_interrupted_message_asks_for_a_new_run_id(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    assert str(_get(restarted, "/builds/in-flight").body["error"]).endswith(
        "submit it again under a new run_id"
    )


def test_an_id_nobody_submitted_is_free_to_anyone(restarted: BuilderService) -> None:
    from kpubdata_builder.service.routes._guards import check_existing_run_access

    assert check_existing_run_access(restarted, "never-existed", _BOB) is None


def test_a_run_nobody_submitted_is_still_404(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    assert _get(restarted, "/builds/never-existed").status_code == 404


def test_the_submission_record_is_written_once_and_read_back(tmp_path: Path) -> None:
    import datetime as dt

    from kpubdata_builder.events import BuildEventStore

    store = BuildEventStore(tmp_path)
    at = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)

    store.record_submission("r1", owner_id="oidc:alice", created_by="alice", submitted_at=at)
    store.record_submission("r1", owner_id="oidc:mallory", created_by="mallory", submitted_at=at)

    found = store.submission("r1")
    assert found is not None
    assert (found.owner_id, found.created_by) == ("oidc:alice", "alice")
    assert found.submitted_at == "2026-10-04T12:00:00+00:00"
    assert store.submission("r2") is None
    assert store.terminal_event("r1") is None


# --- retry_of: the new attempt points at the earlier one (#1042) ---


def _submit_retry(service: BuilderService, run_id: str, retry_of: object) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        "/builds",
        {"spec": _SPEC, "run_id": run_id, "retry_of": retry_of},
        provider_key_headers=["datago=key"],
    )
    assert isinstance(response, ServiceResponse)
    return response


def _wait_for_end(service: BuilderService, run_id: str) -> dict[str, object]:
    service._async_builds._executor.shutdown(wait=True)
    return dict(_get(service, f"/builds/{run_id}").body)


def test_a_retry_names_the_run_it_retries_on_the_job_and_in_the_manifest(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    _as(monkeypatch, _ALICE)

    accepted = _submit_retry(restarted, "second-try", "in-flight")

    assert accepted.status_code == 202
    assert accepted.body["retry_of"] == "in-flight"
    ended = _wait_for_end(restarted, "second-try")
    assert ended["status"] == "succeeded"
    assert ended["retry_of"] == "in-flight"
    manifest = json.loads((tmp_path / "second-try" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["retry_of"] == "in-flight"
    # After a restart the registry is empty; the manifest still says it.
    again = BuilderService(output_root=tmp_path, client_factory=_factory())
    assert _get(again, "/builds/second-try").body["retry_of"] == "in-flight"
    # The earlier attempt is as it ended: nothing was added to it.
    earlier = _get(restarted, "/builds/in-flight").body
    assert (earlier["status"], earlier["code"]) == ("failed", "credentials_required")
    assert "retry_of" not in earlier


def test_a_run_that_retries_nothing_says_nothing(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    _as(monkeypatch, _ALICE)

    accepted = _submit(restarted, "fresh")

    assert "retry_of" not in accepted.body
    assert "retry_of" not in _wait_for_end(restarted, "fresh")
    manifest = json.loads((tmp_path / "fresh" / "manifest.json").read_text(encoding="utf-8"))
    assert "retry_of" not in manifest


def test_the_synchronous_route_records_the_link_too(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    _as(monkeypatch, _ALICE)

    response = dispatch(
        restarted,
        "POST",
        "/build",
        {"spec": _SPEC, "run_id": "sync-retry", "retry_of": "in-flight"},
        provider_key_headers=["datago=key"],
    )

    assert isinstance(response, ServiceResponse)
    assert response.status_code == 200
    manifest = json.loads((tmp_path / "sync-retry" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["retry_of"] == "in-flight"


def test_another_users_run_cannot_be_named_and_reads_as_missing(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The link is shown back, so naming a run is a way to ask about it: the answer is
    # the one reading it gives, and the same as for a run that does not exist.
    _as(monkeypatch, _BOB)

    theirs = _submit_retry(restarted, "bobs-try", "in-flight")
    missing = _submit_retry(restarted, "bobs-other-try", "never-existed")

    assert theirs.status_code == missing.status_code == 404
    assert restarted._async_builds.get("bobs-try") is None
    assert restarted._event_store.submission("bobs-try") is None


@pytest.mark.parametrize("bad", ["", "   ", 7, "../elsewhere", "a/b"])
def test_a_retry_of_that_is_not_a_run_id_is_refused(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    _as(monkeypatch, _ALICE)

    response = _submit_retry(restarted, "bad-link", bad)

    assert response.status_code == 400
    assert restarted._async_builds.get("bad-link") is None


def test_a_run_cannot_retry_itself(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    response = _submit_retry(restarted, "loop", "loop")

    assert response.status_code == 400
    assert response.body == {"error": "'retry_of' must name another run"}


def test_a_null_retry_of_is_the_same_as_none(
    restarted: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _ALICE)

    accepted = _submit_retry(restarted, "explicit-null", None)

    assert accepted.status_code == 202
    assert "retry_of" not in accepted.body


def test_a_store_made_before_the_column_gains_it_and_keeps_its_rows(tmp_path: Path) -> None:
    import datetime as dt
    import sqlite3

    from kpubdata_builder.events import BuildEventStore

    store = BuildEventStore(tmp_path)
    at = dt.datetime(2026, 10, 4, 12, 0, tzinfo=dt.timezone.utc)
    store.record_submission("old", owner_id="oidc:alice", created_by="alice", submitted_at=at)
    database = tmp_path / "_build_events.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE run_submissions DROP COLUMN retry_of")

    reopened = BuildEventStore(tmp_path)
    reopened.record_submission(
        "new", owner_id="oidc:alice", created_by="alice", submitted_at=at, retry_of="old"
    )

    old, new = reopened.submission("old"), reopened.submission("new")
    assert old is not None and new is not None
    assert (old.owner_id, old.retry_of) == ("oidc:alice", None)
    assert new.retry_of == "old"

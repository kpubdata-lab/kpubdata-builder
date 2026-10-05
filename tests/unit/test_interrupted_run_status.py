"""A run a restart interrupted is readable by its owner, and only by its owner (#996).

``mark_interrupted_runs`` fails such a run in the event store alone. After the restart
the job registry is empty and no manifest exists, so the status and events routes
answered 404: a client polling its build was told the run did not exist instead of to
submit it again.
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


def test_the_submitter_may_use_the_interrupted_run_id_again(restarted: BuilderService) -> None:
    from kpubdata_builder.service.routes._guards import check_existing_run_access

    assert check_existing_run_access(restarted, "in-flight", _ALICE) is None


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

"""A build may retry only a run that has ended (#1103).

``check_retry_of`` asked whether the caller could read the named run and nothing about
its state. A retry takes over that run's checkpoint (#1071), so a retry of a run still
being written copied files their writer was appending to, and two builds of one spec
spent the provider's quota side by side.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV

from .test_param_grid_checkpoint import _SPEC, _Client

_ALICE = Principal(kind="oidc", identifier="alice", owner_id="oidc:alice")
_BOB = Principal(kind="oidc", identifier="bob", owner_id="oidc:bob")


class _Held(BuilderService):
    """A service whose job ``first`` stops where the test says, then ends as told."""

    def __init__(self, output_root: Path, *, ends: int = 500, write_manifest: bool = False) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=lambda **_: _Client(),
            async_max_workers=1,
            async_max_queue_size=50,
        )
        self.entered = threading.Event()
        self.release = threading.Event()
        self._ends = ends
        self._write_manifest = write_manifest

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        if run_id != "first":
            # An accepted retry: what it builds is not what these tests are about.
            return ServiceResponse(200, {"status": "ok", "run_id": run_id})
        if self._write_manifest:
            # The window a finishing job is in: its manifest is on disk, it is not done.
            run_dir = self._output_root / run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        self.entered.set()
        assert self.release.wait(timeout=10)
        return ServiceResponse(self._ends, {"error": "held job ended", "run_id": run_id})


def _post(service: BuilderService, path: str, **body: object) -> ServiceResponse:
    response = dispatch(service, "POST", path, {"spec": _SPEC, **body})
    assert isinstance(response, ServiceResponse)
    return response


def _status(service: BuilderService, run_id: str) -> str:
    return str(service.build_status(run_id).body["status"])


def _wait_for(service: BuilderService, run_id: str, *statuses: str) -> None:
    deadline = time.monotonic() + 10
    while _status(service, run_id) not in statuses:
        assert time.monotonic() < deadline, _status(service, run_id)
        time.sleep(0.01)


@pytest.fixture()
def held(tmp_path: Path) -> Iterator[_Held]:
    service = _Held(tmp_path)
    assert _post(service, "/builds", run_id="first").status_code == 202
    assert service.entered.wait(timeout=5)
    yield service
    service.release.set()


@pytest.mark.parametrize(("path", "refusal"), [("/builds", 409), ("/build", 400)])
def test_a_running_run_cannot_be_retried(
    held: _Held, tmp_path: Path, path: str, refusal: int
) -> None:
    assert _status(held, "first") == "running"

    response = _post(held, path, run_id="second", retry_of="first")

    assert response.status_code == refusal
    assert response.body == {
        "error": "the run named in 'retry_of' has not ended; wait for it or cancel it first",
        "code": "retry_of_in_progress",
        "retry_of": "first",
        "status": "running",
    }
    # Nothing was made of the refused build: no directory, no job, no recorded submission.
    assert not (tmp_path / "second").exists()
    assert held._async_builds.get("second") is None
    assert held._event_store.submission("second") is None
    assert _status(held, "first") == "running"


@pytest.mark.parametrize("path", ["/builds", "/build"])
def test_a_queued_run_cannot_be_retried(held: _Held, tmp_path: Path, path: str) -> None:
    # One worker, and it is held: the next job waits in the queue.
    assert _post(held, "/builds", run_id="waiting").status_code == 202
    assert _status(held, "waiting") == "queued"

    response = _post(held, path, run_id="second", retry_of="waiting")

    assert response.body["code"] == "retry_of_in_progress"
    assert response.body["status"] == "queued"
    assert not (tmp_path / "second").exists()


@pytest.mark.parametrize("path", ["/builds", "/build"])
def test_a_run_being_cancelled_cannot_be_retried(held: _Held, path: str) -> None:
    assert held.cancel_build("first").status_code in (200, 202)
    assert _status(held, "first") == "cancelling"

    response = _post(held, path, run_id="second", retry_of="first")

    assert response.body["code"] == "retry_of_in_progress"
    assert response.body["status"] == "cancelling"


@pytest.mark.parametrize("path", ["/builds", "/build"])
def test_the_same_request_is_accepted_once_the_run_has_ended(held: _Held, path: str) -> None:
    early = _post(held, path, run_id="early", retry_of="first")
    assert early.body["code"] == "retry_of_in_progress"

    held.release.set()
    _wait_for(held, "first", "failed")
    response = _post(held, path, run_id="second", retry_of="first")

    assert response.status_code in (200, 202), response.body
    assert response.body.get("code") != "retry_of_in_progress"


def test_every_ended_state_can_be_retried(tmp_path: Path) -> None:
    # failed (above), cancelled, succeeded, and a run a restart interrupted.
    for name in ("c", "d", "i"):
        (tmp_path / name).mkdir()
    cancelled = _Held(tmp_path / "c")
    assert _post(cancelled, "/builds", run_id="first").status_code == 202
    assert cancelled.entered.wait(timeout=5)
    cancelled.cancel_build("first")
    cancelled.release.set()
    _wait_for(cancelled, "first", "cancelled", "failed")
    assert _post(cancelled, "/builds", run_id="second", retry_of="first").status_code == 202

    done = _Held(tmp_path / "d")
    assert done.build(_SPEC, run_id="first").status_code == 200
    assert _post(done, "/builds", run_id="second", retry_of="first").status_code == 202

    interrupted = _Held(tmp_path / "i")
    assert _post(interrupted, "/builds", run_id="first").status_code == 202
    assert interrupted.entered.wait(timeout=5)
    try:
        # The process that ran it is gone: a new one has the events, and no job.
        restarted = _Held(tmp_path / "i")
        assert restarted._async_builds.get("first") is None
        assert restarted._event_store.submission("first") is not None
        assert _post(restarted, "/builds", run_id="second", retry_of="first").status_code == 202
    finally:
        interrupted.release.set()


def test_a_job_writing_its_manifest_has_not_ended(tmp_path: Path) -> None:
    """The race the order of the two reads decides: the manifest is on disk while the
    job that wrote it is still running. The file alone would say the run had ended."""
    service = _Held(tmp_path, ends=200, write_manifest=True)
    assert _post(service, "/builds", run_id="first").status_code == 202
    assert service.entered.wait(timeout=5)
    try:
        assert (tmp_path / "first" / "manifest.json").is_file()

        for path in ("/builds", "/build"):
            response = _post(service, path, run_id="second", retry_of="first")
            assert response.body["code"] == "retry_of_in_progress", path
    finally:
        service.release.set()
    _wait_for(service, "first", "succeeded")
    assert _post(service, "/builds", run_id="second", retry_of="first").status_code == 202


def test_retries_sent_while_the_run_ends_are_each_refused_or_accepted_whole(
    held: _Held, tmp_path: Path
) -> None:
    """Twenty retries cross the moment the run ends. Each is refused as in progress or
    accepted with its job; none is half made."""
    answers: dict[str, ServiceResponse] = {}

    def retry(name: str) -> None:
        answers[name] = _post(held, "/builds", run_id=name, retry_of="first")

    threads = [threading.Thread(target=retry, args=(f"retry-{n}",)) for n in range(20)]
    for index, thread in enumerate(threads):
        thread.start()
        if index == 9:
            held.release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert len(answers) == 20
    for name, response in answers.items():
        if response.status_code == 409:
            assert response.body["code"] == "retry_of_in_progress"
            assert held._async_builds.get(name) is None
            assert not (tmp_path / name).exists()
        else:
            assert response.status_code == 202, response.body
            assert held._async_builds.get(name) is not None


@pytest.mark.parametrize("path", ["/builds", "/build"])
def test_someone_else_is_not_told_the_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Ownership is asked first: another user gets the answer reading the run gives them,
    the same whether it is running or ended, so the attempt tells them nothing."""
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    service = _Held(tmp_path)
    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: _ALICE)
    assert _post(service, "/builds", run_id="first").status_code == 202
    assert service.entered.wait(timeout=5)
    try:
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: _BOB)
        while_running = _post(service, path, run_id="second", retry_of="first")
        read = dispatch(service, "GET", "/builds/first", None)
        assert isinstance(read, ServiceResponse)

        assert while_running.status_code in (403, 404)
        assert (while_running.status_code, while_running.body) == (read.status_code, read.body)
        assert "status" not in while_running.body and "code" not in while_running.body

        # Its owner is told.
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: _ALICE)
        assert _post(service, path, run_id="third", retry_of="first").body["status"] == "running"
    finally:
        service.release.set()
    _wait_for(service, "first", "failed")

    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: _BOB)
    after = _post(service, path, run_id="second", retry_of="first")
    assert (after.status_code, after.body) == (while_running.status_code, while_running.body)
    assert not (tmp_path / "second").exists()


def test_a_run_cannot_retry_itself_whatever_its_state(held: _Held) -> None:
    response = _post(held, "/builds", run_id="first", retry_of="first")

    assert response.status_code == 400
    assert response.body == {"error": "'retry_of' must name another run"}

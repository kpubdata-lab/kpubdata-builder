""" build job    (#482)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.events import BuildEvent
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import AsyncBuildJobRegistry
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


ClientFactory = Callable[[], _FakeClient]


class _ObservedBuildService(BuilderService):
    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: ClientFactory,
        completed: threading.Event,
        async_max_workers: int = 1,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=client_factory,
            async_max_workers=async_max_workers,
        )
        self._completed = completed

    def build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
        manifest_owner_id: str | None = None,
        credential_owner_id: str | None = None,
        principal: Principal | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> ServiceResponse:
        try:
            return super().build(
                spec_yaml,
                run_id=run_id,
                created_by=created_by,
                owner_id=owner_id,
                manifest_owner_id=manifest_owner_id,
                credential_owner_id=credential_owner_id,
                principal=principal,
                cancellation=cancellation,
            )
        finally:
            self._completed.set()


class _BlockingBuildService(BuilderService):
    def __init__(
        self,
        *,
        output_root: Path,
        entered: threading.Event,
        release: threading.Event,
        async_max_queue_size: int = 10,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=lambda: _FakeClient({}),
            async_max_workers=1,
            async_max_queue_size=async_max_queue_size,
        )
        self._entered = entered
        self._release = release

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        self._entered.set()
        self._release.wait(timeout=5)
        return ServiceResponse(200, {"status": "ok", "run_id": run_id})


def _service(tmp_path: Path, completed: threading.Event) -> _ObservedBuildService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    return _ObservedBuildService(
        output_root=tmp_path,
        client_factory=lambda: client,
        completed=completed,
        async_max_workers=1,
    )


class TestAsyncBuildJobs:
    def test_second_job_stays_queued_when_single_worker_is_busy(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)

        first = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert first.status_code == 202
        assert entered.wait(timeout=5)
        second = service.submit_build(VALID_SPEC_YAML, run_id="run2", created_by="tester")

        first_status = service.build_status("run1")
        second_status = service.build_status("run2")
        release.set()
        assert first_status.body["status"] == "running"
        assert second.status_code == 202
        assert second_status.body["status"] == "queued"

    def test_successful_async_build_writes_manifest_and_index(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert response.status_code == 202
        assert completed.wait(timeout=5)

        status = service.build_status("run1")
        entry = service._build_index.get("run1")
        assert status.body["status"] == "succeeded"
        assert (tmp_path / "run1" / "manifest.json").exists()
        assert entry is not None
        assert entry.status == "ok"

    def test_failed_async_build_records_failed_terminal_status(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _ObservedBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),
            completed=completed,
            async_max_workers=1,
        )

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert response.status_code == 202
        assert completed.wait(timeout=5)

        status = service.build_status("run1")
        entry = service._build_index.get("run1")
        assert status.body["status"] == "failed"
        assert entry is not None
        assert entry.status == "failed"

    def test_active_registry_is_empty_after_service_restart(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)

        restarted = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": []}),
            async_max_workers=1,
        )
        release.set()

        status = restarted.build_status("run1")
        assert status.status_code == 404

    def test_duplicate_active_run_id_returns_existing_job(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        first = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)

        second = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        release.set()

        assert first.status_code == 202
        assert second.status_code == 200
        assert second.body["run_id"] == "run1"
        assert second.body["status"] == "running"

    def test_duplicate_terminal_run_id_returns_conflict(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)
        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert response.status_code == 202
        assert completed.wait(timeout=5)

        duplicate = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        assert duplicate.status_code == 409
        assert duplicate.body["run_id"] == "run1"

    def test_post_builds_generates_run_id_when_omitted(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML})
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 202
        run_id = response.body["run_id"]
        assert isinstance(run_id, str)
        assert run_id
        assert completed.wait(timeout=5)

    def test_queue_full_returns_429(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(
            output_root=tmp_path,
            entered=entered,
            release=release,
            async_max_queue_size=1,
        )
        service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)
        queued = service.submit_build(VALID_SPEC_YAML, run_id="run2", created_by="tester")

        saturated = service.submit_build(VALID_SPEC_YAML, run_id="run3", created_by="tester")
        release.set()

        assert queued.status_code == 202
        assert saturated.status_code == 429

    def test_unsafe_run_id_is_rejected_before_job_creation(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = dispatch(
            service,
            "POST",
            "/builds",
            {"spec": VALID_SPEC_YAML, "run_id": "../bad"},
        )

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 400
        assert service.build_status("bad").status_code == 404


class TestRunSubmittedEventFailure:
    """``run_submitted`` event append  job    (#496).

    job executor   ** event append, "event 
    job   "  (  C).  
     ``AsyncBuildExecutor.submit()`` ``on_accept`` hook event append
    job    —   job registry worker
    pool   .
    """

    def test_append_failure_prevents_job_from_being_queued(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)
        # lazy event store    append .
        event_store = service._event_store

        def _broken_append(event: BuildEvent) -> BuildEvent:
            raise RuntimeError("simulated event store outage")

        monkeypatch.setattr(event_store, "append", _broken_append)

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        # HTTP    — 202(accepted) .
        assert response.status_code != 202
        assert response.status_code >= 500
        # job registry/worker pool    — " 
        # "  .
        assert service.build_status("run1").status_code == 404
        #      — run   .
        assert not (tmp_path / "run1").exists()

    def test_unrelated_run_id_is_unaffected_by_a_prior_failure(self, tmp_path: Path) -> None:
        """ run_id     run_id  submission  ."""
        completed = threading.Event()
        service = _ObservedBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
            async_max_workers=1,
        )

        response = service.submit_build(VALID_SPEC_YAML, run_id="run-ok", created_by="tester")

        assert response.status_code == 202
        assert completed.wait(timeout=5)
        assert service.build_status("run-ok").body["status"] == "succeeded"


class TestExecutorEnqueueFailure:
    """``on_accept``(event append)   worker pool  
         (#496 self-review).

    ``AsyncBuildExecutor.submit()`` ``registry.create()`` job "queued"
     ** ``self._executor.submit()``  worker pool .
      event(``run_submitted``)   registry 
      ,    "queued"   phantom
    job  —       . event
    append-only  (#496 ),  job    
    ``registry.mark_failed()`` . #496 lifecycle  timeline 
       ,  ``run_failed`` vocabulary  run_id
     event   ( event type/state/API field ).
    """

    def test_enqueue_failure_leaves_registry_failed_not_phantom_queued(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        def _broken_executor_submit(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated worker pool rejection")

        monkeypatch.setattr(service._async_builds._executor, "submit", _broken_executor_submit)

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        # HTTP    — 202(accepted) .
        assert response.status_code != 202
        assert response.status_code >= 500

        # registry "queued" phantom   —  job  
        #    terminal("failed")  . HTTP(500)
        #  (failed)   " "  —  .
        status = service.build_status("run1")
        assert status.status_code == 200
        assert status.body["status"] == "failed"

        # timeline   : run_submitted(append-only 
        # )   run_failed vocabulary  event , event
        #  "  "   . chronological order
        # run_submitted -> run_failed .
        events = service._event_store.list_for_run("run1", limit=100, tail=False)
        assert [e.event for e in events] == ["run_submitted", "run_failed"]
        assert events[-1].status == "fail"
        # raw exception/stack trace event message   — bounded,
        #   message .
        assert events[-1].message == "build could not be queued for execution"
        assert "RuntimeError" not in (events[-1].message or "")
        assert "simulated worker pool rejection" not in (events[-1].message or "")

        #      — run   .
        assert not (tmp_path / "run1").exists()

    def test_resubmission_after_enqueue_failure_reports_existing_failed_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """  job   accepted(202) phantom 
         failed   ( "existing"  semantics ).
        """
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        def _broken_executor_submit(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated worker pool rejection")

        monkeypatch.setattr(service._async_builds._executor, "submit", _broken_executor_submit)
        first = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert first.status_code >= 500

        second = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        assert second.status_code == 200
        assert second.body["run_id"] == "run1"
        assert second.body["status"] == "failed"


class TestBuildJobStatusOwnership:
    """GET /builds/{run_id}   polling ownership  (#480).

          build (``response``)  ,
    events  active async job(completed run )  cross-owner
          .
    """

    def test_cross_owner_cannot_poll_active_job_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a"),
        )
        submitted = dispatch(
            service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"}
        )
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b"),
        )
        resp = dispatch(service, "GET", "/builds/run1", None)
        assert resp.status_code == 403

        release.set()

    def test_owner_and_admin_can_poll_active_job_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        assert (
            dispatch(
                service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"}
            ).status_code
            == 202
        )
        assert entered.wait(timeout=5)

        resp = dispatch(service, "GET", "/builds/run1", None)
        assert resp.status_code == 200
        assert resp.body["status"] == "running"

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="dev", owner_id="dev:local"),
        )
        admin_resp = dispatch(service, "GET", "/builds/run1", None)
        assert admin_resp.status_code == 200

        release.set()

    def test_unknown_run_status_still_404_after_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path, threading.Event())
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a"),
        )

        resp = dispatch(service, "GET", "/builds/never-submitted", None)

        assert resp.status_code == 404

    def test_unsafe_run_id_rejected_in_status_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(tmp_path, threading.Event())

        resp = dispatch(service, "GET", "/builds/..%2Fescape", None)

        assert resp.status_code == 400


class TestWorkerAlwaysReachesATerminalState:
    """runner   job  (#482).

    worker ``RuntimeError``      thread 
    . ``_finish``   job  ``running`` ,
    polling    build , queue  
    .
    """

    class _RaisingBuildService(_ObservedBuildService):
        exception: BaseException = ValueError("boom")

        def build(self, spec_yaml: str, **kwargs: object) -> ServiceResponse:
            try:
                raise type(self).exception
            finally:
                self._completed.set()

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("not a RuntimeError"),
            KeyError("missing source"),
            TypeError("bad argument"),
        ],
    )
    def test_a_non_runtime_error_still_finishes_the_job(
        self, tmp_path: Path, exc: BaseException
    ) -> None:
        completed = threading.Event()
        service = self._RaisingBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),
            completed=completed,
            async_max_workers=1,
        )
        type(service).exception = exc

        assert service.submit_build(VALID_SPEC_YAML, run_id="run1").status_code == 202
        assert completed.wait(timeout=5)

        status = _await_terminal(service, "run1")
        assert status == "failed"

    def test_the_error_message_names_the_exception_type(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """      .

         error  ``GET /builds/{run_id}``   .  
         " "         —
         SQL  .     
           ,   traceback  .
        """
        import logging

        completed = threading.Event()
        service = self._RaisingBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),
            completed=completed,
            async_max_workers=1,
        )
        type(service).exception = ValueError("spec exploded")

        with caplog.at_level(logging.ERROR):
            assert service.submit_build(VALID_SPEC_YAML, run_id="run1").status_code == 202
            assert completed.wait(timeout=5)
            _await_terminal(service, "run1")

        error = service.build_status("run1").body.get("error")
        assert isinstance(error, str)
        assert error == "internal error: ValueError"
        assert "spec exploded" not in error
        assert "spec exploded" in caplog.text

    def test_the_worker_slot_is_released_for_the_next_job(self, tmp_path: Path) -> None:
        #   job  worker     build .
        completed = threading.Event()
        service = self._RaisingBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),
            completed=completed,
            async_max_workers=1,
        )
        type(service).exception = ValueError("boom")

        service.submit_build(VALID_SPEC_YAML, run_id="run1")
        assert completed.wait(timeout=5)
        _await_terminal(service, "run1")

        completed.clear()
        assert service.submit_build(VALID_SPEC_YAML, run_id="run2").status_code == 202
        assert completed.wait(timeout=5)
        assert _await_terminal(service, "run2") == "failed"


class TestMalformedSpecYaml:
    """  YAML      ."""

    MALFORMED = "dataset_id: [unclosed\n"

    def test_synchronous_build_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path, threading.Event())

        response = service.build(self.MALFORMED, run_id="run1")

        assert response.status_code == 400

    def test_validate_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path, threading.Event())

        assert service.validate(self.MALFORMED).status_code == 400

    def test_async_build_reaches_a_terminal_state(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = service.submit_build(self.MALFORMED, run_id="run1")

        if response.status_code == 202:
            assert completed.wait(timeout=5)
            assert _await_terminal(service, "run1") in {"failed", "cancelled"}
        else:
            assert response.status_code == 400


def _await_terminal(service: BuilderService, run_id: str, timeout: float = 5.0) -> str:
    """terminal       ."""
    import time

    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        status = str(service.build_status(run_id).body.get("status", ""))
        if status in {"succeeded", "failed", "cancelled"}:
            return status
        time.sleep(0.02)
    return status


class TestSyncBuildRespectsRunOwnership:
    """ POST /build   run    (#635).

     run_id     ,  run    
     .  run_id    run     
     .  POST /builds      
    .
    """

    @staticmethod
    def _as(monkeypatch: pytest.MonkeyPatch, owner: str) -> None:
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier=owner, owner_id=f"oidc:{owner}"),
        )

    def test_a_stranger_cannot_overwrite_a_completed_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path, threading.Event())

        self._as(monkeypatch, "owner-a")
        assert (
            dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        ).status_code < 400

        self._as(monkeypatch, "owner-b")
        resp = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        assert resp.status_code == 403

    def test_the_owner_can_rebuild_their_own_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path, threading.Event())

        self._as(monkeypatch, "owner-a")
        assert (
            dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        ).status_code < 400
        resp = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run1"})

        assert resp.status_code < 400

    def test_a_new_run_id_is_not_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        #  run_id  "    "   — 404 
        # .  route       .
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path, threading.Event())
        self._as(monkeypatch, "owner-a")

        resp = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "fresh"})

        assert resp.status_code < 400

    def test_a_generated_run_id_is_not_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path, threading.Event())
        self._as(monkeypatch, "owner-a")

        resp = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML})

        assert resp.status_code < 400


class TestConcurrentSubmitOfTheSameRunId:
    """ · ·  lock scope   (#482 ).

           .  run_id  
    POST       ``on_accept``     event 
      ,      .
    """

    def test_only_one_of_two_concurrent_creates_wins(self) -> None:
        registry = AsyncBuildJobRegistry()
        outcomes: list[str] = []
        start = threading.Barrier(2)

        def _submit() -> None:
            start.wait(timeout=5)
            outcome, _ = registry.try_create(
                run_id="run1", created_by="a", owner_id=None, max_queued=10
            )
            outcomes.append(outcome)

        threads = [threading.Thread(target=_submit) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert sorted(outcomes) == ["created", "existing"]

    def test_the_queue_cap_is_not_exceeded_under_concurrency(self) -> None:
        registry = AsyncBuildJobRegistry()
        created: list[str] = []
        start = threading.Barrier(4)

        def _submit(index: int) -> None:
            start.wait(timeout=5)
            outcome, _ = registry.try_create(
                run_id=f"run{index}", created_by="a", owner_id=None, max_queued=2
            )
            if outcome == "created":
                created.append(f"run{index}")

        threads = [threading.Thread(target=_submit, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(created) == 2


class TestAcceptHookFailureLeavesNoGhostJob:
    """``on_accept``       job    .

    event store  ``run_submitted``    job  registry  ,
           .
    """

    def test_a_failed_accept_hook_discards_the_job(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        def _boom() -> None:
            raise RuntimeError("event store outage")

        with pytest.raises(RuntimeError):
            service._async_builds.submit(
                spec_yaml=VALID_SPEC_YAML,
                run_id="run-ghost",
                created_by="tester",
                owner_id=None,
                runner=lambda *_a: ServiceResponse(200, {}),
                on_accept=_boom,
            )

        assert service._async_builds.get("run-ghost") is None
        assert service._async_builds.registry.queued_count() == 0


class TestTerminalJobsDoNotAccumulateForever:
    """terminal job          .

    snapshot        ,  
    .
    """

    def _finish(self, registry: AsyncBuildJobRegistry, run_id: str) -> None:
        registry.create(run_id=run_id, created_by="a")
        registry.begin_run(run_id)
        registry.finish(run_id, response={"ok": True}, failed=False)

    def test_terminal_jobs_are_evicted_oldest_first(self) -> None:
        registry = AsyncBuildJobRegistry(max_terminal_jobs=2)

        for index in range(4):
            self._finish(registry, f"run{index}")

        assert registry.get("run0") is None
        assert registry.get("run1") is None
        assert registry.get("run2") is not None
        assert registry.get("run3") is not None

    def test_eviction_follows_completion_order_not_submission_order(self) -> None:
        """   ,   run   ."""
        registry = AsyncBuildJobRegistry(max_terminal_jobs=1)
        registry.create(run_id="slow", created_by="a")
        registry.create(run_id="fast", created_by="a")
        registry.begin_run("slow")
        registry.begin_run("fast")

        registry.finish("fast", response={}, failed=False)
        registry.finish("slow", response={}, failed=False)

        assert registry.get("fast") is None
        assert registry.get("slow") is not None

    def test_active_jobs_are_never_evicted(self) -> None:
        """     job     ."""
        registry = AsyncBuildJobRegistry(max_terminal_jobs=1)
        registry.create(run_id="running", created_by="a")
        registry.begin_run("running")

        for index in range(5):
            self._finish(registry, f"done{index}")

        snapshot = registry.get("running")
        assert snapshot is not None
        assert snapshot.status == "running"
        assert registry.cancellation("running") is not None

    def test_cancelled_and_failed_jobs_are_evicted_too(self) -> None:
        registry = AsyncBuildJobRegistry(max_terminal_jobs=1)
        registry.create(run_id="cancelled", created_by="a")
        registry.request_cancel("cancelled")
        registry.create(run_id="failed", created_by="a")
        registry.mark_failed("failed", error="nope")

        assert registry.get("cancelled") is None
        assert registry.get("failed") is not None

    def test_eviction_releases_the_cancellation_state(self) -> None:
        """snapshot   ``_cancellations``      ."""
        registry = AsyncBuildJobRegistry(max_terminal_jobs=1)
        self._finish(registry, "old")
        self._finish(registry, "new")

        assert registry.cancellation("old") is None
        assert registry.cancellation("new") is not None


class TestEvictedJobsAreStillObservable:
    """registry  terminal job     run    (#666 ).

    #666     ``GET /builds/{run_id}``   run  404 
        .  manifest    
    ,    run     " run"   
    . manifest      .
    """

    def _service_with_manifest(
        self, tmp_path: Path, run_id: str, *, status: str, created_by: str | None = None
    ) -> BuilderService:
        import json

        service = _service(tmp_path, threading.Event())
        run_dir = tmp_path / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, object] = {
            "run_id": run_id,
            "status": status,
            "started_at": "2026-09-25T00:00:00+00:00",
            "finished_at": "2026-09-25T00:05:00+00:00",
            "errors": [],
        }
        if created_by is not None:
            manifest["created_by"] = created_by
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return service

    def test_a_completed_run_is_reported_after_eviction(self, tmp_path: Path) -> None:
        service = self._service_with_manifest(tmp_path, "run-old", status="ok")

        response = service.build_status("run-old")

        assert response.status_code == 200
        assert response.body["status"] == "succeeded"
        assert response.body["run_id"] == "run-old"
        assert response.body["created_at"] == "2026-09-25T00:00:00+00:00"
        assert response.body["updated_at"] == "2026-09-25T00:05:00+00:00"

    def test_a_cancelled_run_keeps_its_status(self, tmp_path: Path) -> None:
        service = self._service_with_manifest(tmp_path, "run-cancelled", status="cancelled")

        assert service.build_status("run-cancelled").body["status"] == "cancelled"

    def test_a_failed_run_does_not_carry_its_error_text(self, tmp_path: Path) -> None:
        """manifest  error      —  /manifest  ."""
        service = self._service_with_manifest(tmp_path, "run-failed", status="failed")

        body = service.build_status("run-failed").body

        assert body["status"] == "failed"
        assert "error" not in body

    def test_the_live_registry_still_wins(self, tmp_path: Path) -> None:
        """ registry   job  manifest   registry  ."""
        service = self._service_with_manifest(tmp_path, "run-live", status="ok")
        service._async_builds.registry.create(run_id="run-live", created_by="tester")

        body = service.build_status("run-live").body

        assert body["status"] == "queued"

    def test_a_run_that_never_existed_is_still_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path, threading.Event())

        assert service.build_status("never-ran").status_code == 404


class TestIndexFailuresAreLoggedNotSwallowed:
    """BuildIndex     ,   .

    ``except Exception: pass`` . FS       
    ,     index   ·    .
       run     .
    """

    def test_the_failure_reaches_the_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        service = _service(tmp_path, threading.Event())

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("cubrid unreachable")

        service._build_index.insert_or_replace = _boom  # type: ignore[method-assign]

        with caplog.at_level(logging.ERROR):
            response = service.build(VALID_SPEC_YAML, run_id="run-index")

        #     .
        assert response.status_code < 500
        assert "cubrid unreachable" in caplog.text
        assert "build index update" in caplog.text

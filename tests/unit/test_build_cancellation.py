"""Cooperative cancellation and partial manifest validation (#481, ADR 0008).

This file validates four layers deterministically.

1. **State machine** (``AsyncBuildJobRegistry``): cancellation of queued/running,
   terminal state immutability, idempotent repeated cancellation.
2. **pipeline safety boundary**: use stub probe to mark Bronze/Silver/Gold
   boundaries exactly and verify partial artifacts and partial manifest.
3. **HTTP contract**: route/ownership/status code.
4. **race conditions**: deterministically reproduce with ``threading.Barrier``
   and explicit lock order instead of sleep.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import (
    AsyncBuildExecutor,
    AsyncBuildJobRegistry,
    RunCancellation,
)
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store import SqliteBuildIndex, rebuild_index

VALID_SPEC_YAML = (
    """
dataset_id: dataset.cancel
title: Cancel Sample
description: Cancellation fixture
sources:
  - provider: datago
    dataset: air_quality
    alias: air
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


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


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(
        output_root=tmp_path, client_factory=lambda **_: client, async_max_workers=1
    )


class _BoundaryProbe:
    """Stub probe observing cancellation at exactly ``cancel_after``-th boundary check.

    Deterministically specifies at which **boundary** (e.g., "after Bronze",
    "after Silver") cancellation is observed without depending on sleep or
    timing. If ``cancel_after=None``, no cancellation is requested at all
    (for normal path regression).
    """

    def __init__(self, *, cancel_after: int | None) -> None:
        self._cancel_after = cancel_after
        self.probe_count = 0
        self.committed = False
        self._lock = threading.Lock()

    def cancel_requested(self) -> bool:
        with self._lock:
            self.probe_count += 1
            if self._cancel_after is None:
                return False
            return self.probe_count > self._cancel_after

    def commit(self) -> bool:
        # Last safety boundary. If ``cancel_requested()`` called once more now, True would
        # appear (``probe_count >= cancel_after``) so cancellation is considered won —
        # same as real ``RunCancellation`` situation of "request arrived before commit".
        with self._lock:
            if self._cancel_after is not None and self.probe_count >= self._cancel_after:
                return False
            self.committed = True
            return True


def _read_manifest(tmp_path: Path, run_id: str) -> dict[str, object]:
    raw = (tmp_path / run_id / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert isinstance(manifest, dict)
    return manifest


class _RecordingRunner:
    """Runner counting calls. Proves cancelled queued job never executes."""

    def __init__(self, *, entered: threading.Event | None = None) -> None:
        self.calls = 0
        self._entered = entered
        self._lock = threading.Lock()

    def __call__(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: RunCancellation,
    ) -> ServiceResponse:
        with self._lock:
            self.calls += 1
        if self._entered is not None:
            self._entered.set()
        return ServiceResponse(200, {"run_id": run_id})


# ---------------------------------------------------------------------------
# 1. State machine
# ---------------------------------------------------------------------------


class TestCancellationStateMachine:
    def test_queued_job_becomes_cancelled_without_running(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "cancelled"
        assert snapshot is not None
        assert snapshot.status == "cancelled"
        # Worker never executes cancelled queued job.
        assert registry.begin_run("run1") is False
        assert registry.get("run1") is not None
        assert registry.get("run1").status == "cancelled"  # type: ignore[union-attr]

    def test_running_job_goes_through_cancelling_then_cancelled(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        assert registry.begin_run("run1") is True

        outcome, snapshot = registry.request_cancel("run1")
        assert outcome == "cancelling"
        assert snapshot is not None
        assert snapshot.status == "cancelling"

        # Even if runner returns success, confirmed-cancelled job doesn't become succeeded.
        final = registry.finish("run1", failed=False, response={"status": "ok"})
        assert final is not None
        assert final.status == "cancelled"
        assert final.response is None
        assert final.error is None

    def test_succeeded_job_cannot_be_cancelled(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.finish("run1", failed=False, response={"status": "ok"})

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "terminal"
        assert snapshot is not None
        assert snapshot.status == "succeeded"

    def test_failed_job_cannot_be_cancelled(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.finish("run1", failed=True, error="boom")

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "terminal"
        assert snapshot is not None
        assert snapshot.status == "failed"

    def test_repeated_cancel_is_deterministic(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")

        assert registry.request_cancel("run1")[0] == "cancelling"
        assert registry.request_cancel("run1")[0] == "already"
        registry.finish("run1", failed=False, response={"status": "ok"})
        # Repeated requests after termination always give same answer.
        assert registry.request_cancel("run1")[0] == "already"
        assert registry.request_cancel("run1")[0] == "already"

    def test_commit_closes_the_cancellation_window(self) -> None:
        """Job past last safety boundary does not transition to cancelling."""
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        cancellation = registry.cancellation("run1")
        assert cancellation is not None
        assert cancellation.commit() is True

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "terminal"
        assert snapshot is not None
        assert snapshot.status == "running"
        # and actually ends as succeeded — no cancelling -> succeeded transition.
        final = registry.finish("run1", failed=False, response={"status": "ok"})
        assert final is not None
        assert final.status == "succeeded"

    def test_cancellation_state_is_not_shared_between_runs(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run-a", created_by=None)
        registry.create(run_id="run-b", created_by=None)
        registry.begin_run("run-a")
        registry.begin_run("run-b")

        registry.request_cancel("run-a")

        cancellation_b = registry.cancellation("run-b")
        assert cancellation_b is not None
        assert cancellation_b.cancel_requested() is False
        assert registry.get("run-b") is not None
        assert registry.get("run-b").status == "running"  # type: ignore[union-attr]

    def test_cancelling_job_counts_as_running_workload(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.request_cancel("run1")

        counts = registry.snapshot_counts()

        assert counts.running == 1
        assert counts.queued == 0

    def test_cancelled_queued_job_leaves_no_active_workload(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.request_cancel("run1")

        counts = registry.snapshot_counts()

        assert counts.queued == 0
        assert counts.running == 0


class TestExecutorSkipsCancelledJobs:
    def test_cancelled_queued_job_never_invokes_the_runner(self) -> None:
        executor = AsyncBuildExecutor(max_workers=1, max_queue_size=10)
        blocker_entered = threading.Event()
        blocker_release = threading.Event()

        def _blocker(
            spec_yaml: str,
            run_id: str,
            created_by: str | None,
            cancellation: RunCancellation,
        ) -> ServiceResponse:
            blocker_entered.set()
            blocker_release.wait(timeout=5)
            return ServiceResponse(200, {"run_id": run_id})

        recording = _RecordingRunner()
        try:
            executor.submit(spec_yaml="spec", run_id="run-busy", created_by=None, runner=_blocker)
            assert blocker_entered.wait(timeout=5)
            # Single worker is occupied so this job is definitely in queued state.
            executor.submit(
                spec_yaml="spec", run_id="run-queued", created_by=None, runner=recording
            )
            assert executor.request_cancel("run-queued")[0] == "cancelled"
            blocker_release.set()
            _wait_for_status(executor, "run-busy", "succeeded")
        finally:
            blocker_release.set()
            executor.shutdown()

        assert recording.calls == 0
        snapshot = executor.get("run-queued")
        assert snapshot is not None
        assert snapshot.status == "cancelled"


def _wait_for_status(
    executor: AsyncBuildExecutor, run_id: str, status: str, *, timeout: float = 5.0
) -> None:
    deadline = threading.Event()
    for _ in range(int(timeout * 200)):
        snapshot = executor.get(run_id)
        if snapshot is not None and snapshot.status == status:
            return
        deadline.wait(0.005)
    raise AssertionError(f"job {run_id} did not reach {status}")


# ---------------------------------------------------------------------------
# 2. Pipeline safety boundaries and partial manifest
# ---------------------------------------------------------------------------


class TestPipelineBoundaries:
    """Boundary index: 0=before fetch, 1=after Bronze, 2=after Silver, 3=after Gold."""

    def test_cancel_before_fetch_produces_no_stage_artifacts(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        probe = _BoundaryProbe(cancel_after=0)

        response = service.build(VALID_SPEC_YAML, run_id="run1", cancellation=probe)

        assert response.status_code == 409
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert manifest["outputs"] == []
        # Does not record unexecuted stages as success.
        assert manifest["row_counts"] == {}
        assert not (tmp_path / "run1" / "bronze").exists()

    def test_cancel_after_bronze_keeps_bronze_and_skips_silver(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        response = service.build(
            VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1)
        )

        assert response.status_code == 409
        manifest = _read_manifest(tmp_path, "run1")
        outputs = manifest["outputs"]
        assert isinstance(outputs, list)
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert any("bronze" in str(path) for path in outputs)
        assert not any("silver" in str(path) for path in outputs)
        assert not any("gold" in str(path) for path in outputs)
        assert not (tmp_path / "run1" / "silver").exists()
        assert not (tmp_path / "run1" / "gold").exists()

    def test_cancel_after_silver_keeps_bronze_and_silver_only(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=2))

        manifest = _read_manifest(tmp_path, "run1")
        outputs = [str(path) for path in manifest["outputs"]]  # type: ignore[union-attr]
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert any("bronze" in path for path in outputs)
        assert any("silver" in path for path in outputs)
        assert not any("gold" in path for path in outputs)
        assert not (tmp_path / "run1" / "gold").exists()

    def test_cancel_after_gold_keeps_gold_but_skips_export(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=3))

        manifest = _read_manifest(tmp_path, "run1")
        outputs = [str(path) for path in manifest["outputs"]]
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert any("gold" in path for path in outputs)
        # BuildSpec.exports artifacts not created — does not start new stage after cancellation.
        assert not (tmp_path / "run1" / "out").exists()
        assert not any(path.endswith("data.jsonl") for path in outputs)

    def test_cancel_at_the_final_boundary_still_lands_on_cancelled(self, tmp_path: Path) -> None:
        """Cancellation arriving just before finalize after all stages also confirmed as cancelled.

        Last safe boundary in ``run_build`` is the ``commit()`` branch — without this path,
        cancellation request silently ignored, run ends as succeeded, job status (cancelling)
        contradicts manifest.
        """
        service = _service(tmp_path)
        # Single source has 4 boundaries (0~3). After passing all, cancellation wins at commit
        # .
        probe = _BoundaryProbe(cancel_after=4)

        response = service.build(VALID_SPEC_YAML, run_id="run1", cancellation=probe)

        assert response.status_code == 409
        assert probe.committed is False
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        # At this point artifacts are all created, but run is not confirmed as normally completed
        # so is not promoted to success.
        assert any("gold" in str(path) for path in manifest["outputs"])  # type: ignore[union-attr]
        assert service._build_index.get("run1") is not None
        assert service._build_index.get("run1").status == "cancelled"  # type: ignore[union-attr]

    def test_failed_source_reason_survives_a_later_cancellation(self, tmp_path: Path) -> None:
        """Cancellation does not swallow failures — failure reason stays in manifest errors."""
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda **_: _FakeClient({}),  # source fetch fails
            async_max_workers=1,
        )

        # Passing boundary 0 makes fetch fail so source outcome becomes "failed",
        # then cancellation wins at finalize boundary.
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["errors"] != []

    def test_cancellation_is_not_recorded_as_failure(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["errors"] == []
        assert manifest["status"] != "failed"

    def test_probe_without_cancellation_keeps_normal_success_path(self, tmp_path: Path) -> None:
        """Without cancellation, even with probe, existing success path and result are same."""
        service = _service(tmp_path)
        probe = _BoundaryProbe(cancel_after=None)

        response = service.build(VALID_SPEC_YAML, run_id="run1", cancellation=probe)

        assert response.status_code == 200
        assert probe.committed is True
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "ok"
        assert manifest["partial"] is False

    def test_synchronous_build_is_unaffected(self, tmp_path: Path) -> None:
        """Sync ``POST /build`` maintains existing behavior without cancellation concept."""
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML})

        assert response.status_code == 200
        assert isinstance(response, ServiceResponse)
        assert response.body["status"] == "ok"
        manifest = _read_manifest(tmp_path, str(response.body["run_id"]))
        assert manifest["status"] == "ok"
        assert manifest["partial"] is False

    def test_partial_manifest_carries_no_paths_beyond_the_run_workspace(
        self, tmp_path: Path
    ) -> None:
        """Cancellation manifest does not mix in raw exception/stack trace."""
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        raw = (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        assert "Traceback" not in raw
        assert "BuildCancelled" not in raw


# ---------------------------------------------------------------------------
# 3. BuildIndex / dataset semantics
# ---------------------------------------------------------------------------


class TestCancelledRunIndexSemantics:
    def test_build_index_records_cancelled_status(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        entry = service._build_index.get("run1")
        assert entry is not None
        assert entry.status == "cancelled"

    def test_rebuild_index_preserves_cancelled_status_from_manifest(self, tmp_path: Path) -> None:
        """Even if derived index is lost, cancelled is restored from manifest alone."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=2))
        service._build_index.close()

        rebuild_index(tmp_path)

        rebuilt = SqliteBuildIndex(tmp_path)
        try:
            entry = rebuilt.get("run1")
            assert entry is not None
            assert entry.status == "cancelled"
        finally:
            rebuilt.close()

    def test_cancelled_run_is_not_a_successful_artifact_write(self, tmp_path: Path) -> None:
        """Cancelled run is not promoted based on 'recent successful build'."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        assert service._build_index.latest_successful_finished_at() is None

    def test_build_list_reports_cancelled_not_ok(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        response = dispatch(service, "GET", "/builds", None)

        assert isinstance(response, ServiceResponse)
        builds = response.body["builds"]
        assert isinstance(builds, list)
        assert [b["status"] for b in builds] == ["cancelled"]  # type: ignore[index]

    def test_build_list_filesystem_fallback_reports_cancelled(self, tmp_path: Path) -> None:
        """Even if index is empty and falls back to filesystem, cancelled not mistaken as ok."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))
        service._build_index.delete("run1")

        response = dispatch(service, "GET", "/builds", None)

        assert isinstance(response, ServiceResponse)
        builds = response.body["builds"]
        assert isinstance(builds, list)
        assert [b["status"] for b in builds] == ["cancelled"]  # type: ignore[index]

    def test_cancelled_run_without_gold_exposes_no_gold_stage(self, tmp_path: Path) -> None:
        """Cancelled run that didn't create Gold doesn't appear as complete
        (=publishable) artifact."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        stages = dispatch(service, "GET", "/builds/run1/stages", None)

        assert isinstance(stages, ServiceResponse)
        assert stages.status_code == 200
        sources = stages.body["sources"]
        assert isinstance(sources, list)
        assert sources, "취소된 run도 시도한 source는 노출한다"
        for source in sources:
            assert isinstance(source, dict)
            # Bronze remains but Gold does not exist.
            assert source["gold"] != "available"
        # Dataset summary stage indicator also reflects same fact.
        detail = dispatch(service, "GET", "/datasets/dataset.cancel", None)
        assert isinstance(detail, ServiceResponse)
        stage_map = detail.body["stages"]
        assert isinstance(stage_map, dict)
        for summary in stage_map.values():
            assert isinstance(summary, dict)
            assert summary["gold"] != "available"

    def test_dataset_detail_surfaces_cancelled_status(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=2))

        response = dispatch(service, "GET", "/datasets/dataset.cancel", None)

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200
        assert response.body["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4. HTTP contract / ownership
# ---------------------------------------------------------------------------


class _BlockingCancelService(BuilderService):
    """Hold worker until ``release`` to make running window deterministic."""

    def __init__(
        self,
        *,
        output_root: Path,
        entered: threading.Event,
        release: threading.Event,
        completed: threading.Event,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=lambda **_: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            async_max_workers=1,
        )
        self._entered = entered
        self._release = release
        self._completed = completed

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        self._entered.set()
        self._release.wait(timeout=5)
        try:
            return super()._run_build_job(spec_yaml, run_id, created_by, cancellation)
        finally:
            self._completed.set()


def _await_event(
    service: BuilderService, run_id: str, event: str, *, timeout: float = 5.0
) -> list[str]:
    """Wait until ``event`` is recorded for run, return list of event names.

    Terminal events are appended **immediately after** terminal state transition,
    so reading events based on status alone creates timing dependency.
    """
    waiter = threading.Event()
    for _ in range(int(timeout * 200)):
        names = [e.event for e in service._event_store.list_for_run(run_id, limit=100, tail=False)]
        if event in names:
            return names
        waiter.wait(0.005)
    raise AssertionError(f"run {run_id} never recorded {event}")


def _await_job_status(
    service: BuilderService, run_id: str, status: str, *, timeout: float = 5.0
) -> None:
    """Wait until job reaches specified terminal state.

    The ``completed`` event is set when runner **returns**, but terminal transition
    and ``run_cancelled`` event append happen **after that** in executor (``_finish``) —
    confusing these two points creates test timing dependency.
    """
    waiter = threading.Event()
    for _ in range(int(timeout * 200)):
        snapshot = service._async_builds.get(run_id)
        if snapshot is not None and snapshot.status == status:
            return
        waiter.wait(0.005)
    raise AssertionError(f"job {run_id} did not reach {status}")


class TestCancelEndpoint:
    def test_unsafe_run_id_is_rejected(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/builds/..%2Fescape/cancel", None)

        assert response.status_code == 400

    def test_nested_path_is_not_treated_as_a_run_id(self, tmp_path: Path) -> None:
        """``/builds/a/b/cancel`` parses as run_id "a/b" without escaping path."""
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/builds/a/b/cancel", None)

        assert response.status_code == 400

    def test_unknown_run_returns_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/builds/never-submitted/cancel", None)

        assert response.status_code == 404

    def test_queued_job_cancel_returns_200_cancelled(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            assert (
                dispatch(
                    service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "busy"}
                ).status_code
                == 202
            )
            assert entered.wait(timeout=5)
            assert (
                dispatch(
                    service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"}
                ).status_code
                == 202
            )

            response = dispatch(service, "POST", "/builds/waiting/cancel", None)
        finally:
            release.set()

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200
        assert response.body["status"] == "cancelled"
        assert response.body["run_id"] == "waiting"
        # Cancelled queued job doesn't even create workspace.
        assert not (tmp_path / "waiting").exists()

    def test_running_job_cancel_reaches_cancelled_terminal(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)

            cancel = dispatch(service, "POST", "/builds/run1/cancel", None)
            assert isinstance(cancel, ServiceResponse)
            assert cancel.status_code == 200
            assert cancel.body["status"] == "cancelling"
        finally:
            release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "cancelled")

        status = dispatch(service, "GET", "/builds/run1", None)
        assert isinstance(status, ServiceResponse)
        assert status.body["status"] == "cancelled"
        # Cancelled job carries neither build output nor error string.
        assert "response" not in status.body
        assert "error" not in status.body
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True

    def test_repeat_cancel_on_cancelled_job_is_idempotent_200(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)
            first = dispatch(service, "POST", "/builds/run1/cancel", None)
            second = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "cancelled")
        third = dispatch(service, "POST", "/builds/run1/cancel", None)

        assert first.status_code == 200
        assert second.status_code == 200
        assert isinstance(second, ServiceResponse)
        assert second.body["status"] == "cancelling"
        assert third.status_code == 200
        assert isinstance(third, ServiceResponse)
        assert third.body["status"] == "cancelled"

    def test_resubmitting_a_cancelled_run_id_returns_the_existing_cancelled_job(
        self, tmp_path: Path
    ) -> None:
        """Resubmitting cancelled run_id does not create new job
        (existing "existing" resubmit contract).

        Per ADR 0008 policy, retry uses new run_id (no automatic in-place retry).
        If this path creates new job, confirmed cancelled state is silently overwritten.
        """
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "busy"})
            assert entered.wait(timeout=5)
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"})
            dispatch(service, "POST", "/builds/waiting/cancel", None)

            resubmitted = dispatch(
                service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"}
            )
        finally:
            release.set()

        assert isinstance(resubmitted, ServiceResponse)
        assert resubmitted.status_code == 200
        assert resubmitted.body["status"] == "cancelled"

    def test_terminal_succeeded_job_cancel_returns_409(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        assert entered.wait(timeout=5)
        release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "succeeded")

        response = dispatch(service, "POST", "/builds/run1/cancel", None)

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 409
        assert response.body["status"] == "succeeded"
        status = dispatch(service, "GET", "/builds/run1", None)
        assert isinstance(status, ServiceResponse)
        assert status.body["status"] == "succeeded"


class TestCancelOwnership:
    def _oidc(self, monkeypatch: pytest.MonkeyPatch, owner: str, label: str = "user") -> None:
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier=label, owner_id=owner),
        )

    def test_cross_owner_cannot_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            self._oidc(monkeypatch, "oidc:owner-a", label="a")
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)

            self._oidc(monkeypatch, "oidc:owner-b", label="b")
            response = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()

        assert response.status_code == 404
        # Actual state did not change.
        self._oidc(monkeypatch, "oidc:owner-a", label="a")
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "succeeded")
        status = dispatch(service, "GET", "/builds/run1", None)
        assert isinstance(status, ServiceResponse)
        assert status.body["status"] == "succeeded"

    def test_same_label_different_stable_owner_cannot_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            self._oidc(monkeypatch, "oidc:owner-a", label="same-label")
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)

            # Display label same but stable owner_id differs.
            self._oidc(monkeypatch, "oidc:owner-b", label="same-label")
            response = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()

        assert response.status_code == 404

    def test_owner_can_cancel_and_owner_id_never_leaks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            self._oidc(monkeypatch, "oidc:owner-a", label="a")
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)
            response = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "cancelled")

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200
        assert "owner_id" not in response.body
        assert "oidc:owner-a" not in json.dumps(response.body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 5. structured events (#496 vocabulary reuse)
# ---------------------------------------------------------------------------


class TestCancellationEvents:
    def test_queued_cancel_timeline_is_submitted_then_cancelled(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "busy"})
            assert entered.wait(timeout=5)
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"})
            dispatch(service, "POST", "/builds/waiting/cancel", None)
        finally:
            release.set()

        assert _await_event(service, "waiting", "run_cancelled") == [
            "run_submitted",
            "run_cancelled",
        ]
        events = service._event_store.list_for_run("waiting", limit=100, tail=False)
        assert events[-1].status == "ok"
        assert events[-1].message == "build cancelled at a safe stage boundary"

    def test_running_cancel_timeline_has_no_contradictory_terminal_event(
        self, tmp_path: Path
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)
            dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()
        assert completed.wait(timeout=5)
        names = _await_event(service, "run1", "run_cancelled")

        assert names[0] == "run_submitted"
        assert names[-1] == "run_cancelled"
        assert names.count("run_cancelled") == 1
        assert "run_finished" not in names
        assert "run_failed" not in names

    def test_successful_run_has_no_cancellation_event(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1")

        names = [e.event for e in service._event_store.list_for_run("run1", limit=100, tail=False)]
        assert "run_cancelled" not in names
        assert "run_finished" in names


# ---------------------------------------------------------------------------
# 6. Race conditions (Barrier-based, no sleep)
# ---------------------------------------------------------------------------


def _run_concurrently(*targets: Callable[[], None]) -> None:
    """Start all targets simultaneously with one Barrier."""
    barrier = threading.Barrier(len(targets))
    errors: list[BaseException] = []

    def _wrap(fn: Callable[[], None]) -> Callable[[], None]:
        def _inner() -> None:
            # barrier.wait is also in try — if BrokenBarrierError silently kills thread,
            # "executed simultaneously" premise breaks and test may pass.
            try:
                barrier.wait(timeout=5)
                fn()
            except BaseException as exc:  # noqa: BLE001 - move thread exception to main thread
                errors.append(exc)

        return _inner

    threads = [threading.Thread(target=_wrap(target)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    if errors:
        raise errors[0]


class TestCancellationRaces:
    ITERATIONS = 200

    def test_cancel_and_worker_start_yield_exactly_one_valid_path(self) -> None:
        """A. Even if queued cancel and worker start happen simultaneously,
        exactly one valid path."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            results: dict[str, object] = {}

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["cancel"] = reg.request_cancel(rid)[0]

            def _start(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["start"] = reg.begin_run(rid)

            _run_concurrently(_cancel, _start)

            snapshot = registry.get(run_id)
            assert snapshot is not None
            if results["start"] is True:
                # Worker wins: only queued -> running -> (cancelling) path possible.
                assert results["cancel"] in ("cancelling", "cancelled")
                assert snapshot.status in ("running", "cancelling")
                # If cancel returns "cancelled", it caught queued first, so
                # begin_run cannot be True — two results cannot hold simultaneously.
                assert results["cancel"] != "cancelled"
            else:
                # Cancellation wins: runner never starts.
                assert results["cancel"] == "cancelled"
                assert snapshot.status == "cancelled"

    def test_two_concurrent_cancels_never_corrupt_state(self) -> None:
        """D. Two simultaneous cancellations: state converges to one,
        responses deterministic combination."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            registry.begin_run(run_id)
            outcomes: list[str] = []
            outcomes_lock = threading.Lock()

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: list[str] = outcomes,
                out_lock: threading.Lock = outcomes_lock,
            ) -> None:
                outcome = reg.request_cancel(rid)[0]
                with out_lock:
                    out.append(outcome)

            _run_concurrently(_cancel, _cancel)

            assert sorted(outcomes) == ["already", "cancelling"]
            snapshot = registry.get(run_id)
            assert snapshot is not None
            assert snapshot.status == "cancelling"

    def test_cancel_and_final_boundary_never_both_win(self) -> None:
        """C. Even if last boundary (commit) and cancellation happen
        simultaneously, only one result."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            registry.begin_run(run_id)
            cancellation = registry.cancellation(run_id)
            assert cancellation is not None
            results: dict[str, object] = {}

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["cancel"] = reg.request_cancel(rid)[0]

            def _commit(
                cancel_state: RunCancellation = cancellation,
                out: dict[str, object] = results,
            ) -> None:
                out["commit"] = cancel_state.commit()

            _run_concurrently(_cancel, _commit)
            final = registry.finish(run_id, failed=False, response={"status": "ok"})
            assert final is not None

            if results["commit"] is True:
                # Pipeline wins: cancellation rejected, ends as succeeded.
                assert results["cancel"] == "terminal"
                assert final.status == "succeeded"
            else:
                # Cancellation wins: doesn't end as success.
                assert results["cancel"] == "cancelling"
                assert final.status == "cancelled"
                assert final.response is None

    def test_cancel_racing_a_build_failure_settles_on_one_terminal_state(self) -> None:
        """E. Even if cancellation and failure overlap, terminal state is one and consistent."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            registry.begin_run(run_id)
            results: dict[str, object] = {}

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["cancel"] = reg.request_cancel(rid)[0]

            def _fail(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                snapshot = reg.finish(rid, failed=True, error="boom")
                out["final"] = None if snapshot is None else snapshot.status

            _run_concurrently(_cancel, _fail)

            snapshot = registry.get(run_id)
            assert snapshot is not None
            assert snapshot.status in ("failed", "cancelled")
            if snapshot.status == "cancelled":
                # If ended as cancelled, failure reason not exposed as error.
                assert snapshot.error is None
            else:
                assert snapshot.error == "boom"

    def test_terminal_state_is_never_overwritten_by_a_late_finish(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.request_cancel("run1")
        registry.finish("run1", failed=False, response={"status": "ok"})

        again = registry.finish("run1", failed=True, error="late failure")

        assert again is not None
        assert again.status == "cancelled"
        assert again.error is None


class TestRunCancellationPrimitive:
    def test_request_after_commit_is_refused(self) -> None:
        cancellation = RunCancellation()
        assert cancellation.commit() is True
        assert cancellation.request() is False
        assert cancellation.cancel_requested() is False

    def test_commit_after_request_is_refused(self) -> None:
        cancellation = RunCancellation()
        assert cancellation.request() is True
        assert cancellation.commit() is False
        assert cancellation.cancel_requested() is True

    def test_commit_is_idempotent(self) -> None:
        cancellation = RunCancellation()
        assert cancellation.commit() is True
        assert cancellation.commit() is True

    def test_close_latches_the_window_and_reports_requested(self) -> None:
        cancellation = RunCancellation()
        cancellation.request()
        assert cancellation.close() is True
        assert cancellation.request() is False

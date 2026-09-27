"""Monitoring API unit test (#516).

Pure logic from ``service/monitoring.py`` (latency/p95, queue/worker, artifact
store, build statistics aggregation) and dispatch-level ownership isolation verification.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import AsyncBuildExecutor, RunCancellation
from kpubdata_builder.service.monitoring import (
    ApiStatus,
    ArtifactStoreStatus,
    LatencyRecorder,
    QueueStatus,
    WorkerStatus,
    aggregate_status,
    api_status,
    artifact_store_status,
    build_statistics,
    queue_status,
    validate_bucket,
    validate_window,
    worker_status,
)
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.service.quality import Availability
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store import SqliteBuildIndex

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


class _FakeCatalog:
    def __init__(self, items: list[object] | None = None) -> None:
        self._items = items or []

    def list(self, *, provider: str | None = None) -> list[object]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    """Minimal SourceClient mock required by service.build()."""

    datasets = _FakeCatalog()

    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, dataset_id: str) -> _FakeDataset:
        return _FakeDataset(self._data.get(dataset_id, []))

    def close(self) -> None:
        pass


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def _build_as(service: BuilderService, run_id: str, created_by: str) -> None:
    dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": run_id})
    import json

    mpath = service._output_root / run_id / "manifest.json"
    data = json.loads(mpath.read_text(encoding="utf-8"))
    data["created_by"] = created_by
    mpath.write_text(json.dumps(data), encoding="utf-8")


# =============================================================================
# LatencyRecorder / p95
# =============================================================================


class TestLatencyRecorder:
    def test_no_samples_reports_zero_count_and_null_p95(self) -> None:
        recorder = LatencyRecorder()
        sample_count, p95 = recorder.snapshot()
        assert sample_count == 0
        assert p95 is None

    def test_single_sample(self) -> None:
        recorder = LatencyRecorder()
        recorder.record(42.0)
        sample_count, p95 = recorder.snapshot()
        assert sample_count == 1
        assert p95 == 42.0

    def test_p95_nearest_rank_boundary(self) -> None:
        # n=20, rank = ceil(0.95*20) = 19 -> 1-indexed 19th (ascending) = value 19.
        recorder = LatencyRecorder()
        for value in range(1, 21):
            recorder.record(float(value))
        sample_count, p95 = recorder.snapshot()
        assert sample_count == 20
        assert p95 == 19.0

    def test_p95_unsorted_input_still_correct(self) -> None:
        recorder = LatencyRecorder()
        for value in [50.0, 10.0, 30.0, 20.0, 40.0]:
            recorder.record(value)
        # n=5, rank=ceil(0.95*5)=5 -> 5th ascending (max value) = 50.
        sample_count, p95 = recorder.snapshot()
        assert sample_count == 5
        assert p95 == 50.0

    def test_bounded_ring_buffer_evicts_oldest(self) -> None:
        recorder = LatencyRecorder(max_samples=3)
        for value in [1.0, 2.0, 3.0, 4.0]:
            recorder.record(value)
        sample_count, p95 = recorder.snapshot()
        # Only last 3 remain: [2, 3, 4]. rank=ceil(0.95*3)=3 -> max value 4.
        assert sample_count == 3
        assert p95 == 4.0

    def test_concurrent_record_and_snapshot_is_safe(self) -> None:
        recorder = LatencyRecorder(max_samples=10_000)
        errors: list[BaseException] = []

        def _writer(n: int) -> None:
            try:
                for i in range(n):
                    recorder.record(float(i))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _reader() -> None:
            try:
                for _ in range(50):
                    recorder.snapshot()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(200,)) for _ in range(4)]
        threads += [threading.Thread(target=_reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        sample_count, _ = recorder.snapshot()
        assert sample_count == 800  # 4 writers * 200 (bounded 10_000 so no loss)

    def test_record_failure_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Metric recording failure must not propagate to request failure (#516)."""
        recorder = LatencyRecorder()

        class _BrokenLock:
            def __enter__(self) -> None:
                raise RuntimeError("simulated lock failure")

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(recorder, "_lock", _BrokenLock())
        recorder.record(10.0)  # must not raise exception.

    def test_snapshot_failure_returns_none_not_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """collector failure is distinguished as None, not (0, None) (#527).

        (0, None) is "normal un-sampled"; None is "measurement itself impossible" —
        conflating them makes api_status() always masquerade as available.
        """
        recorder = LatencyRecorder()

        class _BrokenLock:
            def __enter__(self) -> None:
                raise RuntimeError("simulated lock failure")

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(recorder, "_lock", _BrokenLock())
        assert recorder.snapshot() is None  # doesn't raise exception, but also not (0, None).


class TestApiStatus:
    def test_available_with_no_samples(self) -> None:
        status = api_status(LatencyRecorder())
        assert status.availability == "available"
        assert status.sample_count == 0
        assert status.p95_latency_ms is None

    def test_available_with_samples(self) -> None:
        recorder = LatencyRecorder()
        recorder.record(5.0)
        status = api_status(recorder)
        assert status.availability == "available"
        assert status.sample_count == 1
        assert status.p95_latency_ms == 5.0

    def test_unavailable_when_collector_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """collector failure (#527) is unavailable+null+null, not masked as available+0."""
        recorder = LatencyRecorder()

        class _BrokenLock:
            def __enter__(self) -> None:
                raise RuntimeError("simulated lock failure")

            def __exit__(self, *exc: object) -> None:
                return None

        monkeypatch.setattr(recorder, "_lock", _BrokenLock())
        status = api_status(recorder)
        assert status.availability == "unavailable"
        assert status.sample_count is None
        assert status.p95_latency_ms is None


# =============================================================================
# Queue / Worker — read-only snapshot of actual AsyncBuildExecutor/Registry (#511/#513)
# (#516). Always-created executor is normally available.
# =============================================================================


class _StubResponse:
    """Minimal BuildJobResponse mock required by AsyncBuildExecutor.submit()."""

    def __init__(self, status_code: int = 200, body: dict[str, JsonValue] | None = None) -> None:
        self.status_code = status_code
        self.body: dict[str, JsonValue] = body or {}


def _blocking_runner(entered: threading.Event, release: threading.Event):  # type: ignore[no-untyped-def]
    """Signal runner is running via ``entered`` and wait until ``release``.

    Test helper to deterministically observe queue/worker state by holding jobs in "running"
    state at desired points (#516).
    """

    def _runner(
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: RunCancellation,
    ) -> _StubResponse:
        entered.set()
        release.wait(timeout=5)
        return _StubResponse(200, {"run_id": run_id})

    return _runner


def _wait_for_terminal(executor: AsyncBuildExecutor, run_id: str, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = executor.get(run_id)
        if snapshot is not None and snapshot.status in ("succeeded", "failed", "cancelled"):
            return
        time.sleep(0.01)
    raise AssertionError(f"job {run_id} did not reach terminal status within {timeout}s")


class TestQueueWorkerStatus:
    def test_no_jobs_is_available_with_zero_counts(self) -> None:
        executor = AsyncBuildExecutor(max_workers=3, max_queue_size=10)
        try:
            queue = queue_status(executor)
            workers = worker_status(executor)
        finally:
            executor.shutdown()
        assert queue.availability == "available"
        assert queue.waiting == 0
        assert queue.running == 0
        assert queue.total == 0
        assert workers.availability == "available"
        assert workers.active == 0
        assert workers.capacity == 3
        assert workers.utilization == 0.0

    def test_queued_job_is_reflected_in_waiting(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        executor = AsyncBuildExecutor(max_workers=1, max_queue_size=10)
        try:
            executor.submit(
                spec_yaml="spec",
                run_id="run-blocking",
                created_by=None,
                runner=_blocking_runner(entered, release),
            )
            assert entered.wait(timeout=5)
            executor.submit(
                spec_yaml="spec",
                run_id="run-queued",
                created_by=None,
                runner=_blocking_runner(threading.Event(), threading.Event()),
            )
            queue = queue_status(executor)
            workers = worker_status(executor)
            release.set()
        finally:
            executor.shutdown()
        assert queue.availability == "available"
        assert queue.waiting == 1
        assert queue.running == 1
        assert queue.total == 2
        assert workers.active == 1
        assert workers.capacity == 1
        assert workers.utilization == 1.0

    def test_running_job_is_reflected_in_running_and_active(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        executor = AsyncBuildExecutor(max_workers=1, max_queue_size=10)
        try:
            executor.submit(
                spec_yaml="spec",
                run_id="run-1",
                created_by=None,
                runner=_blocking_runner(entered, release),
            )
            assert entered.wait(timeout=5)
            queue = queue_status(executor)
            workers = worker_status(executor)
            release.set()
        finally:
            executor.shutdown()
        assert queue.waiting == 0
        assert queue.running == 1
        assert queue.total == 1
        assert workers.active == 1

    def test_succeeded_and_failed_terminal_jobs_excluded_from_active(self) -> None:
        executor = AsyncBuildExecutor(max_workers=2, max_queue_size=10)
        try:

            def _succeed(
                spec_yaml: str,
                run_id: str,
                created_by: str | None,
                cancellation: RunCancellation,
            ) -> _StubResponse:
                return _StubResponse(200, {"run_id": run_id})

            def _fail(
                spec_yaml: str,
                run_id: str,
                created_by: str | None,
                cancellation: RunCancellation,
            ) -> _StubResponse:
                raise RuntimeError("simulated build failure")

            executor.submit(spec_yaml="spec", run_id="run-ok", created_by=None, runner=_succeed)
            executor.submit(spec_yaml="spec", run_id="run-failed", created_by=None, runner=_fail)
            _wait_for_terminal(executor, "run-ok")
            _wait_for_terminal(executor, "run-failed")

            queue = queue_status(executor)
            workers = worker_status(executor)
        finally:
            executor.shutdown()
        assert queue.waiting == 0
        assert queue.running == 0
        assert queue.total == 0
        assert workers.active == 0
        assert executor.get("run-ok") is not None  # registry preserves terminal jobs too.
        assert executor.get("run-failed") is not None

    def test_capacity_matches_configured_max_workers(self) -> None:
        executor = AsyncBuildExecutor(max_workers=7, max_queue_size=10)
        try:
            workers = worker_status(executor)
        finally:
            executor.shutdown()
        assert workers.capacity == 7

    def test_utilization_is_active_over_capacity_ratio(self) -> None:
        entered_a = threading.Event()
        entered_b = threading.Event()
        release = threading.Event()
        executor = AsyncBuildExecutor(max_workers=4, max_queue_size=10)
        try:
            executor.submit(
                spec_yaml="spec",
                run_id="run-a",
                created_by=None,
                runner=_blocking_runner(entered_a, release),
            )
            executor.submit(
                spec_yaml="spec",
                run_id="run-b",
                created_by=None,
                runner=_blocking_runner(entered_b, release),
            )
            assert entered_a.wait(timeout=5)
            assert entered_b.wait(timeout=5)
            workers = worker_status(executor)
            release.set()
        finally:
            executor.shutdown()
        assert workers.active == 2
        assert workers.capacity == 4
        assert workers.utilization == 0.5

    def test_monitoring_read_does_not_mutate_job_state(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        executor = AsyncBuildExecutor(max_workers=1, max_queue_size=10)
        try:
            executor.submit(
                spec_yaml="spec",
                run_id="run-1",
                created_by=None,
                runner=_blocking_runner(entered, release),
            )
            assert entered.wait(timeout=5)
            before = executor.get("run-1")
            queue_status(executor)
            worker_status(executor)
            after = executor.get("run-1")
            release.set()
        finally:
            executor.shutdown()
        assert before is not None and after is not None
        assert before.status == after.status == "running"
        assert before.updated_at == after.updated_at

    def test_snapshot_thread_safety_under_concurrent_submits_and_reads(self) -> None:
        release = threading.Event()
        executor = AsyncBuildExecutor(max_workers=4, max_queue_size=200)
        errors: list[BaseException] = []

        def _submitter(i: int) -> None:
            try:
                executor.submit(
                    spec_yaml="spec",
                    run_id=f"run-{i}",
                    created_by=None,
                    runner=_blocking_runner(threading.Event(), release),
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def _reader() -> None:
            try:
                for _ in range(50):
                    queue = queue_status(executor)
                    workers = worker_status(executor)
                    assert queue.total == (queue.waiting or 0) + (queue.running or 0)
                    assert (workers.active or 0) <= (workers.capacity or 0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_submitter, args=(i,)) for i in range(20)]
        threads += [threading.Thread(target=_reader) for _ in range(4)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            release.set()
        finally:
            executor.shutdown()
        assert not errors


# =============================================================================
# Artifact Store
# =============================================================================


class TestArtifactStoreStatus:
    def test_missing_output_root_is_unavailable(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        index = SqliteBuildIndex(
            tmp_path
        )  # index in tmp_path, output_root to non-existent path only.
        status = artifact_store_status(missing, index)
        assert status.availability == "unavailable"
        assert status.last_write_at is None

    def test_available_with_no_successful_builds_yet(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        status = artifact_store_status(tmp_path, index)
        assert status.availability == "available"
        assert status.last_write_at is None  # zero count, not unconfirmable.

    def test_available_with_last_write_evidence(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run-1",
            status="ok",
            started_at="2026-08-15T01:00:00Z",
            finished_at="2026-08-15T01:05:00Z",
        )
        status = artifact_store_status(tmp_path, index)
        assert status.availability == "available"
        assert status.last_write_at == "2026-08-15T01:05:00Z"

    def test_failed_only_builds_do_not_count_as_write_evidence(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run-1",
            status="failed",
            started_at="2026-08-15T01:00:00Z",
            finished_at="2026-08-15T01:05:00Z",
        )
        status = artifact_store_status(tmp_path, index)
        assert status.availability == "available"
        assert status.last_write_at is None

    def test_index_query_failure_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        index = SqliteBuildIndex(tmp_path)
        monkeypatch.setattr(
            index,
            "latest_successful_finished_at",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated sqlite failure")),
        )
        status = artifact_store_status(tmp_path, index)
        assert status.availability == "unavailable"
        assert status.last_write_at is None

    def test_no_absolute_path_leaks_into_status(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        status = artifact_store_status(tmp_path, index)
        rendered = f"{status.availability}{status.last_write_at}"
        assert str(tmp_path) not in rendered


# =============================================================================
# Aggregate status (#516 final check) — from required subsystem availability
# determines healthy/degraded; latency threshold is never used.
# =============================================================================


def _api(
    *,
    availability: Availability = "available",
    sample_count: int | None = 0,
    p95: float | None = None,
) -> ApiStatus:
    return ApiStatus(availability=availability, sample_count=sample_count, p95_latency_ms=p95)


def _queue(
    *, availability: Availability = "available", waiting: int | None = 0, running: int | None = 0
) -> QueueStatus:
    total = None if waiting is None or running is None else waiting + running
    return QueueStatus(availability=availability, waiting=waiting, running=running, total=total)


def _workers(
    *, availability: Availability = "available", active: int | None = 0, capacity: int | None = 10
) -> WorkerStatus:
    utilization = None if active is None or capacity is None or capacity == 0 else active / capacity
    return WorkerStatus(
        availability=availability, active=active, capacity=capacity, utilization=utilization
    )


def _artifact(
    *, availability: Availability = "available", last_write_at: str | None = None
) -> ArtifactStoreStatus:
    return ArtifactStoreStatus(availability=availability, last_write_at=last_write_at)


class TestAggregateStatus:
    def test_all_available_is_healthy(self) -> None:
        status = aggregate_status(
            api=_api(), queue=_queue(), workers=_workers(), artifact_store=_artifact()
        )
        assert status == "healthy"

    def test_queue_actually_zero_is_still_healthy(self) -> None:
        """queue waiting/running=0 is available+0 and not grounds for degraded."""
        status = aggregate_status(
            api=_api(),
            queue=_queue(waiting=0, running=0),
            workers=_workers(),
            artifact_store=_artifact(),
        )
        assert status == "healthy"

    def test_workers_actually_zero_is_still_healthy(self) -> None:
        """workers active=0 is available+0 and not grounds for degraded."""
        status = aggregate_status(
            api=_api(),
            queue=_queue(),
            workers=_workers(active=0),
            artifact_store=_artifact(),
        )
        assert status == "healthy"

    def test_artifact_unavailable_is_degraded(self) -> None:
        status = aggregate_status(
            api=_api(),
            queue=_queue(),
            workers=_workers(),
            artifact_store=_artifact(availability="unavailable", last_write_at=None),
        )
        assert status == "degraded"

    def test_api_unavailable_is_degraded(self) -> None:
        """api collector failure (#527) with availability=unavailable makes aggregate degraded."""
        status = aggregate_status(
            api=_api(availability="unavailable", sample_count=None, p95=None),
            queue=_queue(),
            workers=_workers(),
            artifact_store=_artifact(),
        )
        assert status == "degraded"

    def test_required_subsystem_partial_is_degraded(self) -> None:
        """If any of api/queue/workers/artifact_store is partial, aggregate is degraded."""
        status = aggregate_status(
            api=_api(availability="partial"),
            queue=_queue(),
            workers=_workers(),
            artifact_store=_artifact(),
        )
        assert status == "degraded"

    def test_queue_unavailable_is_degraded(self) -> None:
        status = aggregate_status(
            api=_api(),
            queue=_queue(availability="unavailable", waiting=None, running=None),
            workers=_workers(),
            artifact_store=_artifact(),
        )
        assert status == "degraded"

    def test_workers_unavailable_is_degraded(self) -> None:
        status = aggregate_status(
            api=_api(),
            queue=_queue(),
            workers=_workers(availability="unavailable", active=None, capacity=None),
            artifact_store=_artifact(),
        )
        assert status == "degraded"

    def test_no_samples_with_rest_available_is_healthy(self) -> None:
        """sample_count=0/p95=null can be startup/unsampled state, not grounds for degraded."""
        status = aggregate_status(
            api=_api(sample_count=0, p95=None),
            queue=_queue(),
            workers=_workers(),
            artifact_store=_artifact(),
        )
        assert status == "healthy"

    def test_provider_has_no_parameter_and_no_influence(self) -> None:
        """Provider status is optional in #516 so determination function doesn't take it at all."""
        import inspect

        params = inspect.signature(aggregate_status).parameters
        assert "provider" not in params


# =============================================================================
# window/bucket validation
# =============================================================================


class TestValidation:
    def test_validate_window_accepts_24h(self) -> None:
        assert validate_window("24h") == "24h"

    def test_validate_window_rejects_others(self) -> None:
        assert validate_window("7d") is None
        assert validate_window("") is None

    def test_validate_bucket_accepts_hour(self) -> None:
        assert validate_bucket("hour") == "hour"

    def test_validate_bucket_rejects_others(self) -> None:
        assert validate_bucket("day") is None
        assert validate_bucket("") is None


# =============================================================================
# Build statistics aggregation
# =============================================================================


_NOW = datetime(2026, 8, 15, 10, 30, 0, tzinfo=timezone.utc)


class TestBuildStatistics:
    def test_empty_index_is_available_with_zero_buckets(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        assert stats.availability == "available"
        assert stats.excluded_count == 0
        assert len(stats.buckets) == 24
        assert all(b.total == 0 for b in stats.buckets)
        assert stats.recent_runs == ()

    def test_counts_success_failed_cancelled_in_correct_bucket(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="ok-1", status="ok", started_at=None, finished_at="2026-08-15T09:15:00Z"
        )
        index.insert_or_replace(
            run_id="failed-1",
            status="failed",
            started_at=None,
            finished_at="2026-08-15T09:45:00Z",
        )
        index.insert_or_replace(
            run_id="cancelled-1",
            status="cancelled",
            started_at=None,
            finished_at="2026-08-15T09:59:59Z",
        )
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        bucket_09 = next(b for b in stats.buckets if b.bucket_start == "2026-08-15T09:00:00Z")
        assert bucket_09.total == 3
        # internal BuildIndex status "ok" maps to wire field "success" (#527).
        assert bucket_09.success == 1
        assert bucket_09.failed == 1
        assert bucket_09.cancelled == 1
        assert bucket_09.bucket_end == "2026-08-15T10:00:00Z"

    def test_bucket_boundary_is_half_open(self, tmp_path: Path) -> None:
        """On-the-hour timestamp belongs to next bucket ([start, end) half-open)."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="on-boundary",
            status="ok",
            started_at=None,
            finished_at="2026-08-15T09:00:00Z",
        )
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        bucket_09 = next(b for b in stats.buckets if b.bucket_start == "2026-08-15T09:00:00Z")
        bucket_08 = next(b for b in stats.buckets if b.bucket_start == "2026-08-15T08:00:00Z")
        assert bucket_09.total == 1
        assert bucket_08.total == 0

    def test_entries_outside_window_are_excluded(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        # window is [2026-08-14T11:00, 2026-08-15T11:00) — day before is outside window.
        index.insert_or_replace(
            run_id="too-old",
            status="ok",
            started_at=None,
            finished_at="2026-08-13T09:00:00Z",
        )
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        assert all(b.total == 0 for b in stats.buckets)

    def test_malformed_timestamp_excluded_and_marks_partial(self, tmp_path: Path) -> None:
        """Date prefix is normal but time portion is corrupted legacy value (#516)."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="good", status="ok", started_at=None, finished_at="2026-08-15T09:00:00Z"
        )
        index.insert_or_replace(
            run_id="bad", status="ok", started_at=None, finished_at="2026-08-15T09:99:00Z"
        )
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        assert stats.availability == "partial"
        assert stats.excluded_count == 1
        total = sum(b.total for b in stats.buckets)
        assert total == 1  # malformed row is not counted in any bucket.

    def test_null_finished_at_excluded_and_marks_partial(self, tmp_path: Path) -> None:
        """finished_at NULL rows are silently excluded, not lost, and counted as partial (#516)."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="good", status="ok", started_at=None, finished_at="2026-08-15T09:00:00Z"
        )
        index.insert_or_replace(
            run_id="null-finished", status="ok", started_at=None, finished_at=None
        )
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        assert stats.availability == "partial"
        assert stats.excluded_count == 1

    def test_index_query_failure_is_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        index = SqliteBuildIndex(tmp_path)

        def _raise(*_args: object, **_kwargs: object) -> list:
            raise RuntimeError("simulated sqlite failure")

        monkeypatch.setattr(index, "list_between", _raise)
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        assert stats.availability == "unavailable"
        assert stats.buckets == ()
        assert stats.recent_runs == ()

    def test_deterministic_bucket_ordering(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        starts = [b.bucket_start for b in stats.buckets]
        assert starts == sorted(starts)
        assert len(starts) == len(set(starts))

    def test_recent_runs_bounded_to_ten(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        for i in range(15):
            index.insert_or_replace(
                run_id=f"run-{i:02d}",
                status="ok",
                started_at=None,
                finished_at=f"2026-08-15T09:{i:02d}:00Z",
            )
        stats = build_statistics(
            index, window="24h", bucket="hour", principal=None, enforce_ownership=False, now=_NOW
        )
        assert len(stats.recent_runs) == 10

    def test_ownership_filters_other_principal_when_enforced(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="mine",
            status="ok",
            started_at=None,
            finished_at="2026-08-15T09:00:00Z",
            created_by="oidc:userA",
        )
        index.insert_or_replace(
            run_id="theirs",
            status="ok",
            started_at=None,
            finished_at="2026-08-15T09:10:00Z",
            created_by="oidc:userB",
        )
        principal = Principal(kind="oidc", identifier="userA")
        stats = build_statistics(
            index,
            window="24h",
            bucket="hour",
            principal=principal,
            enforce_ownership=True,
            now=_NOW,
        )
        run_ids = {r.run_id for r in stats.recent_runs}
        assert run_ids == {"mine"}
        total = sum(b.total for b in stats.buckets)
        assert total == 1

    def test_recent_run_survives_limit_when_older_than_other_users_runs(
        self, tmp_path: Path
    ) -> None:
        """#527: even with latest 10 runs of other users, my older recent run
        is not cut by global LIMIT(10) applied before ownership filter —
        filter must be applied in SQL before LIMIT (``BuildIndex.list_recent_owned``).
        """
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="mine-old",
            status="ok",
            started_at=None,
            finished_at="2020-01-01T00:00:00Z",  # all older than 10 other user runs below.
            created_by="oidc:userA",
        )
        for i in range(10):
            index.insert_or_replace(
                run_id=f"theirs-{i:02d}",
                status="ok",
                started_at=None,
                finished_at=f"2026-08-15T{i:02d}:00:00Z",  # all newer than mine-old.
                created_by="oidc:userB",
            )
        principal = Principal(kind="oidc", identifier="userA")
        stats = build_statistics(
            index,
            window="24h",
            bucket="hour",
            principal=principal,
            enforce_ownership=True,
            now=_NOW,
        )
        run_ids = {r.run_id for r in stats.recent_runs}
        assert "mine-old" in run_ids
        assert "theirs-00" not in run_ids

    def test_ownership_not_filtered_when_enforcement_off(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="mine",
            status="ok",
            started_at=None,
            finished_at="2026-08-15T09:00:00Z",
            created_by="oidc:userA",
        )
        index.insert_or_replace(
            run_id="theirs",
            status="ok",
            started_at=None,
            finished_at="2026-08-15T09:10:00Z",
            created_by="oidc:userB",
        )
        principal = Principal(kind="oidc", identifier="userA")
        stats = build_statistics(
            index,
            window="24h",
            bucket="hour",
            principal=principal,
            enforce_ownership=False,
            now=_NOW,
        )
        run_ids = {r.run_id for r in stats.recent_runs}
        assert run_ids == {"mine", "theirs"}

    def test_dev_principal_bypasses_ownership_filter(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="theirs",
            status="ok",
            started_at=None,
            finished_at="2026-08-15T09:10:00Z",
            created_by="oidc:userB",
        )
        principal = Principal(kind="dev")
        stats = build_statistics(
            index,
            window="24h",
            bucket="hour",
            principal=principal,
            enforce_ownership=True,
            now=_NOW,
        )
        run_ids = {r.run_id for r in stats.recent_runs}
        assert run_ids == {"theirs"}


# =============================================================================
# dispatch level: route wiring, ownership isolation, secret non-exposure
# =============================================================================


class TestMonitoringDispatch:
    def test_summary_route_returns_200(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/monitoring/summary", None)
        assert resp.status_code == 200
        assert resp.body["status"] == "healthy"
        assert resp.body["queue"]["availability"] == "available"  # type: ignore[index]
        assert resp.body["queue"]["waiting"] == 0  # type: ignore[index]
        assert resp.body["queue"]["running"] == 0  # type: ignore[index]
        assert resp.body["queue"]["total"] == 0  # type: ignore[index]
        assert resp.body["workers"]["availability"] == "available"  # type: ignore[index]
        assert resp.body["workers"]["active"] == 0  # type: ignore[index]
        assert resp.body["workers"]["capacity"] == 10  # type: ignore[index]
        assert resp.body["workers"]["utilization"] == 0.0  # type: ignore[index]

    def test_summary_status_is_degraded_when_artifact_store_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """artifact_store BuildIndex query failure makes unavailable, which makes overall status degraded."""
        service = _service(tmp_path)
        monkeypatch.setattr(
            service._build_index,
            "latest_successful_finished_at",
            lambda: (_ for _ in ()).throw(RuntimeError("simulated sqlite failure")),
        )
        resp = dispatch(service, "GET", "/monitoring/summary", None)
        assert resp.status_code == 200
        assert resp.body["artifact_store"]["availability"] == "unavailable"  # type: ignore[index]
        assert resp.body["status"] == "degraded"

    def test_builds_route_returns_200_with_defaults(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/monitoring/builds", None, query="")
        assert resp.status_code == 200
        assert resp.body["window"] == "24h"
        assert resp.body["bucket"] == "hour"

    def test_builds_route_rejects_unsupported_window(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/monitoring/builds", None, query="window=7d")
        assert resp.status_code == 400

    def test_builds_route_rejects_unsupported_bucket(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/monitoring/builds", None, query="bucket=day")
        assert resp.status_code == 400

    def test_cross_user_recent_runs_not_leaked_when_ownership_enforced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        _build_as(service, "runA", "oidc:userA")
        _build_as(service, "runB", "oidc:userB")

        user_a = Principal(kind="oidc", identifier="userA")
        resp = service.monitoring_builds(window="24h", bucket="hour", principal=user_a)
        assert resp.status_code == 200
        recent_run_ids = {
            cast(dict[str, JsonValue], r)["run_id"]
            for r in cast(list[JsonValue], resp.body["recent_runs"])
        }
        assert "runB" not in recent_run_ids

    def test_no_secrets_or_internal_paths_in_summary_response(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/monitoring/summary", None)
        rendered = str(resp.body)
        assert str(tmp_path) not in rendered
        for forbidden in ("owner_id", "api_key", "bearer", "credential", "secret"):
            assert forbidden not in rendered.lower()

    def test_no_secrets_or_dataset_owner_in_builds_response(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "run-1"})
        resp = dispatch(service, "GET", "/monitoring/builds", None, query="")
        rendered = str(resp.body)
        assert str(tmp_path) not in rendered
        for forbidden in ("owner_id", "dataset_id", "credential", "secret"):
            assert forbidden not in rendered.lower()

    def test_dispatch_records_latency_sample(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        dispatch(service, "GET", "/healthz", None)
        sample_count, _ = service._latency_recorder.snapshot()
        assert sample_count >= 1

"""A cancel, a shutdown and a build's own ending arriving together (#1241).

Each path is tested alone elsewhere: a user's cancel (``test_build_cancellation``),
the shutdown that ends queued jobs (``test_shutdown_drain``) and the time limit
(``test_build_time_limit``). Here they are released together by a barrier, many
times over, and every interleaving must leave a run with exactly one ending that
agrees with what each caller was told: a run told ``cancelled`` is not failed, a
run whose commit won is not cancelled, a job ended in the queue is never started,
and the build slot count drops back to zero. A run nobody touched keeps running.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from kpubdata_builder.service.jobs import AsyncBuildJobRegistry, CancelOutcome, RunCancellation

_ROUNDS = 200


def _together(*actions: Callable[[], None]) -> None:
    """Start every action at the same barrier and wait for all of them."""
    barrier = threading.Barrier(len(actions))
    errors: list[BaseException] = []

    def _run(action: Callable[[], None]) -> None:
        barrier.wait(timeout=5)
        try:
            action()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the test below
            errors.append(exc)

    threads = [threading.Thread(target=_run, args=(action,)) for action in actions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors, errors


def test_a_queued_job_cancelled_while_the_server_shuts_down_ends_once() -> None:
    for _ in range(_ROUNDS):
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="queued", created_by=None, owner_id="owner-a")
        registry.create(run_id="other", created_by=None, owner_id="owner-b")
        assert registry.begin_run("other") is True
        cancel: list[CancelOutcome] = []
        ended: list[str] = []

        def _cancel(
            registry: AsyncBuildJobRegistry = registry, cancel: list[CancelOutcome] = cancel
        ) -> None:
            cancel.append(registry.request_cancel("queued")[0])

        def _shutdown(registry: AsyncBuildJobRegistry = registry, ended: list[str] = ended) -> None:
            ended.extend(job.run_id for job in registry.close_and_fail_queued("shutting down"))

        _together(_cancel, _shutdown)

        final = registry.get("queued")
        assert final is not None
        if cancel == ["cancelled"]:
            assert final.status == "cancelled"
            assert ended == []
        else:
            assert cancel == ["terminal"]
            assert final.status == "failed"
            assert ended == ["queued"]
        # A job ended in the queue never starts, whichever ending it got.
        assert registry.begin_run("queued") is False
        # The run that was already running is left to finish.
        other = registry.get("other")
        assert other is not None
        assert other.status == "running"
        counts = registry.snapshot_counts()
        assert (counts.queued, counts.running) == (0, 1)


def test_a_worker_starting_a_job_while_it_is_cancelled_and_the_server_shuts_down() -> None:
    for _ in range(_ROUNDS):
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run", created_by=None)
        started: list[bool] = []
        cancel: list[CancelOutcome] = []
        ended: list[str] = []

        def _start(
            registry: AsyncBuildJobRegistry = registry, started: list[bool] = started
        ) -> None:
            started.append(registry.begin_run("run"))

        def _cancel(
            registry: AsyncBuildJobRegistry = registry, cancel: list[CancelOutcome] = cancel
        ) -> None:
            cancel.append(registry.request_cancel("run")[0])

        def _shutdown(registry: AsyncBuildJobRegistry = registry, ended: list[str] = ended) -> None:
            ended.extend(job.run_id for job in registry.close_and_fail_queued("shutting down"))

        _together(_start, _cancel, _shutdown)

        snapshot = registry.get("run")
        assert snapshot is not None
        if started == [True]:
            # The worker won: the shutdown left it running, and the cancel, which came
            # after the start, asked it to stop at its next safe boundary.
            assert ended == []
            assert cancel == ["cancelling"]
            final = registry.finish("run", failed=False, response={"status": "ok"})
            assert final is not None
            assert final.status == "cancelled"
        else:
            # The worker lost, so the runner is never called; the job has the ending of
            # whichever of the other two came first.
            assert started == [False]
            if ended == ["run"]:
                assert snapshot.status == "failed"
                assert cancel == ["terminal"]
            else:
                assert snapshot.status == "cancelled"
                assert cancel == ["cancelled"]
        counts = registry.snapshot_counts()
        assert (counts.queued, counts.running) == (0, 0)


def test_a_cancel_racing_the_builds_commit_gives_one_consistent_ending() -> None:
    for _ in range(_ROUNDS):
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run", created_by=None)
        registry.create(run_id="other", created_by=None)
        assert registry.begin_run("run") is True
        assert registry.begin_run("other") is True
        cancellation = registry.cancellation("run")
        assert cancellation is not None
        committed: list[bool] = []
        cancel: list[CancelOutcome] = []

        def _commit_and_finish(
            registry: AsyncBuildJobRegistry = registry,
            committed: list[bool] = committed,
            probe: RunCancellation = cancellation,
        ) -> None:
            # What the pipeline does: commit at its last boundary, then end. A refused
            # commit means a cancel came first and the build stops there.
            ok = probe.commit()
            committed.append(ok)
            registry.finish("run", failed=False, response={"status": "ok"} if ok else None)

        def _cancel(
            registry: AsyncBuildJobRegistry = registry, cancel: list[CancelOutcome] = cancel
        ) -> None:
            cancel.append(registry.request_cancel("run")[0])

        _together(_commit_and_finish, _cancel)

        final = registry.get("run")
        assert final is not None
        if committed == [True]:
            assert final.status == "succeeded"
            assert cancel == ["terminal"]
        else:
            assert final.status == "cancelled"
            assert final.response is None
            assert cancel == ["cancelling"]
        other = registry.get("other")
        assert other is not None
        assert other.status == "running"
        assert registry.snapshot_counts().running == 1

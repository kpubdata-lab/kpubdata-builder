"""Async build job registry and bounded worker executor (#482, #481 cancelled).

Cancellation (#481) design summary:

- **No forced termination**: Don't kill worker thread. ``RunCancellation`` holds
  per-run cooperative cancellation state; pipeline checks this at safe stage
  boundaries (``pipeline.cancellation.CancellationProbe`` contract).
- **Single arbiter**: Whether job ends succeeded/failed vs cancelled is decided by
  single latch in ``RunCancellation`` (committed/requested) (``AsyncBuildJobRegistry.finish``).
  No state where same run records both succeeded and cancelled simultaneously.
- **Lock order**: registry lock -> run cancellation lock. No reverse path
  (``RunCancellation`` knows nothing of registry), and neither lock makes external calls
  (runner/pipeline/disk I/O) while holding it.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import Lock
from typing import TYPE_CHECKING, Literal, Protocol
from uuid import uuid4

from ..spec import JsonValue
from .build_slots import BuildSlots

_logger = logging.getLogger(__name__)

BuildJobStatus = Literal["queued", "running", "cancelling", "succeeded", "failed", "cancelled"]


if TYPE_CHECKING:
    from .app import ServiceResponse as BuildJobResponse
else:

    class BuildJobResponse(Protocol):
        """BuilderService response structure needed by job executor."""

        status_code: int
        body: dict[str, JsonValue]


class BuildJobRunner(Protocol):
    """Actual build execution entry point called by job worker.

    ``cancellation`` is per-run cooperative cancellation state (#481) — runner passes it
    straight down to pipeline without service concepts like registry/HTTP/Principal,
    keeping cancellation domain-agnostic.
    """

    def __call__(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: RunCancellation,
    ) -> BuildJobResponse: ...


SubmitStatus = Literal["accepted", "existing", "queue_full"]

# Result of ``AsyncBuildJobRegistry.request_cancel``. Each value maps to exactly one HTTP
# response (``BuilderService.cancel_build``) — deterministic regardless of races.
#   - "cancelled":  Terminated queued job before execution (runner called 0 times).
#   - "cancelling": Requested cancellation of running job. Actual termination at next safe boundary.
#   - "already":    Cancellation already requested (cancelling) or already cancelled.
#   - "terminal":   Already ended succeeded/failed, or pipeline past last safe boundary
#                   confirming normal termination (cannot cancel anymore).
#   - "unknown":    run_id unknown to registry.
CancelOutcome = Literal["cancelled", "cancelling", "already", "terminal", "unknown"]

# Terminal states that don't revert to running/cancelling.
_TERMINAL_STATUSES: frozenset[BuildJobStatus] = frozenset({"succeeded", "failed", "cancelled"})

# Number of terminal jobs registry holds in memory. Oldest are evicted first.
# Completed run's source of truth is manifest and event store, so this cache only
# covers "just finished run + polling client retrieves result" window.
_DEFAULT_MAX_TERMINAL_JOBS = 256


class RunCancellation:
    """Per-run cooperative cancellation state (#481).

    Structurally satisfies ``pipeline.cancellation.CancellationProbe`` —
    pipeline doesn't know this class or service layer, only calls ``cancel_requested()`` and
    ``commit()``. Instance per run, so one run's cancellation doesn't leak to another.

    Two monotonic latches only.

    ``_requested``
        Cancellation requested. Once True, never False again.
    ``_committed``
        Pipeline past last safe boundary, confirmed normal termination (success/failed
        manifest written). After this, ``request()`` always returns False.

    Both latches change only under same lock, so "commit and request succeed simultaneously"
    state is impossible — thus ``cancelling -> succeeded`` or "success manifest written then
    cancelled" reversal cannot happen structurally.
    """

    __slots__ = ("_committed", "_lock", "_requested")

    def __init__(self) -> None:
        self._lock = Lock()
        self._requested = False
        self._committed = False

    def request(self) -> bool:
        """Request cancellation. Returns True if not yet committed to normal termination.

        Calling again when already requested returns True (idempotent) —
        "cancellation is still valid" same answer.
        """
        with self._lock:
            if self._committed:
                return False
            self._requested = True
            return True

    def cancel_requested(self) -> bool:
        """``CancellationProbe``: Check if cancellation requested. Don't change state."""
        with self._lock:
            return self._requested

    def commit(self) -> bool:
        """``CancellationProbe``: Commit to normal termination if not yet cancelled. Latch."""
        with self._lock:
            if self._requested:
                return False
            self._committed = True
            return True

    def close(self) -> bool:
        """Close window when job terminates, return whether it ended as cancelled.

        Called exactly once right before ``AsyncBuildJobRegistry.finish`` confirms
        terminal state. Latches commit, so cancel requests arriving after finalization
        can't change already-confirmed terminal state.
        """
        with self._lock:
            self._committed = True
            return self._requested


@dataclass(frozen=True, slots=True)
class BuildJobSnapshot:
    """Build job state snapshot.

    ``owner_id`` is internal field for ownership check of active/terminal async runs
    (#496 follow-up, #505 canonical stable identity) — unlike ``created_by``
    (Principal.label, display/legacy fallback), this value takes priority for new
    ownership checks. ``BuilderService._run_build_job`` reuses it for persisted
    manifest/BuildIndex (#505 SSOT) recording. However, ``to_body()`` never exposes
    it on wire, and ``kind="file"`` source resolver (#498) doesn't receive it — async
    file-backed source owner propagation limitation remains.
    """

    run_id: str
    status: BuildJobStatus
    created_at: str
    updated_at: str
    created_by: str | None = None
    owner_id: str | None = None
    response: dict[str, JsonValue] | None = None
    error: str | None = None
    #: The BuildSpec's dataset_id, read at submit time so a table can say it is being
    #: refreshed while the run is still queued or running (#781). Internal, like
    #: ``owner_id`` — ``to_body()`` does not expose it. None when the spec could not
    #: be read; the run then simply does not show as in progress for any table.
    dataset_id: str | None = None
    #: The earlier run this one retries (#1042). On the wire, unlike the two above.
    retry_of: str | None = None

    def to_body(self) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "run_id": self.run_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.created_by is not None:
            body["created_by"] = self.created_by
        if self.response is not None:
            body["response"] = self.response
        if self.error is not None:
            body["error"] = self.error
            # The one failure a client acts on by its code (#996, #1070): the job's
            # keys are gone and submitting again works.
            if self.error.startswith("credentials_required:"):
                body["code"] = "credentials_required"
        if self.retry_of is not None:
            body["retry_of"] = self.retry_of
        return body


@dataclass(frozen=True, slots=True)
class BuildJobSubmitResult:
    status: SubmitStatus
    snapshot: BuildJobSnapshot | None = None


@dataclass(frozen=True, slots=True)
class AsyncBuildJobCounts:
    """``snapshot_counts()`` aggregates active job count in single lock scope (#516)."""

    queued: int
    running: int


class AsyncBuildJobRegistry:
    """In-memory registry of active/terminal jobs only.

    Terminal jobs are kept **in completion order, max ``max_terminal_jobs`` only**.
    Previously kept all forever, so ``_jobs`` on long-lived process held all runs executed
    — snapshots include success response body (``response``), so each job isn't tiny.
    Only restart reduced size.

    Only memory cache is evicted. Run's source of truth lives in two places —
    ``manifest.json`` of completed run and append-only event store. So
    ``GET /builds/{run_id}/events`` and manifest-based queries answer after eviction,
    and ownership checks manifests first (``_guards``) so runs with artifacts retain
    ownership. Only ``GET /builds/{run_id}`` returns 404 for very old runs.
    """

    def __init__(self, *, max_terminal_jobs: int = _DEFAULT_MAX_TERMINAL_JOBS) -> None:
        self._lock = Lock()
        self._jobs: dict[str, BuildJobSnapshot] = {}
        # run_id -> cooperative cancellation state (#481). Same lifecycle as snapshot,
        # mutable state never exposed externally (``cancellation()`` returns narrow
        # request/probe API only).
        self._cancellations: dict[str, RunCancellation] = {}
        # run_id in completion order. Must evict by termination order (``_jobs`` insertion
        # order), not creation order — long-running jobs submitted first shouldn't evict
        # just-finished ones.
        self._terminal_order: deque[str] = deque()
        self._max_terminal_jobs = max(0, max_terminal_jobs)

    def create(
        self,
        *,
        run_id: str,
        created_by: str | None,
        owner_id: str | None = None,
        dataset_id: str | None = None,
        retry_of: str | None = None,
    ) -> BuildJobSnapshot:
        now = _utc_now_text()
        snapshot = BuildJobSnapshot(
            run_id=run_id,
            status="queued",
            created_at=now,
            updated_at=now,
            created_by=created_by,
            owner_id=owner_id,
            dataset_id=dataset_id,
            retry_of=retry_of,
        )
        with self._lock:
            self._jobs[run_id] = snapshot
            self._cancellations[run_id] = RunCancellation()
        return snapshot

    def try_create(
        self,
        *,
        run_id: str,
        created_by: str | None,
        owner_id: str | None = None,
        max_queued: int,
        dataset_id: str | None = None,
        retry_of: str | None = None,
    ) -> tuple[str, BuildJobSnapshot | None]:
        """Check existence/queue capacity/create in **single lock scope** (#482 follow-up).

        Calling ``get`` → ``queued_count`` → ``create`` separately allows another thread
        between calls. Concurrent POST with same run_id both pass existence check, call
        ``on_accept`` twice, log event twice, exceed queue limit.

        Returns: ``("existing"|"queue_full"|"created", snapshot|None)``.
        """
        now = _utc_now_text()
        with self._lock:
            existing = self._jobs.get(run_id)
            if existing is not None:
                return "existing", existing
            queued = sum(1 for job in self._jobs.values() if job.status == "queued")
            if queued >= max_queued:
                return "queue_full", None
            snapshot = BuildJobSnapshot(
                run_id=run_id,
                status="queued",
                created_at=now,
                updated_at=now,
                created_by=created_by,
                owner_id=owner_id,
                dataset_id=dataset_id,
                retry_of=retry_of,
            )
            self._jobs[run_id] = snapshot
            self._cancellations[run_id] = RunCancellation()
            return "created", snapshot

    def cancellation(self, run_id: str) -> RunCancellation | None:
        """Return this run's cooperative cancellation state (#481). None if not found."""
        with self._lock:
            return self._cancellations.get(run_id)

    def begin_run(self, run_id: str) -> bool:
        """Called right before worker executes runner. Return True if start is allowed (#481).

        Make ``queued -> running`` transition atomic under single lock scope —
        cancel request and worker start both can happen simultaneously; exactly one path
        succeeds.

        - If cancel wins: job becomes ``queued -> cancelled``, this returns False,
          **runner never called** (pipeline artifact count = 0).
        - If worker wins: job becomes ``queued -> running``; subsequent cancel becomes
          ``running -> cancelling``, terminating at next safe boundary.

        Reversals like ``cancelled -> running`` are structurally impossible due to
        this atomic decision.
        """
        with self._lock:
            current = self._jobs.get(run_id)
            if current is None or current.status != "queued":
                return False
            self._jobs[run_id] = _transition(current, status="running")
            return True

    def request_cancel(self, run_id: str) -> tuple[CancelOutcome, BuildJobSnapshot | None]:
        """Request cancellation. Atomically decide state transition and outcome.

        Acquire registry lock then run cancellation lock (documented only lock order).
        Neither critical section performs external calls/I/O.
        """
        with self._lock:
            current = self._jobs.get(run_id)
            if current is None:
                return "unknown", None
            if current.status in _TERMINAL_STATUSES:
                # Repeat cancel requests on already-cancelled get "already cancelled" response,
                # making calls deterministic.
                outcome: CancelOutcome = "already" if current.status == "cancelled" else "terminal"
                return outcome, current
            cancellation = self._cancellations.get(run_id)
            if cancellation is not None and not cancellation.request():
                # Pipeline already past last safe boundary, committed to normal termination —
                # will soon be succeeded/failed. Changing to cancelling here would create
                # forbidden transition "cancelling -> succeeded".
                return "terminal", current
            if current.status == "queued":
                cancelled = _transition(current, status="cancelled")
                self._jobs[run_id] = cancelled
                self._retire_locked(run_id)
                return "cancelled", cancelled
            if current.status == "cancelling":
                return "already", current
            cancelling = _transition(current, status="cancelling")
            self._jobs[run_id] = cancelling
            return "cancelling", cancelling

    def finish(
        self,
        run_id: str,
        *,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
        failed: bool,
    ) -> BuildJobSnapshot | None:
        """Confirm terminal state after runner exits (#481).

        **Only** place that decides terminal state. ``RunCancellation.close()`` closes
        cancellation window, reporting "ended as cancelled", so same run can't record both
        succeeded and cancelled. Cancelled jobs carry no build response body or error
        string — cancellation isn't failure, and partial execution output isn't exposed
        as success response (partial artifact's source of truth is partial manifest).

        Already-terminal jobs unchanged (idempotent) — don't overwrite confirmed terminal state.

        **Cancellation precedence scope (explicit policy)**: If ``close()`` returns True,
        job is ``cancelled`` regardless of runner result. Includes case where execution
        ended before pipeline reached safe boundary to observe cancellation (e.g.,
        ``BuilderService.build()`` returned 400 on pre-pipeline spec validation). That run
        wrote no manifest/artifact, so reporting ``cancelled`` matches both user request
        and actual result (nothing created), and doesn't create forbidden transition
        ``cancelling -> failed``.

        Conversely, pipeline-actual failures are never swallowed by cancellation —
        ``commit()`` closes cancellation window before writing success manifest, so later
        cancel requests are rejected (``request_cancel`` returns "terminal"), and
        ``close()`` also returns False.
        """
        with self._lock:
            current = self._jobs.get(run_id)
            if current is None:
                return None
            if current.status in _TERMINAL_STATUSES:
                return current
            cancellation = self._cancellations.get(run_id)
            cancelled = cancellation.close() if cancellation is not None else False
            if cancelled:
                updated = _transition(current, status="cancelled")
            elif failed:
                updated = _transition(current, status="failed", response=response, error=error)
            else:
                updated = _transition(current, status="succeeded", response=response)
            self._jobs[run_id] = updated
            self._retire_locked(run_id)
            return updated

    def mark_failed(
        self, run_id: str, *, response: dict[str, JsonValue] | None = None, error: str | None = None
    ) -> BuildJobSnapshot:
        """Mark job as failed (submission-path-only, #496).

        Use ``finish()`` for actual execution result termination — this method is for
        jobs that never even got queued to worker (``AsyncBuildExecutor.submit`` enqueue
        failure). Keeps phantom queued from lingering. At that point, cancel requests
        can't yet arrive (submission response hasn't returned) so no cancellation race.
        """
        return self._replace(run_id, status="failed", response=response, error=error)

    def list_all(self) -> list[BuildJobSnapshot]:
        """Every job the registry still holds, in-flight ones included (#679).

        The administrator view needs these. ``BuildIndex`` is only written once a
        build has produced a manifest, so a queued or running job does not appear
        there at all -- and a stuck run is exactly what an operator looks for.
        """
        with self._lock:
            return list(self._jobs.values())

    def get(self, run_id: str) -> BuildJobSnapshot | None:
        with self._lock:
            return self._jobs.get(run_id)

    def discard(self, run_id: str) -> None:
        """Reset job to non-existent right after creation.

        If ``on_accept`` fails, ``run_submitted`` doesn't persist to event store, so
        job exists only in registry — becomes phantom queued, takes queue capacity but
        no one observes it.
        """
        with self._lock:
            self._jobs.pop(run_id, None)
            self._cancellations.pop(run_id, None)

    def queued_count(self) -> int:
        with self._lock:
            return sum(1 for job in self._jobs.values() if job.status == "queued")

    def active_snapshots(self) -> tuple[BuildJobSnapshot, ...]:
        """Jobs still queued or running (``cancelling`` counts as running), in one lock.

        For showing that a table is being refreshed (#781). Copies, never the dict.
        """
        with self._lock:
            return tuple(
                job
                for job in self._jobs.values()
                if job.status in ("queued", "running", "cancelling")
            )

    def snapshot_counts(self) -> AsyncBuildJobCounts:
        """Aggregate queued/running job count in single lock scope (#516).

        Calling separate methods at different times to combine them creates inconsistent
        snapshot from state transitions between calls (queued -> running) — monitoring must
        compute atomically like this method. Terminal (succeeded/failed/cancelled) jobs
        are kept in registry but not included here — monitoring shows current workload,
        not terminal history. Never expose mutable ``_jobs`` dict externally.

        ``cancelling`` (#481) counts as running — cancellation requested but job still
        occupies worker slot and is part of current workload. Excluding it would
        underreport monitoring worker utilization. When cancellation terminates, it
        becomes terminal (cancelled) and naturally drops from aggregation.
        """
        with self._lock:
            queued = 0
            running = 0
            for job in self._jobs.values():
                if job.status == "queued":
                    queued += 1
                elif job.status in ("running", "cancelling"):
                    running += 1
        return AsyncBuildJobCounts(queued=queued, running=running)

    def _retire_locked(self, run_id: str) -> None:
        """Record that job just became terminal; evict oldest if over limit.

        Must hold ``self._lock``. Single run_id never appears twice —
        all paths changing to terminal filter already-terminal first.
        """
        self._terminal_order.append(run_id)
        while len(self._terminal_order) > self._max_terminal_jobs:
            evicted = self._terminal_order.popleft()
            job = self._jobs.get(evicted)
            # Ignore already-discarded entries (shouldn't happen) or items that somehow
            # became non-terminal — evicting a live job is much worse than exceeding
            # limit slightly.
            if job is not None and job.status in _TERMINAL_STATUSES:
                del self._jobs[evicted]
                self._cancellations.pop(evicted, None)

    def _replace(
        self,
        run_id: str,
        *,
        status: BuildJobStatus,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> BuildJobSnapshot:
        with self._lock:
            current = self._jobs[run_id]
            # Don't overwrite confirmed terminal state (#481) — prevent reversals like
            # cancelled -> failed.
            if current.status in _TERMINAL_STATUSES:
                return current
            updated = _transition(current, status=status, response=response, error=error)
            self._jobs[run_id] = updated
            if status in _TERMINAL_STATUSES:
                self._retire_locked(run_id)
            return updated


@dataclass(frozen=True, slots=True)
class AsyncBuildStats:
    """Monitoring API (#516) consumes raw async build executor aggregates.

    Hold only ``queued``/``running``/``capacity`` raw values — derived values
    (``total``/``active``/``utilization``) and availability judgment are
    ``service/monitoring.py``'s responsibility.
    """

    queued: int
    running: int
    capacity: int


class AsyncBuildExecutor:
    """Execute build jobs with fixed-size worker pool."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_queue_size: int = 10,
        max_terminal_jobs: int = _DEFAULT_MAX_TERMINAL_JOBS,
        on_cancelled: Callable[[str], None] | None = None,
        build_slots: BuildSlots | None = None,
    ) -> None:
        # Shared with the synchronous build path (#1028). A worker takes its slot before
        # the job is marked running, so a job waiting for one stays queued (#1040).
        self._build_slots = build_slots
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="kpubdata-build"
        )
        # Explicitly preserve capacity at creation time so external code (monitoring)
        # doesn't read private ``ThreadPoolExecutor._max_workers`` (#516).
        self._max_workers = max_workers
        self._max_queue_size = max_queue_size
        # Called exactly once when running job actually terminates as cancelled at safe
        # boundary (#481). Caller (BuilderService) appends termination event
        # (run_cancelled) via hook — called from worker thread, so hook must not
        # propagate exceptions (caller responsibility).
        self._on_cancelled = on_cancelled
        self.registry = AsyncBuildJobRegistry(max_terminal_jobs=max_terminal_jobs)

    def stats(self) -> AsyncBuildStats:
        """Read-only aggregate snapshot for monitoring (#516).

        Don't expose mutable internal job dict; return queued/running aggregates
        (``registry.snapshot_counts()``) obtained in single lock scope plus worker pool
        capacity. This call itself doesn't change job state.
        """
        counts = self.registry.snapshot_counts()
        return AsyncBuildStats(
            queued=counts.queued, running=counts.running, capacity=self._max_workers
        )

    def submit(
        self,
        *,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
        owner_id: str | None = None,
        on_accept: Callable[[], None] | None = None,
        on_enqueue_failure: Callable[[], None] | None = None,
        dataset_id: str | None = None,
        retry_of: str | None = None,
    ) -> BuildJobSubmitResult:
        """Queue job. If "existing"/"queue_full", new submission not counted, so
        ``on_accept`` not called.

        ``owner_id`` passes only to ``registry.create()`` — persisted in snapshot (#496
        follow-up, active run ownership check) — NOT passed to ``runner`` invocation
        (below: ``self._executor.submit(self._run, spec_yaml, run_id, created_by, runner)``).
        run_build/source resolver owner propagation already has separate limitation (#498);
        this change doesn't widen that scope.

        ``on_accept`` called before job queued to worker pool (``self._executor.submit``)
        (#496). Caller (``BuilderService.submit_build``) appends "run_submitted" event via
        hook — if append fails and raises, job neither created in registry nor queued to
        worker. This order prevents "event lost but job running" contradiction — job
        "accepted" status depends on this event record success. Callers without ``on_accept``
        (legacy monitoring tests) queue normally without gating.

        Reverse (#496 self-review): If ``on_accept`` succeeds (event recorded) but
        ``self._executor.submit()`` (actual worker pool queueing) fails, ``registry.create()``
        made "queued" item phantom forever — event (``run_submitted``) append-only so
        can't delete (#496 principle). Replace with ``registry.mark_failed()`` (same
        terminal mechanism already used for job execution failure) then re-raise —
        don't create new state.

        ``on_enqueue_failure`` called right after ``registry.mark_failed()``, before
        re-raising (#496 lifecycle contract: timeline itself must express this failure) —
        caller appends existing ``run_failed`` event for same run_id. Distinct from
        ``on_accept`` failure path (event never recorded at all) — that path never reaches
        here because it doesn't get past ``registry.create()``, leaving ``run_submitted``
        and ``run_failed`` both unrecorded.
        """
        # Check existence/capacity/create atomically. When done separately, concurrent
        # POSTs to same run_id both pass existence check, call ``on_accept`` twice, exceed
        # queue limit.
        outcome, snapshot = self.registry.try_create(
            run_id=run_id,
            created_by=created_by,
            owner_id=owner_id,
            max_queued=self._max_queue_size,
            dataset_id=dataset_id,
            retry_of=retry_of,
        )
        if outcome == "existing":
            return BuildJobSubmitResult(status="existing", snapshot=snapshot)
        if outcome == "queue_full":
            return BuildJobSubmitResult(status="queue_full")
        assert snapshot is not None  # noqa: S101 - "created" always gives snapshot
        if on_accept is not None:
            # Called after creation. If hook raises, clean up job below —
            # previously called before, allowing both requests to reach here.
            try:
                on_accept()
            except Exception:
                self.registry.discard(run_id)
                raise
        try:
            self._executor.submit(self._run, spec_yaml, run_id, created_by, runner)
        except Exception:
            self.registry.mark_failed(run_id, error="failed to queue build job")
            if on_enqueue_failure is not None:
                on_enqueue_failure()
            raise
        return BuildJobSubmitResult(status="accepted", snapshot=snapshot)

    def list_all(self) -> list[BuildJobSnapshot]:
        return self.registry.list_all()

    def get(self, run_id: str) -> BuildJobSnapshot | None:
        return self.registry.get(run_id)

    def request_cancel(self, run_id: str) -> tuple[CancelOutcome, BuildJobSnapshot | None]:
        """Request cancellation for this run (#481). Delegate to registry's atomic transition."""
        return self.registry.request_cancel(run_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
    ) -> None:
        """Worker thread entry point. Execute runner without holding any lock.

        If ``begin_run`` returns False, this job was cancelled before execution, so
        **don't call runner** (#481) — cancelled job doesn't start pipeline or create
        artifact. Termination event (``run_cancelled``) already recorded by cancel
        request that created this transition.
        """
        if self._build_slots is not None:
            # No timeout: this is the queue. A job cancelled while it waits is still
            # ``queued``; ``begin_run`` below refuses it and the slot goes straight back.
            self._build_slots.acquire()
        try:
            self._run_with_slot(spec_yaml, run_id, created_by, runner)
        finally:
            if self._build_slots is not None:
                self._build_slots.release()

    def _run_with_slot(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        runner: BuildJobRunner,
    ) -> None:
        if not self.registry.begin_run(run_id):
            return
        try:
            cancellation = self.registry.cancellation(run_id)
            if cancellation is None:  # pragma: no cover - create() always makes one together
                cancellation = RunCancellation()
            response = runner(spec_yaml, run_id, created_by, cancellation)
        except Exception as exc:  # noqa: BLE001 - any failure must terminate job
            # Old path caught only RuntimeError — other exceptions leaked from worker thread,
            # leaving job stuck in running. Polling client waits forever; queue slot never
            # returns. Whatever failed, our job is to confirm terminal state here.
            # Log exception string but don't expose in response — unexpected exceptions here
            # have unknown internals (paths, SQL, credentials). Include type name (which
            # layer) without arbitrary internal strings.
            _logger.exception("build job %s failed with an unhandled exception", run_id)
            self._finish(run_id, failed=True, error=f"internal error: {type(exc).__name__}")
            return
        if response.status_code < 400:
            self._finish(run_id, failed=False, response=response.body)
            return
        # Don't distinguish failure/cancellation by status_code alone here (#481). Cancelled
        # run's build() also returns 4xx (409 summary), but terminal state decided only by
        # ``registry.finish()``'s cancellation latch — so response/error discarded for
        # cancelled jobs (``finish()`` docs). Single decision source prevents "execution
        # result" and "cancellation status" giving conflicting answers.
        error = response.body.get("error")
        self._finish(
            run_id,
            failed=True,
            response=response.body,
            error=error if isinstance(error, str) else "build failed",
        )

    def _finish(
        self,
        run_id: str,
        *,
        failed: bool,
        response: dict[str, JsonValue] | None = None,
        error: str | None = None,
    ) -> None:
        """Confirm terminal state and call cancelled hook if ended as cancelled."""
        snapshot = self.registry.finish(run_id, response=response, error=error, failed=failed)
        if (
            snapshot is not None
            and snapshot.status == "cancelled"
            and self._on_cancelled is not None
        ):
            self._on_cancelled(run_id)


def _transition(
    current: BuildJobSnapshot,
    *,
    status: BuildJobStatus,
    response: dict[str, JsonValue] | None = None,
    error: str | None = None,
) -> BuildJobSnapshot:
    """Create new snapshot with state transition. Caller must hold registry lock."""
    return BuildJobSnapshot(
        run_id=current.run_id,
        status=status,
        created_at=current.created_at,
        updated_at=_utc_now_text(),
        created_by=current.created_by,
        owner_id=current.owner_id,
        response=response,
        error=error,
        # Carried through every transition: a table shows its refresh as running only
        # if the running snapshot still knows which table it belongs to (#781).
        dataset_id=current.dataset_id,
        # And so is the retry link: it is a fact about the run, not about one state (#1042).
        retry_of=current.retry_of,
    )


def _utc_now_text() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def generate_run_id() -> str:
    return f"{datetime.now(tz=timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex[:12]}"


__all__ = [
    "AsyncBuildExecutor",
    "AsyncBuildJobCounts",
    "AsyncBuildJobRegistry",
    "AsyncBuildStats",
    "BuildJobSubmitResult",
    "BuildJobResponse",
    "BuildJobSnapshot",
    "BuildJobStatus",
    "CancelOutcome",
    "RunCancellation",
    "SubmitStatus",
    "generate_run_id",
]

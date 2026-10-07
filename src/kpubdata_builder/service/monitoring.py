"""System Resource·Build Statistics query logic (#516).

Provides Builder API/Queue/Worker/Artifact Store status and BuildIndex-based
hourly build statistics for the Studio Monitoring screen. Run-level events
are handled by #496; this module only deals with system/aggregate observability.

Core principle ("do not fabricate missing state"):
    - Values never measured are represented as ``null``/``unavailable``, not
      disguised as 0/healthy.
    - ``Availability`` vocabulary is reused as already defined in ``quality.py``
      (``available``/``partial``/``unavailable``).

Async build execution model (queued/running worker pool) is already implemented
via ``jobs.AsyncBuildExecutor``/``AsyncBuildJobRegistry`` (#511/#513) and is
always created and used by ``BuilderService`` — queue/worker state directly
reflects the read-only snapshot of that executor. We don't pretend to have
non-existent executors or create new configurations without executors.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from ..store import BuildEntry, BuildIndex
from .auth import Principal, principal_owns
from .jobs import AsyncBuildExecutor
from .ownership import lists_only_own_runs
from .quality import Availability

# Latency samples use a fixed-size ring buffer of the most recent N requests,
# not a time window (#516) — memory upper bound is the goal, not time-based
# windowing like "last X minutes".
_LATENCY_WINDOW_SIZE = 1000

# Recent runs are a Monitoring card preview, so we expose only a small fixed
# count like /builds, not letting clients adjust the limit (#516).
_RECENT_RUNS_LIMIT = 10

BuildBucketWindow = Literal["24h"]
BuildBucketGranularity = Literal["hour"]

_SUPPORTED_WINDOWS: dict[str, int] = {"24h": 24 * 3600}
_SUPPORTED_BUCKETS: dict[str, int] = {"hour": 3600}


class LatencyRecorder:
    """Bounded, thread-safe in-memory Builder API request latency recorder (#516).

    Records the entire ``dispatch()`` execution time (routing+auth+business logic)
    in milliseconds. Does not include actual HTTP socket I/O (that is the http.py
    layer).
    """

    def __init__(self, *, max_samples: int = _LATENCY_WINDOW_SIZE) -> None:
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._lock = threading.Lock()

    def record(self, latency_ms: float) -> None:
        """Record a sample. Does not propagate exceptions even if recording fails (#516)."""
        try:
            with self._lock:
                self._samples.append(latency_ms)
        except Exception:
            # Metric collection failure must not propagate to request failure (#516).
            pass

    def snapshot(self) -> tuple[int, float | None] | None:
        """Return ``(sample_count, p95_latency_ms)``. If no samples, p95 is None.

        Returns ``None`` if the collector itself fails (e.g., lock corruption) (#527) —
        to distinguish "valid zero samples" (``(0, None)``) from "measurement
        subsystem failure". This failure does not propagate as an exception; the
        caller (``api_status()``) maps ``None`` to ``unavailable``.
        """
        try:
            with self._lock:
                samples = list(self._samples)
        except Exception:
            return None
        return _p95(samples)


def _p95(samples: list[float]) -> tuple[int, float | None]:
    """Compute p95 using nearest-rank method (#516, no interpolation).

    Uses ``rank = clamp(ceil(0.95 * n), 1, n)`` as the 1-indexed position in the
    sorted samples. Examples: n=1 -> rank=1 (that sample itself), n=20 -> rank=19.
    """
    n = len(samples)
    if n == 0:
        return 0, None
    ordered = sorted(samples)
    rank = max(1, min(math.ceil(0.95 * n), n))
    return n, ordered[rank - 1]


@dataclass(frozen=True)
class ApiStatus:
    availability: Availability
    sample_count: int | None
    p95_latency_ms: float | None


def api_status(recorder: LatencyRecorder) -> ApiStatus:
    """Return Builder API status.

    The fact that ``dispatch()`` ran and this function was called proves the
    process is responding. However, we do not disguise as ``available`` even
    if the latency collector (``LatencyRecorder``) itself is corrupted and
    cannot read samples (#527) — if ``recorder.snapshot()`` returns ``None``
    (collector failure), status is ``unavailable`` + ``sample_count=None`` +
    ``p95_latency_ms=None``; if it is legitimately zero-sampled (``(0, None)``),
    status is ``available`` + ``sample_count=0`` + ``p95_latency_ms=None`` —
    distinguishing "zero samples" from "measurement unavailable". This subsystem
    judgment failure does not fail the original request (``dispatch``) or this
    monitoring request itself (``snapshot()`` does not propagate exceptions).
    Latency threshold judgments (Healthy/Degraded) lack justification
    (ADR/config), so they are not invented in this PR — only raw
    ``sample_count``/``p95_latency_ms`` are provided.
    """
    snapshot = recorder.snapshot()
    if snapshot is None:
        return ApiStatus(availability="unavailable", sample_count=None, p95_latency_ms=None)
    sample_count, p95 = snapshot
    return ApiStatus(availability="available", sample_count=sample_count, p95_latency_ms=p95)


@dataclass(frozen=True)
class QueueStatus:
    availability: Availability
    waiting: int | None
    running: int | None
    total: int | None


@dataclass(frozen=True)
class WorkerStatus:
    availability: Availability
    active: int | None
    capacity: int | None
    utilization: float | None


def queue_status(async_builds: AsyncBuildExecutor) -> QueueStatus:
    """Async build queue status (#516).

    Directly reflects the read-only snapshot (``stats()``) of the
    ``AsyncBuildExecutor`` (#511/#513) that ``BuilderService`` always creates —
    async build is always supported in this service, so availability is always
    ``available``. ``total`` is ``waiting + running`` (terminal succeeded/failed/
    cancelled history is not workload state, so we exclude it — the registry may
    preserve these in memory, but ``stats()`` already excludes them).
    """
    stats = async_builds.stats()
    return QueueStatus(
        availability="available",
        waiting=stats.queued,
        running=stats.running,
        total=stats.queued + stats.running,
    )


def worker_status(async_builds: AsyncBuildExecutor) -> WorkerStatus:
    """Async build worker pool status. See ``queue_status`` documentation for justification (#516).

    ``active`` is the current running job count (one worker per job, so
    running == active), ``capacity`` is the ``max_workers`` preserved at executor
    creation. We do not directly read private fields of ``ThreadPoolExecutor`` —
    ``AsyncBuildExecutor.stats()`` already exposes capacity.
    """
    stats = async_builds.stats()
    utilization = (stats.running / stats.capacity) if stats.capacity > 0 else 0.0
    return WorkerStatus(
        availability="available",
        active=stats.running,
        capacity=stats.capacity,
        utilization=utilization,
    )


@dataclass(frozen=True)
class ArtifactStoreStatus:
    availability: Availability
    last_write_at: str | None


def artifact_store_status(output_root: Path, build_index: BuildIndex) -> ArtifactStoreStatus:
    """Artifact Store status (#516).

    We do not consider the folder existing alone as healthy — ``output_root``
    must be accessible as a directory and BuildIndex query must also succeed
    for status to be ``available``. ``last_write_at`` is obtained only from
    ``finished_at`` of the most recent successful (``ok``) build actually
    recorded in BuildIndex; if no success record exists, status is ``available``
    but ``last_write_at`` is ``null`` — distinguishing "zero records" from
    "unable to verify".
    """
    if not output_root.exists() or not output_root.is_dir():
        return ArtifactStoreStatus(availability="unavailable", last_write_at=None)
    try:
        last_write_at = build_index.latest_successful_finished_at()
    except Exception:
        return ArtifactStoreStatus(availability="unavailable", last_write_at=None)
    return ArtifactStoreStatus(availability="available", last_write_at=last_write_at)


MonitoringAggregateStatus = Literal["healthy", "degraded"]


def aggregate_status(
    *,
    api: ApiStatus,
    queue: QueueStatus,
    workers: WorkerStatus,
    artifact_store: ArtifactStoreStatus,
) -> MonitoringAggregateStatus:
    """Determine deterministic aggregate status from required subsystem availability (#516).

    Latency thresholds (SLA) are not used due to lack of justification (ADR/config) —
    ``sample_count=0``/``p95_latency_ms=None`` could be startup/zero-sample state,
    so these are not degraded reasons by themselves (only ``api.availability`` is
    considered). Provider status is optional per #516, so it is not included in
    this judgment (the caller does not pass it at all).

    If all required subsystems (api/queue/workers/artifact_store) have
    availability ``available``, status is ``healthy``; if any has
    ``partial``/``unavailable``, status is ``degraded``. Actual zero (waiting/running/
    active=0) for queue/workers is independent of availability, so it does not
    affect this judgment — only when availability itself is unavailable is it
    reflected as degraded.
    """
    required_availabilities = (
        api.availability,
        queue.availability,
        workers.availability,
        artifact_store.availability,
    )
    if all(availability == "available" for availability in required_availabilities):
        return "healthy"
    return "degraded"


@dataclass(frozen=True)
class BuildBucket:
    """Build count per hourly bucket (#516 wire contract: total/success/failed/cancelled).

    ``success`` maps the internal BuildIndex/BuildEntry.status value ``"ok"``
    to external Monitoring API naming (#527) — internal BuildIndex status
    vocabulary (``ok``/``failed``/``cancelled``) is kept as-is; only the wire
    field name is changed to match the contract.
    """

    bucket_start: str
    bucket_end: str
    total: int
    success: int
    failed: int
    cancelled: int


@dataclass(frozen=True)
class RecentRun:
    run_id: str
    status: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True)
class BuildStatistics:
    window: str
    bucket: str
    availability: Availability
    excluded_count: int
    buckets: tuple[BuildBucket, ...]
    recent_runs: tuple[RecentRun, ...]


def validate_window(window: str) -> BuildBucketWindow | None:
    """Return the window value if supported, else None (#516 — not broadening scope)."""
    return "24h" if window == "24h" else None


def validate_bucket(bucket: str) -> BuildBucketGranularity | None:
    """Return the bucket value if supported, else None (#516 — not broadening scope)."""
    return "hour" if bucket == "hour" else None


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse ISO 8601 string to UTC ``datetime``. Return None if invalid or None.

    Strict validation to avoid silently including malformed/legacy timestamps —
    the caller counts excluded rows as ``excluded_count``, which reflects in the
    partial judgment.
    """
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _bucket_start(dt: datetime, bucket_seconds: int) -> datetime:
    epoch_seconds = int(dt.timestamp())
    floored = (epoch_seconds // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def _isoformat_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _should_enforce_ownership(principal: Principal | None, *, enforce: bool) -> bool:
    """Determine whether to apply ownership filter (#516, #505).

    ``ownership.lists_only_own_runs``, as ``builds_api._apply_ownership`` and
    ``datasets.filter_ownership`` ask it — every principal but ``dev`` where ownership
    is enforced, the API key included (#1091). Both ``_filter_ownership`` (Python post-filter) and
    ``BuildIndex.list_recent_owned`` (SQL push-down, #527) share this judgment
    to keep policy aligned.
    """
    return lists_only_own_runs(principal, enforce=enforce)


def _filter_ownership(
    entries: list[BuildEntry], principal: Principal | None, *, enforce: bool
) -> list[BuildEntry]:
    """Apply existing ownership policy to BuildEntry list (#516, #505).

    Prevent monitoring aggregates and recent runs from becoming a side channel
    exposing other principals' run metadata. Used only after loading the full
    window (e.g., ``raw_entries``) where LIMIT loss is not a concern — recent
    runs queries with LIMIT use ``BuildIndex.list_recent_owned`` instead to apply
    the filter first in SQL (#527, see ``build_statistics`` below).
    """
    if not _should_enforce_ownership(principal, enforce=enforce):
        return entries
    assert principal is not None  # _should_enforce_ownership already guarantees
    return [
        e
        for e in entries
        if principal_owns(created_by=e.created_by, owner_id=e.owner_id, principal=principal)
    ]


def build_statistics(
    build_index: BuildIndex,
    *,
    window: BuildBucketWindow,
    bucket: BuildBucketGranularity,
    principal: Principal | None,
    enforce_ownership: bool,
    now: datetime | None = None,
) -> BuildStatistics:
    """Aggregate build statistics and recent runs by window/bucket (#516).

    Timezone: UTC. Bucket boundaries are ``[start, end)`` half-open. Bucket
    reference timestamp is ``finished_at`` (BuildIndex records only completed
    builds, ADR 0003).

    - If BuildIndex query itself fails, ``unavailable`` (empty buckets).
    - If query succeeds but some rows excluded due to malformed timestamp,
      ``partial``.
    - If query succeeds + no exclusions, ``available`` (valid even at zero count).

    Recent runs are bounded by ``_RECENT_RUNS_LIMIT`` (10). If ownership must be
    enforced (#505), that filter is applied **before** LIMIT in SQL (#527) — if
    we fetch the global latest 10 first and filter in Python, other principals'
    recent runs can fill the LIMIT, cutting off the requester's recent run.
    Bucket aggregates (``raw_entries``) already load the full window without LIMIT,
    so this problem does not occur; they use post-filter (Python) as before.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    window_seconds = _SUPPORTED_WINDOWS[window]
    bucket_seconds = _SUPPORTED_BUCKETS[bucket]
    window_end = _bucket_start(current, bucket_seconds) + timedelta(seconds=bucket_seconds)
    window_start = window_end - timedelta(seconds=window_seconds)

    try:
        raw_entries = build_index.list_between(_isoformat_z(window_start), _isoformat_z(window_end))
        if _should_enforce_ownership(principal, enforce=enforce_ownership):
            assert principal is not None  # _should_enforce_ownership already guarantees
            recent_entries = build_index.list_recent_owned(
                limit=_RECENT_RUNS_LIMIT,
                principal_owner_id=principal.owner_id,
                principal_label=principal.label,
            )
        else:
            recent_entries = build_index.list_builds(limit=_RECENT_RUNS_LIMIT)
    except Exception:
        return BuildStatistics(
            window=window,
            bucket=bucket,
            availability="unavailable",
            excluded_count=0,
            buckets=(),
            recent_runs=(),
        )

    scoped_entries = _filter_ownership(raw_entries, principal, enforce=enforce_ownership)
    # recent_entries were already filtered above by SQL push-down if needed (#527) —
    # do not apply Python filter again here (not a duplicate anyway, and with LIMIT
    # already applied, we cannot recover lost records).
    scoped_recent = recent_entries

    bucket_count = window_seconds // bucket_seconds
    bucket_starts = [
        window_start + timedelta(seconds=i * bucket_seconds) for i in range(bucket_count)
    ]
    counters: dict[str, dict[str, int]] = {
        _isoformat_z(start): {"total": 0, "ok": 0, "failed": 0, "cancelled": 0}
        for start in bucket_starts
    }

    excluded_count = 0
    for entry in scoped_entries:
        parsed = _parse_iso_utc(entry.finished_at)
        if parsed is None:
            excluded_count += 1
            continue
        key = _isoformat_z(_bucket_start(parsed, bucket_seconds))
        counter = counters.get(key)
        if counter is None:
            # Window query itself limits to [start,end), so this normally does not occur,
            # but defensive handling for floating-point/parsing variance near boundaries.
            excluded_count += 1
            continue
        counter["total"] += 1
        if entry.status in ("ok", "failed", "cancelled"):
            counter[entry.status] += 1

    buckets = tuple(
        BuildBucket(
            bucket_start=_isoformat_z(start),
            bucket_end=_isoformat_z(start + timedelta(seconds=bucket_seconds)),
            total=counters[_isoformat_z(start)]["total"],
            # Internal BuildIndex status "ok" -> external wire field "success" (#527).
            success=counters[_isoformat_z(start)]["ok"],
            failed=counters[_isoformat_z(start)]["failed"],
            cancelled=counters[_isoformat_z(start)]["cancelled"],
        )
        for start in bucket_starts
    )
    recent_runs = tuple(
        RecentRun(
            run_id=e.run_id, status=e.status, started_at=e.started_at, finished_at=e.finished_at
        )
        for e in scoped_recent
    )

    availability: Availability = "partial" if excluded_count > 0 else "available"
    return BuildStatistics(
        window=window,
        bucket=bucket,
        availability=availability,
        excluded_count=excluded_count,
        buckets=buckets,
        recent_runs=recent_runs,
    )


__all__ = [
    "ApiStatus",
    "ArtifactStoreStatus",
    "BuildBucket",
    "BuildStatistics",
    "LatencyRecorder",
    "MonitoringAggregateStatus",
    "QueueStatus",
    "RecentRun",
    "WorkerStatus",
    "aggregate_status",
    "api_status",
    "artifact_store_status",
    "build_statistics",
    "queue_status",
    "validate_bucket",
    "validate_window",
    "worker_status",
]

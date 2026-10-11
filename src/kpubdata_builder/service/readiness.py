"""Readiness, kept apart from liveness (#1101).

``GET /healthz`` says the process answers and nothing more; it touches no file. That
is what a container runtime should restart on. ``GET /readyz`` says whether this
process can do its work now: the build index can be read, the output directory can be
written, the disk under it has room, and the build workers have not been stopped.

What it does not judge, on purpose:

- **A full build queue.** That is the build queue's state, not the service's: sign-in,
  cancel and every read still work. It is reported as ``accepting_builds`` and leaves
  ``status`` alone.
- **The identity provider's signing keys.** Fetching them here would turn every probe
  into a call to the identity provider. A request that cannot be judged without them
  answers 503 ``auth_unavailable`` by itself (since 1.79.0).
- **A schema migration.** A store a newer release wrote stops the start (#1096); a
  process that is running has already passed that.

A probe is bounded three ways: the checks run at most once per ``cache_seconds`` and
callers that arrive meanwhile get the same answer (one computation in flight, never
several); the filesystem part runs on its own thread and is given ``timeout_seconds``
— a hung mount is then ``timeout``, not a hung probe — and while one such thread is
still stuck no second one is started. Nothing in the answer names a path, a size or
an error text.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..spec import JsonValue
from ..store import BuildIndex
from .jobs import AsyncBuildExecutor

MIN_FREE_PERCENT_ENV = "KPUBDATA_BUILDER_READY_MIN_FREE_PERCENT"
MIN_FREE_MB_ENV = "KPUBDATA_BUILDER_READY_MIN_FREE_MB"
#: The issue's proposal (#1101), not a measured value (kpubdata#812).
DEFAULT_MIN_FREE_PERCENT = 15.0
#: One GiB: room for a build's spill and a SQLite journal. Chosen without measurement.
DEFAULT_MIN_FREE_MB = 1024

#: How long one answer is reused. Short enough that an outage shows within one
#: external check interval, long enough that a burst of probes costs one check.
DEFAULT_CACHE_SECONDS = 5.0
#: How long the filesystem checks are given before they count as failed.
DEFAULT_TIMEOUT_SECONDS = 2.0

CheckState = Literal["ok", "failed", "timeout", "low", "draining", "stopped"]


@dataclass(frozen=True)
class DiskThresholds:
    """The free space below which the disk is ``low``. ``0`` turns a bound off."""

    min_free_percent: float
    min_free_mb: int


def disk_thresholds() -> DiskThresholds:
    """The thresholds from the environment, or the defaults.

    Raises:
        ValueError: A value is set and is not usable: the percentage must be a finite
            number from 0 to 100 and the size a whole number of MB, 0 or more.
            ``serve`` refuses to start on it (``startup_settings``).
    """
    raw_percent = os.environ.get(MIN_FREE_PERCENT_ENV, "").strip()
    percent = DEFAULT_MIN_FREE_PERCENT
    if raw_percent:
        try:
            percent = float(raw_percent)
        except ValueError:
            percent = math.nan
        if not (math.isfinite(percent) and 0 <= percent <= 100):
            raise ValueError(
                f"{MIN_FREE_PERCENT_ENV} must be a number from 0 to 100, got {raw_percent!r}"
            )
    raw_mb = os.environ.get(MIN_FREE_MB_ENV, "").strip()
    megabytes = DEFAULT_MIN_FREE_MB
    if raw_mb:
        try:
            megabytes = int(raw_mb)
        except ValueError:
            megabytes = -1
        if megabytes < 0:
            raise ValueError(f"{MIN_FREE_MB_ENV} must be an integer >= 0, got {raw_mb!r}")
    return DiskThresholds(min_free_percent=percent, min_free_mb=megabytes)


@dataclass(frozen=True)
class DiskUsage:
    free_bytes: int
    total_bytes: int


@dataclass(frozen=True)
class Readiness:
    """One readiness answer. ``disk`` is kept for the metrics and never sent by ``/readyz``."""

    checks: dict[str, CheckState]
    accepting_builds: bool
    disk: DiskUsage | None

    @property
    def ready(self) -> bool:
        return all(state == "ok" for state in self.checks.values())

    def body(self) -> dict[str, JsonValue]:
        return {
            "status": "ready" if self.ready else "not_ready",
            "checks": {name: state for name, state in self.checks.items()},
            "accepting_builds": self.accepting_builds,
        }


@dataclass(frozen=True)
class _FilesystemResult:
    state_store: CheckState
    output_dir: CheckState
    disk: CheckState
    usage: DiskUsage | None


def _disk_state(usage: DiskUsage, thresholds: DiskThresholds) -> CheckState:
    if usage.total_bytes <= 0:
        return "failed"
    free_percent = usage.free_bytes * 100 / usage.total_bytes
    if free_percent < thresholds.min_free_percent:
        return "low"
    if usage.free_bytes < thresholds.min_free_mb * 1024 * 1024:
        return "low"
    return "ok"


def _check_filesystem(
    output_root: Path,
    build_index: BuildIndex,
    disk_usage: Callable[[Path], DiskUsage],
) -> _FilesystemResult:
    """The checks that touch the disk. Each failure is caught and named, never raised."""
    try:
        build_index.latest_successful_finished_at()
        state_store: CheckState = "ok"
    except Exception:  # noqa: BLE001 - any failure to read the index is "failed"
        state_store = "failed"
    # Where the SQLite stores and their journals are written. A file is created and
    # removed: a read-only remount or a full disk refuses that, and a permission bit
    # read alone would not show the first.
    try:
        with tempfile.NamedTemporaryFile(dir=output_root, prefix=".readyz-", delete=True):
            pass
        output_dir: CheckState = "ok"
    except Exception:  # noqa: BLE001 - any failure to write is "failed"
        output_dir = "failed"
    usage: DiskUsage | None
    try:
        usage = disk_usage(output_root)
        disk = _disk_state(usage, disk_thresholds())
    except Exception:  # noqa: BLE001 - an unreadable disk or threshold is "failed"
        usage = None
        disk = "failed"
    return _FilesystemResult(state_store, output_dir, disk, usage)


def _disk_usage(path: Path) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(free_bytes=usage.free, total_bytes=usage.total)


class ReadinessProbe:
    """Answers ``/readyz`` (#1101); see the module docstring for what it judges."""

    def __init__(
        self,
        *,
        output_root: Path,
        build_index: BuildIndex,
        async_builds: AsyncBuildExecutor,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        disk_usage: Callable[[Path], DiskUsage] = _disk_usage,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._output_root = output_root
        self._build_index = build_index
        self._async_builds = async_builds
        self._cache_seconds = cache_seconds
        self._timeout_seconds = timeout_seconds
        self._disk_usage = disk_usage
        self._clock = clock
        # Held while one computation runs: a caller that arrives meanwhile waits for it
        # and then reads its answer from the cache.
        self._compute_lock = threading.Lock()
        self._cached: tuple[float, Readiness] | None = None
        # The filesystem thread of an earlier probe that has not come back.
        self._stuck: threading.Thread | None = None

    def check(self) -> Readiness:
        cached = self._fresh()
        if cached is not None:
            return cached
        with self._compute_lock:
            cached = self._fresh()
            if cached is not None:
                return cached
            result = self._compute()
            self._cached = (self._clock(), result)
            return result

    def _fresh(self) -> Readiness | None:
        cached = self._cached
        if cached is not None and self._clock() - cached[0] < self._cache_seconds:
            return cached[1]
        return None

    def _compute(self) -> Readiness:
        filesystem = self._filesystem()
        if filesystem is None:
            filesystem = _FilesystemResult("timeout", "timeout", "timeout", None)
        stats = self._async_builds.stats()
        if self._async_builds.draining:
            workers: CheckState = "draining"
        elif self._async_builds.stopped:
            workers = "stopped"
        else:
            workers = "ok"
        accepting = workers == "ok" and stats.queued < self._async_builds.max_queue_size
        return Readiness(
            checks={
                "state_store": filesystem.state_store,
                "output_dir": filesystem.output_dir,
                "disk": filesystem.disk,
                "workers": workers,
            },
            accepting_builds=accepting,
            disk=filesystem.usage,
        )

    def _filesystem(self) -> _FilesystemResult | None:
        """The filesystem checks, or None when they did not finish in time."""
        if self._stuck is not None and self._stuck.is_alive():
            return None
        self._stuck = None
        outcome: list[_FilesystemResult] = []

        def run() -> None:
            outcome.append(
                _check_filesystem(self._output_root, self._build_index, self._disk_usage)
            )

        thread = threading.Thread(target=run, name="kpubdata-readyz", daemon=True)
        thread.start()
        thread.join(self._timeout_seconds)
        if thread.is_alive():
            self._stuck = thread
            return None
        return outcome[0] if outcome else None


__all__ = [
    "DEFAULT_MIN_FREE_MB",
    "DEFAULT_MIN_FREE_PERCENT",
    "MIN_FREE_MB_ENV",
    "MIN_FREE_PERCENT_ENV",
    "DiskThresholds",
    "DiskUsage",
    "Readiness",
    "ReadinessProbe",
    "disk_thresholds",
]

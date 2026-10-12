"""Process metrics in the Prometheus text format (#1101).

``GET /metrics`` answers an administrator with what an operator alerts on: the build
queue's depth and the workers' load, how long builds wait and run, the responses by
status — 429s and 5xx among them — the free disk under the output directory and the
readiness checks.

Counters and histograms are kept here, for the process, as a Prometheus client keeps
its default registry: a request's status is known in the HTTP layer and a build's end
in ``build_log``, neither of which holds the service. Gauges are read when the page is
rendered. A process that restarts starts its counters at zero, which Prometheus'
``rate()`` and ``increase()`` already expect.

Every label value comes from a closed set — a status code, a ``code`` from
``request_log.KNOWN_CODES`` or ``other``, a build mode and outcome, a check name. No
path, run id, owner, dataset or error text is a label, so the page carries nothing a
log line may not.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from . import request_log

#: Seconds. A build runs from seconds to the six-hour limit (#1119).
BUILD_DURATION_BUCKETS: tuple[float, ...] = (1, 5, 15, 60, 300, 900, 1800, 3600, 7200, 21600)
#: Seconds a queued async build waited before it started.
QUEUE_WAIT_BUCKETS: tuple[float, ...] = (0.1, 1, 5, 15, 60, 300, 900, 3600)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

_PREFIX = "kpubdata_builder_"
_MODES = frozenset({"async", "sync"})
_OUTCOMES = frozenset({"succeeded", "failed", "cancelled"})


class _Histogram:
    def __init__(self, buckets: Sequence[float]) -> None:
        self.buckets = tuple(sorted(buckets))
        self.counts = [0] * len(self.buckets)
        self.count = 0
        self.total = 0.0

    def observe(self, value: float) -> None:
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[index] += 1
        self.count += 1
        self.total += value


class _Registry:
    """The counters and histograms of this process. Every method is thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._responses: dict[tuple[str, str], int] = {}
        self._builds: dict[tuple[str, str], _Histogram] = {}
        self._queue_wait = _Histogram(QUEUE_WAIT_BUCKETS)

    def observe_response(self, status: int, code: object = None) -> None:
        key = (str(int(status)), request_log.code_of(code) or "")
        with self._lock:
            self._responses[key] = self._responses.get(key, 0) + 1

    def observe_build(self, mode: str, status: str, seconds: float) -> None:
        key = (mode if mode in _MODES else "other", status if status in _OUTCOMES else "other")
        with self._lock:
            histogram = self._builds.get(key)
            if histogram is None:
                histogram = self._builds[key] = _Histogram(BUILD_DURATION_BUCKETS)
            histogram.observe(max(seconds, 0.0))

    def observe_queue_wait(self, seconds: float) -> None:
        with self._lock:
            self._queue_wait.observe(max(seconds, 0.0))

    def response_count(self, status: int, code: str = "") -> int:
        with self._lock:
            return self._responses.get((str(status), code), 0)

    def render_counters(self) -> list[str]:
        with self._lock:
            lines = [
                f"# HELP {_PREFIX}http_responses_total HTTP responses by status and stable code.",
                f"# TYPE {_PREFIX}http_responses_total counter",
            ]
            for (status, code), value in sorted(self._responses.items()):
                labels = _labels({"status": status, "code": code})
                lines.append(f"{_PREFIX}http_responses_total{labels} {value}")
            lines += [
                f"# HELP {_PREFIX}build_duration_seconds How long a build ran, by outcome.",
                f"# TYPE {_PREFIX}build_duration_seconds histogram",
            ]
            for (mode, status), histogram in sorted(self._builds.items()):
                lines += _histogram_lines(
                    f"{_PREFIX}build_duration_seconds", {"mode": mode, "status": status}, histogram
                )
            lines += [
                f"# HELP {_PREFIX}build_queue_wait_seconds How long an async build queued.",
                f"# TYPE {_PREFIX}build_queue_wait_seconds histogram",
                *_histogram_lines(f"{_PREFIX}build_queue_wait_seconds", {}, self._queue_wait),
            ]
            return lines


REGISTRY = _Registry()


def observe_response(status: int, code: object = None) -> None:
    """Count one HTTP response. Never raises: a metric must not fail a request."""
    with contextlib.suppress(Exception):
        REGISTRY.observe_response(status, code)


def observe_build(mode: str, status: str, seconds: float) -> None:
    """Record one build's running time. Never raises."""
    with contextlib.suppress(Exception):
        REGISTRY.observe_build(mode, status, seconds)


def observe_queue_wait(created_at: str) -> None:
    """Record how long a job queued since ``created_at`` (ISO 8601). Never raises."""
    try:
        created = datetime.fromisoformat(created_at)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        waited = (datetime.now(tz=timezone.utc) - created).total_seconds()
        REGISTRY.observe_queue_wait(waited)
    except Exception:  # noqa: BLE001, S110 - counting is best effort
        pass


@dataclass(frozen=True)
class Gauges:
    """What is read when the page is rendered. ``None`` is a value that could not be read
    and is left out of the page rather than written as zero."""

    queued: int
    running: int
    capacity: int
    max_queued: int
    ready: bool
    checks: Mapping[str, str]
    accepting_builds: bool
    disk_free_bytes: int | None
    disk_total_bytes: int | None


def render(gauges: Gauges) -> str:
    """The whole page: gauges first, then counters and histograms."""
    lines: list[str] = []

    def gauge(name: str, help_text: str, samples: Iterable[tuple[dict[str, str], float]]) -> None:
        lines.append(f"# HELP {_PREFIX}{name} {help_text}")
        lines.append(f"# TYPE {_PREFIX}{name} gauge")
        for labels, value in samples:
            lines.append(f"{_PREFIX}{name}{_labels(labels)} {_number(value)}")

    gauge("builds_queued", "Async builds waiting for a worker.", [({}, gauges.queued)])
    gauge("builds_running", "Async builds running or being cancelled.", [({}, gauges.running)])
    gauge("build_workers", "Async build workers.", [({}, gauges.capacity)])
    gauge("build_queue_limit", "How many async builds may wait.", [({}, gauges.max_queued)])
    gauge(
        "accepting_builds",
        "1 when POST /builds would queue a build now.",
        [({}, 1 if gauges.accepting_builds else 0)],
    )
    gauge("ready", "1 when every readiness check is ok.", [({}, 1 if gauges.ready else 0)])
    gauge(
        "readiness_check",
        "1 when the readiness check is ok.",
        [
            ({"check": name}, 1 if state == "ok" else 0)
            for name, state in sorted(gauges.checks.items())
        ],
    )
    if gauges.disk_free_bytes is not None and gauges.disk_total_bytes is not None:
        gauge(
            "disk_free_bytes",
            "Free bytes on the filesystem of the output directory.",
            [({}, gauges.disk_free_bytes)],
        )
        gauge(
            "disk_total_bytes",
            "Size in bytes of the filesystem of the output directory.",
            [({}, gauges.disk_total_bytes)],
        )
    lines += REGISTRY.render_counters()
    return "\n".join(lines) + "\n"


def _histogram_lines(name: str, labels: dict[str, str], histogram: _Histogram) -> list[str]:
    lines = [
        f"{name}_bucket{_labels({**labels, 'le': _number(bound)})} {count}"
        for bound, count in zip(histogram.buckets, histogram.counts, strict=True)
    ]
    lines.append(f"{name}_bucket{_labels({**labels, 'le': '+Inf'})} {histogram.count}")
    lines.append(f"{name}_sum{_labels(labels)} {_number(histogram.total)}")
    lines.append(f"{name}_count{_labels(labels)} {histogram.count}")
    return lines


def _labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    escaped = (
        f'{key}="{value.replace(chr(92), chr(92) * 2).replace(chr(34), chr(92) + chr(34))}"'
        for key, value in labels.items()
    )
    return "{" + ",".join(escaped) + "}"


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


__all__ = [
    "BUILD_DURATION_BUCKETS",
    "CONTENT_TYPE",
    "QUEUE_WAIT_BUCKETS",
    "REGISTRY",
    "Gauges",
    "observe_build",
    "observe_queue_wait",
    "observe_response",
    "render",
]

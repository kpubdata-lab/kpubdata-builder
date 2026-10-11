"""GET /readyz apart from GET /healthz, and the GET /metrics page (#1101).

Each readiness failure is made happen and read back: the build index that cannot be
read, the output directory that cannot be written, a disk below either threshold or
unreadable, a filesystem check that hangs, workers draining or stopped. A full queue is
shown not to be one. The metrics page is read through the HTTP layer, as a scraper
reads it.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import HTTPServer
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service import metrics as metrics_module
from kpubdata_builder.service import readiness as readiness_module
from kpubdata_builder.service.app import ServiceResponse, TextResponse
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.http import make_handler
from kpubdata_builder.service.jobs import AsyncBuildExecutor, AsyncBuildStats
from kpubdata_builder.service.readiness import DiskUsage, ReadinessProbe, disk_thresholds
from kpubdata_builder.service.routes import monitoring as monitoring_route
from kpubdata_builder.service.startup_settings import check_settings
from kpubdata_builder.store import BuildIndex, SqliteBuildIndex

from .test_service import VALID_SPEC_YAML, _FakeClient

_GIB = 1024**3


@pytest.fixture(autouse=True)
def _thresholds_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The machine running the tests has whatever disk it has; a test sets its own."""
    monkeypatch.setenv(readiness_module.MIN_FREE_PERCENT_ENV, "0")
    monkeypatch.setenv(readiness_module.MIN_FREE_MB_ENV, "0")


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client)


def _usage(free: int, total: int = 100 * _GIB) -> DiskUsage:
    return DiskUsage(free_bytes=free, total_bytes=total)


@dataclass
class _Executor:
    """Stands in for the executor where only its state matters."""

    queued: int = 0
    max_queue_size: int = 10
    draining: bool = False
    stopped: bool = False

    def stats(self) -> AsyncBuildStats:
        return AsyncBuildStats(queued=self.queued, running=0, capacity=2)


class _BrokenIndex:
    def latest_successful_finished_at(self) -> str | None:
        raise RuntimeError("database disk image is malformed: /data/secret/path")


def _probe(
    tmp_path: Path,
    *,
    build_index: object | None = None,
    executor: object | None = None,
    disk: DiskUsage | None = None,
    disk_usage: Callable[[Path], DiskUsage] | None = None,
    cache_seconds: float = readiness_module.DEFAULT_CACHE_SECONDS,
    timeout_seconds: float = readiness_module.DEFAULT_TIMEOUT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ReadinessProbe:
    def fixed(_: Path) -> DiskUsage:
        return disk or _usage(50 * _GIB)

    return ReadinessProbe(
        output_root=tmp_path,
        build_index=cast(BuildIndex, build_index or SqliteBuildIndex(tmp_path)),
        async_builds=cast(AsyncBuildExecutor, executor or _Executor()),
        disk_usage=disk_usage or fixed,
        cache_seconds=cache_seconds,
        timeout_seconds=timeout_seconds,
        clock=clock,
    )


# --------------------------------------------------------------------------- readiness


def test_healthz_and_readyz_answer_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
    service = _service(tmp_path)

    assert dispatch(service, "GET", "/healthz", None) == ServiceResponse(200, {"status": "ok"})
    response = dispatch(service, "GET", "/readyz", None)

    assert response == ServiceResponse(
        200,
        {
            "status": "ready",
            "checks": {"state_store": "ok", "output_dir": "ok", "disk": "ok", "workers": "ok"},
            "accepting_builds": True,
        },
    )


def test_an_unreadable_build_index_is_not_ready_and_the_body_names_no_detail(
    tmp_path: Path,
) -> None:
    readiness = _probe(tmp_path, build_index=_BrokenIndex()).check()

    assert not readiness.ready
    assert readiness.checks["state_store"] == "failed"
    text = json.dumps(readiness.body())
    assert "secret" not in text and str(tmp_path) not in text and "malformed" not in text


def test_an_output_directory_that_cannot_be_written_is_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: object, **__: object) -> object:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", refuse)

    readiness = _probe(tmp_path).check()

    assert readiness.checks["output_dir"] == "failed"
    assert readiness.body()["status"] == "not_ready"


def test_the_write_check_leaves_no_file_behind(tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    assert _probe(tmp_path).check().checks["output_dir"] == "ok"
    assert {p for p in tmp_path.iterdir() if p.name.startswith(".readyz-")} == set()
    assert set(tmp_path.iterdir()) >= before


def test_a_disk_below_the_percentage_is_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(readiness_module.MIN_FREE_PERCENT_ENV, "15")

    low = _probe(tmp_path, disk=_usage(10 * _GIB)).check()
    ok = _probe(tmp_path, disk=_usage(20 * _GIB)).check()

    assert low.checks["disk"] == "low" and not low.ready
    assert ok.checks["disk"] == "ok" and ok.ready


def test_a_disk_below_the_size_is_low_even_when_the_percentage_is_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(readiness_module.MIN_FREE_PERCENT_ENV, "15")
    monkeypatch.setenv(readiness_module.MIN_FREE_MB_ENV, "1024")

    readiness = _probe(tmp_path, disk=_usage(512 * 1024**2, total=2 * _GIB)).check()

    assert readiness.checks["disk"] == "low"


def test_zero_turns_each_disk_bound_off(tmp_path: Path) -> None:
    assert _probe(tmp_path, disk=_usage(1, total=100 * _GIB)).check().checks["disk"] == "ok"


def test_a_disk_that_cannot_be_read_is_failed(tmp_path: Path) -> None:
    def unreadable(_: Path) -> DiskUsage:
        raise OSError("stale file handle")

    assert _probe(tmp_path, disk_usage=unreadable).check().checks["disk"] == "failed"


def test_a_hung_filesystem_check_is_a_timeout_and_is_not_started_twice(tmp_path: Path) -> None:
    release = threading.Event()
    calls: list[int] = []

    def hang(_: Path) -> DiskUsage:
        calls.append(1)
        release.wait(10)
        return _usage(50 * _GIB)

    probe = _probe(tmp_path, disk_usage=hang, timeout_seconds=0.05, cache_seconds=0)
    try:
        first = probe.check()
        second = probe.check()
    finally:
        release.set()

    assert first.checks == {
        "state_store": "timeout",
        "output_dir": "timeout",
        "disk": "timeout",
        "workers": "ok",
    }
    assert second.checks["disk"] == "timeout"
    assert len(calls) == 1


def test_draining_and_stopped_workers_are_not_ready(tmp_path: Path) -> None:
    draining = _probe(tmp_path, executor=_Executor(draining=True)).check()
    stopped = _probe(tmp_path, executor=_Executor(stopped=True)).check()

    assert draining.checks["workers"] == "draining" and not draining.accepting_builds
    assert stopped.checks["workers"] == "stopped" and not stopped.ready


def test_the_executor_reports_draining_and_stopped(tmp_path: Path) -> None:
    executor = AsyncBuildExecutor(max_workers=1)
    assert (executor.draining, executor.stopped) == (False, False)
    executor.begin_shutdown("interrupted: test")
    assert (executor.draining, executor.stopped) == (True, True)

    other = AsyncBuildExecutor(max_workers=1)
    other.shutdown()
    assert (other.draining, other.stopped) == (False, True)


def test_a_full_queue_stops_accepting_builds_and_stays_ready(tmp_path: Path) -> None:
    readiness = _probe(tmp_path, executor=_Executor(queued=10, max_queue_size=10)).check()

    assert readiness.ready
    assert readiness.body()["status"] == "ready"
    assert readiness.accepting_builds is False


def test_an_answer_is_reused_until_it_ages_and_computed_once_for_a_burst(
    tmp_path: Path,
) -> None:
    now = [0.0]
    calls: list[int] = []
    gate = threading.Event()

    def counted(_: Path) -> DiskUsage:
        calls.append(1)
        gate.wait(5)
        return _usage(50 * _GIB)

    probe = _probe(tmp_path, disk_usage=counted, cache_seconds=5, clock=lambda: now[0])
    threads = [threading.Thread(target=probe.check) for _ in range(8)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(5)
    assert len(calls) == 1

    now[0] = 4.9
    probe.check()
    assert len(calls) == 1
    now[0] = 5.0
    probe.check()
    assert len(calls) == 2


def test_a_threshold_that_cannot_be_used_stops_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(readiness_module.MIN_FREE_PERCENT_ENV, "150")
    monkeypatch.setenv(readiness_module.MIN_FREE_MB_ENV, "lots")

    with pytest.raises(ValueError, match="READY_MIN_FREE_PERCENT"):
        disk_thresholds()
    problems = check_settings().problems
    assert any("KPUBDATA_BUILDER_READY_MIN_FREE_PERCENT" in p for p in problems)


def test_the_defaults_are_fifteen_percent_and_one_gib(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(readiness_module.MIN_FREE_PERCENT_ENV)
    monkeypatch.delenv(readiness_module.MIN_FREE_MB_ENV)

    thresholds = disk_thresholds()

    assert (thresholds.min_free_percent, thresholds.min_free_mb) == (15.0, 1024)


# ----------------------------------------------------------------------------- metrics


def test_metrics_is_refused_to_a_user_who_is_not_an_administrator(tmp_path: Path) -> None:
    alice = Principal(kind="oidc", identifier="alice", owner_id="oidc:alice")

    response = monitoring_route.route(_service(tmp_path), "GET", "/metrics", None, "", alice)

    assert isinstance(response, ServiceResponse) and response.status_code == 403


@pytest.fixture()
def served(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.delenv("KPUBDATA_BUILDER_DEV_MODE", raising=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_API_KEY", "secret")
    server = HTTPServer(("127.0.0.1", 0), make_handler(_service(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)


def _get(url: str, key: str | None = None) -> tuple[int, str, str]:
    request = urllib.request.Request(url, headers={"X-API-Key": key} if key else {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers["Content-Type"], response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers["Content-Type"], exc.read().decode()


def test_metrics_needs_credentials_and_answers_the_prometheus_text_format(served: str) -> None:
    assert _get(f"{served}/metrics")[0] == 401

    status, content_type, page = _get(f"{served}/metrics", "secret")

    assert status == 200
    assert content_type == "text/plain; version=0.0.4; charset=utf-8"
    for line in (
        "# TYPE kpubdata_builder_builds_queued gauge",
        "kpubdata_builder_builds_queued 0",
        "kpubdata_builder_builds_running 0",
        "kpubdata_builder_build_queue_limit 10",
        "kpubdata_builder_accepting_builds 1",
        "kpubdata_builder_ready 1",
        'kpubdata_builder_readiness_check{check="disk"} 1',
        "# TYPE kpubdata_builder_http_responses_total counter",
        "# TYPE kpubdata_builder_build_duration_seconds histogram",
        'kpubdata_builder_build_queue_wait_seconds_bucket{le="+Inf"}',
    ):
        assert line in page
    assert "kpubdata_builder_disk_free_bytes " in page
    assert "kpubdata_builder_disk_total_bytes " in page


def test_responses_are_counted_by_status_429s_and_5xx_included(served: str) -> None:
    registry = metrics_module.REGISTRY
    before_404 = registry.response_count(404)
    before_401 = registry.response_count(401, "unauthorized")

    _get(f"{served}/no-such-route", "secret")
    _get(f"{served}/version", "wrong")

    # Counted in the handler's ``finally``, after the answer is written: the client can
    # read the answer before the count lands.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (
        registry.response_count(404) < before_404 + 1
        or registry.response_count(401, "unauthorized") < before_401 + 1
    ):
        time.sleep(0.01)
    assert registry.response_count(404) == before_404 + 1
    assert registry.response_count(401, "unauthorized") == before_401 + 1

    before_429 = registry.response_count(429, "build_queue_full")
    before_500 = registry.response_count(500)
    metrics_module.observe_response(429, "build_queue_full")
    metrics_module.observe_response(500)
    assert registry.response_count(429, "build_queue_full") == before_429 + 1
    assert registry.response_count(500) == before_500 + 1
    page = _get(f"{served}/metrics", "secret")[2]
    assert 'kpubdata_builder_http_responses_total{status="429",code="build_queue_full"}' in page
    assert 'kpubdata_builder_http_responses_total{status="500",code=""}' in page


def test_a_code_outside_the_vocabulary_is_never_a_label() -> None:
    metrics_module.observe_response(400, "/data/run-1 owner=alice")

    page = metrics_module.render(
        metrics_module.Gauges(
            queued=0,
            running=0,
            capacity=1,
            max_queued=1,
            ready=True,
            checks={},
            accepting_builds=True,
            disk_free_bytes=None,
            disk_total_bytes=None,
        )
    )

    assert "alice" not in page and "/data" not in page
    assert 'kpubdata_builder_http_responses_total{status="400",code="other"}' in page
    # A disk that could not be read is left out, not written as zero.
    assert "disk_free_bytes" not in page


def test_a_build_is_timed_into_the_duration_histogram(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    service = _service(tmp_path)

    def count() -> int:
        page = service.metrics_page().text
        prefix = 'kpubdata_builder_build_duration_seconds_count{mode="sync",status="succeeded"} '
        lines = [line for line in page.splitlines() if line.startswith(prefix)]
        return int(lines[0].rsplit(" ", 1)[1]) if lines else 0

    before = count()
    response = dispatch(
        service, "POST", "/build", {"spec": VALID_SPEC_YAML, "run_id": "metrics-run"}
    )

    assert isinstance(response, ServiceResponse) and response.status_code == 200
    assert count() == before + 1
    assert "metrics-run" not in service.metrics_page().text


def test_an_async_build_records_how_long_it_queued() -> None:
    before = metrics_module.REGISTRY.render_counters()
    metrics_module.observe_queue_wait("2026-01-01T00:00:00+00:00")
    after = metrics_module.REGISTRY.render_counters()

    def total(lines: list[str]) -> int:
        line = next(
            x for x in lines if x.startswith("kpubdata_builder_build_queue_wait_seconds_count")
        )
        return int(line.rsplit(" ", 1)[1])

    assert total(after) == total(before) + 1


def test_metrics_page_is_a_text_response(tmp_path: Path) -> None:
    page = _service(tmp_path).metrics_page()
    assert isinstance(page, TextResponse) and page.text.endswith("\n")

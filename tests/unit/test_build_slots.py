"""How many builds and previews run at once is its own limit (#1028).

``KPUBDATA_BUILDER_MAX_WORKERS`` sized the request threads and the async build workers
together. With it at 2, two synchronous ``POST /build`` on the request threads ran on
top of two async jobs — four builds against a memory budget made for two — and every
other request shared two threads. The number of builds is now one limit both paths go
through, and previews can be given one too.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import http as http_module

_SPEC = """\
dataset_id: slots.{name}
title: Slots
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


class _Result:
    items = [{"id": "1"}]


class _Gauge:
    """Counts the fetches in flight and holds each one until the gate opens."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self._lock = threading.Lock()
        self._inside = 0
        self.peak = 0
        self.entered = 0

    def fetch(self) -> _Result:
        with self._lock:
            self._inside += 1
            self.entered += 1
            self.peak = max(self.peak, self._inside)
        try:
            self.gate.wait(timeout=20)
        finally:
            with self._lock:
                self._inside -= 1
        return _Result()

    def wait_for(self, entered: int, *, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while self.entered < entered and time.monotonic() < deadline:
            time.sleep(0.01)
        assert self.entered >= entered, f"only {self.entered} of {entered} fetches started"


def _factory(gauge: _Gauge) -> Callable[..., object]:
    class _Dataset:
        def list(self, **_params: object) -> _Result:
            return gauge.fetch()

    class _Client:
        def dataset(self, _key: str) -> _Dataset:
            return _Dataset()

    def create(**_kwargs: object) -> object:
        return _Client()

    return create


@pytest.fixture()
def gauge() -> Iterator[_Gauge]:
    made = _Gauge()
    yield made
    made.gate.set()


def _start(target: Callable[[], object]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _run_four_builds(service: BuilderService, gauge: _Gauge, *, expect_running: int) -> int:
    """Two synchronous builds on their own threads and two async jobs; the peak in flight."""
    statuses: list[int] = []

    def sync(name: str) -> None:
        response = dispatch(service, "POST", "/build", {"spec": _SPEC.format(name=name)})
        assert isinstance(response, ServiceResponse)
        statuses.append(response.status_code)

    threads = [_start(lambda name=name: sync(name)) for name in ("sync1", "sync2")]
    for name in ("async1", "async2"):
        submitted = dispatch(service, "POST", "/builds", {"spec": _SPEC.format(name=name)})
        assert isinstance(submitted, ServiceResponse)
        assert submitted.status_code == 202
    try:
        gauge.wait_for(expect_running)
        # Long enough for a build that is not held back to reach its fetch.
        time.sleep(0.5)
        return gauge.peak
    finally:
        gauge.gate.set()
        for thread in threads:
            thread.join(timeout=20)
        service._async_builds._executor.shutdown(wait=True)
        assert statuses == [200, 200]
        assert gauge.entered == 4


def test_sync_and_async_builds_share_one_limit(tmp_path: Path, gauge: _Gauge) -> None:
    service = BuilderService(
        output_root=tmp_path,
        client_factory=_factory(gauge),
        async_max_workers=2,
        max_concurrent_builds=2,
    )

    assert _run_four_builds(service, gauge, expect_running=2) == 2


def test_the_measurement_sees_four_when_four_are_allowed(tmp_path: Path, gauge: _Gauge) -> None:
    """The gate itself: the same four builds do run together when nothing holds them
    back — which is what two request threads plus two workers did before."""
    service = BuilderService(
        output_root=tmp_path,
        client_factory=_factory(gauge),
        async_max_workers=2,
        max_concurrent_builds=4,
    )

    assert _run_four_builds(service, gauge, expect_running=4) == 4


def test_unset_the_limit_is_the_async_worker_count(tmp_path: Path, gauge: _Gauge) -> None:
    service = BuilderService(
        output_root=tmp_path, client_factory=_factory(gauge), async_max_workers=3
    )

    assert service.max_concurrent_builds == 3


@pytest.mark.parametrize(("limit", "expected_peak"), [(1, 1), (None, 2)])
def test_previews_wait_for_a_slot_only_when_a_limit_is_set(
    tmp_path: Path, gauge: _Gauge, limit: int | None, expected_peak: int
) -> None:
    service = BuilderService(
        output_root=tmp_path, client_factory=_factory(gauge), max_concurrent_previews=limit
    )
    statuses: list[int] = []

    def preview(name: str) -> None:
        response = dispatch(service, "POST", "/preview", {"spec": _SPEC.format(name=name)})
        assert isinstance(response, ServiceResponse)
        statuses.append(response.status_code)

    threads = [_start(lambda name=name: preview(name)) for name in ("p1", "p2")]
    try:
        gauge.wait_for(expected_peak)
        time.sleep(0.5)
        peak = gauge.peak
    finally:
        gauge.gate.set()
        for thread in threads:
            thread.join(timeout=20)

    assert peak == expected_peak
    assert statuses == [200, 200]


@pytest.mark.parametrize("bad", [{"max_concurrent_builds": 0}, {"max_concurrent_previews": 0}])
def test_a_limit_below_one_is_refused(tmp_path: Path, gauge: _Gauge, bad: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        BuilderService(output_root=tmp_path, client_factory=_factory(gauge), **bad)


# --- serve: the settings ---


def _serve(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *args: str) -> dict[str, int | None]:
    seen: dict[str, int | None] = {}

    def fake_serve(service: object, *, host: str, port: int, max_workers: int) -> None:
        assert isinstance(service, BuilderService)
        seen["request_threads"] = max_workers
        seen["builds"] = service.max_concurrent_builds
        seen["async_workers"] = service._async_builds.stats().capacity
        seen["previews"] = service.max_concurrent_previews

    monkeypatch.setattr(http_module, "serve", fake_serve)
    assert main(["serve", "--output-dir", str(tmp_path), *args]) == 0
    return seen


@pytest.fixture()
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MAX_WORKERS", "MAX_BUILDS", "MAX_PREVIEWS"):
        monkeypatch.delenv(f"KPUBDATA_BUILDER_{name}", raising=False)


def test_limiting_builds_does_not_shrink_the_request_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_BUILDS", "2")

    assert _serve(monkeypatch, tmp_path) == {
        "request_threads": 10,
        "builds": 2,
        "async_workers": 2,
        "previews": None,
    }


def test_a_deployment_that_only_set_max_workers_keeps_its_build_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_WORKERS", "2")

    seen = _serve(monkeypatch, tmp_path)

    # Two async workers as before — and now two builds in all, not two per path.
    assert (seen["request_threads"], seen["builds"], seen["async_workers"]) == (2, 2, 2)


def test_flags_win_over_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_BUILDS", "2")
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_PREVIEWS", "1")

    seen = _serve(monkeypatch, tmp_path, "--max-builds", "3", "--max-previews", "4")

    assert (seen["builds"], seen["async_workers"], seen["previews"]) == (3, 3, 4)


@pytest.mark.parametrize("flag", ["--max-builds", "--max-previews"])
def test_serve_refuses_a_limit_below_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clean_env: None, flag: str
) -> None:
    monkeypatch.setattr(http_module, "serve", lambda *_args, **_kwargs: None)

    with pytest.raises(SystemExit, match="must be >= 1"):
        main(["serve", "--output-dir", str(tmp_path), flag, "0"])

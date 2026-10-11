"""A per-user query rate, a per-provider egress limit, and a query that stops when its
client leaves (#1245).

The rate and the egress limit share one sliding window (``rate_limit``): no 60 seconds
hold more admitted events than the limit, per key. The query rate is a user's; the egress
limit is a provider's, for every user and build together.
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder import egress
from kpubdata_builder.cli import main
from kpubdata_builder.pipeline import preview_build
from kpubdata_builder.query import cancellation
from kpubdata_builder.query.engine import QueryCancelledError, QueryEngine
from kpubdata_builder.rate_limit import RateLimited, WindowLimiter
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch, provider_probe
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service import http as http_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.http import BoundedThreadingHTTPServer, make_handler
from kpubdata_builder.service.query_rate import (
    QUERY_RATE_PER_MINUTE_ENV,
    QUERY_ROUTES,
    QueryRateLimiter,
    query_rate_per_minute,
)
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.stages.bronze import build_bronze_artifact

from .conftest import spawn_timeout_multiplier
from .test_query_engine import _pid_is_alive

ME = Principal(kind="service", identifier="me", owner_id="owner:me")
OTHER = Principal(kind="service", identifier="other", owner_id="owner:other")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Sleeps:
    """A ``sleep`` that moves the clock instead of waiting."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)


def _window(limit: int, clock: _Clock, sleeps: _Sleeps | None = None) -> WindowLimiter:
    return WindowLimiter(lambda: limit, clock=clock, sleep=sleeps or _Sleeps(clock))


def _refused(limiter: WindowLimiter, key: str) -> int | None:
    try:
        limiter.admit(key)
    except RateLimited as limited:
        return limited.retry_after_seconds
    return None


# ------------------------------------------------------------------ the window itself


def test_the_window_admits_the_limit_and_then_says_how_long_to_wait() -> None:
    clock = _Clock()
    limiter = _window(3, clock)

    for _ in range(3):
        assert _refused(limiter, "a") is None
        clock.now += 10
    # Events at 1000, 1010 and 1020; the first leaves the window at 1060.
    assert _refused(limiter, "a") == 30
    clock.now = 1059.5
    assert _refused(limiter, "a") == 1
    clock.now = 1060.0
    assert _refused(limiter, "a") is None


def test_a_refused_event_is_not_counted() -> None:
    clock = _Clock()
    limiter = _window(1, clock)

    assert _refused(limiter, "a") is None
    for _ in range(5):
        clock.now += 1
        assert _refused(limiter, "a") is not None
    clock.now = 1060.0
    assert _refused(limiter, "a") is None


def test_each_key_has_its_own_window() -> None:
    clock = _Clock()
    limiter = _window(1, clock)

    assert _refused(limiter, "a") is None
    assert _refused(limiter, "a") is not None
    assert _refused(limiter, "b") is None


@pytest.mark.parametrize("limit", [0, -1])
def test_no_limit_admits_everything_and_keeps_nothing(limit: int) -> None:
    clock = _Clock()
    limiter = _window(limit, clock)

    for _ in range(1000):
        assert _refused(limiter, "a") is None
    assert limiter._windows == {}


def test_a_waiting_caller_reserves_the_next_slot_in_order() -> None:
    clock = _Clock()
    sleeps = _Sleeps(clock)
    limiter = _window(2, clock, sleeps)

    waited = [limiter.wait("datago") for _ in range(5)]

    # Two at once, then each when the event two places back leaves the window.
    assert waited == [0.0, 0.0, 60.0, 60.0, 120.0]
    assert sleeps.slept == [60.0, 60.0, 120.0]
    # A caller that asks to be admitted now sees the reserved slots as taken.
    assert _refused(limiter, "datago") == 120


def test_concurrent_callers_never_exceed_the_limit() -> None:
    limiter = WindowLimiter(lambda: 10, sleep=lambda _seconds: None)
    admitted: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(50)

    def call(index: int) -> None:
        start.wait()
        if _refused(limiter, "shared") is None:
            with lock:
                admitted.append(index)

    threads = [threading.Thread(target=call, args=(index,)) for index in range(50)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(admitted) == 10


def test_concurrent_waiters_get_distinct_slots_within_the_limit() -> None:
    clock = _Clock()
    delays: list[float] = []
    lock = threading.Lock()
    limiter = WindowLimiter(lambda: 3, clock=clock, sleep=lambda _seconds: None)
    start = threading.Barrier(9)

    def call() -> None:
        start.wait()
        waited = limiter.wait("datago")
        with lock:
            delays.append(waited)

    threads = [threading.Thread(target=call) for _ in range(9)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # Three per minute: three now, three a minute later, three two minutes later.
    assert sorted(delays) == [0.0] * 3 + [60.0] * 3 + [120.0] * 3


# ------------------------------------------------------------- the per-user query rate


def test_the_query_rate_is_off_by_default_and_in_a_single_user_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(QUERY_RATE_PER_MINUTE_ENV, raising=False)
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    assert query_rate_per_minute() == 0

    monkeypatch.setenv(QUERY_RATE_PER_MINUTE_ENV, "5")
    assert query_rate_per_minute() == 5

    monkeypatch.delenv("ENFORCE_OWNERSHIP")
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    assert query_rate_per_minute() == 0


def _no_client(**_kwargs: object) -> Any:
    raise AssertionError("no provider is called here")


@pytest.fixture
def multi_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BuilderService:
    """A multi-user service whose callers are named by their ``X-API-Key``."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    monkeypatch.setenv(QUERY_RATE_PER_MINUTE_ENV, "2")
    people = {"me": ME, "other": OTHER}

    def authenticate(*, api_key: str | None = None, **_kwargs: object) -> Principal:
        return people[api_key or "me"]

    monkeypatch.setattr(app_module, "authenticate", authenticate)
    return BuilderService(output_root=tmp_path, client_factory=_no_client)


def _ask(service: BuilderService, method: str, path: str, who: str = "me") -> ServiceResponse:
    response = dispatch(service, method, path, {}, api_key=who)
    assert isinstance(response, ServiceResponse)
    return response


@pytest.mark.parametrize(("method", "path"), sorted(QUERY_ROUTES))
def test_each_query_route_is_counted_and_refused_with_a_stable_code(
    multi_user: BuilderService, method: str, path: str
) -> None:
    first = _ask(multi_user, method, path)
    second = _ask(multi_user, method, path)
    third = _ask(multi_user, method, path)

    assert first.status_code != 429 and second.status_code != 429
    assert third.status_code == 429
    assert third.body["code"] == "query_rate_limited"
    retry_after = third.body["retry_after_seconds"]
    assert isinstance(retry_after, int) and 1 <= retry_after <= 60


def test_all_query_routes_share_one_users_window(multi_user: BuilderService) -> None:
    assert _ask(multi_user, "POST", "/query").status_code != 429
    assert _ask(multi_user, "POST", "/warehouse/rows").status_code != 429

    assert _ask(multi_user, "POST", "/warehouse/aggregate").status_code == 429


def test_another_user_is_not_affected(multi_user: BuilderService) -> None:
    for _ in range(2):
        _ask(multi_user, "POST", "/query")
    assert _ask(multi_user, "POST", "/query").status_code == 429

    assert _ask(multi_user, "POST", "/query", who="other").status_code != 429


def test_a_request_that_is_not_a_query_is_not_counted(multi_user: BuilderService) -> None:
    for _ in range(5):
        assert _ask(multi_user, "GET", "/builds").status_code != 429
        assert _ask(multi_user, "GET", "/warehouse/tables").status_code != 429

    assert _ask(multi_user, "POST", "/query").status_code != 429


def test_a_refused_query_runs_nothing(
    multi_user: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[object] = []
    monkeypatch.setattr(BuilderService, "query", lambda self, body, *, principal: ran.append(body))
    for _ in range(3):
        _ask(multi_user, "POST", "/query")

    assert len(ran) == 2


def test_the_limiter_holds_owner_ids_only() -> None:
    clock = _Clock()
    limiter = QueryRateLimiter(_window(1, clock))

    assert limiter.refusal("POST", "/query", "owner:me") is None
    refusal = limiter.refusal("POST", "/query", "owner:me")

    assert refusal is not None and refusal.status_code == 429
    assert set(limiter._limiter._windows) == {"owner:me"}


# --------------------------------------------------------------------- Retry-After


@contextmanager
def _serving(service: BuilderService) -> Iterator[str]:
    server = BoundedThreadingHTTPServer(("127.0.0.1", 0), make_handler(service), max_workers=4)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _post(base: str, path: str) -> tuple[int, Mapping[str, str], dict[str, Any]]:
    request = urllib.request.Request(
        base + path, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), json.loads(error.read())


def test_a_query_past_the_rate_carries_retry_after_over_http(
    multi_user: BuilderService,
) -> None:
    with _serving(multi_user) as base:
        statuses = [_post(base, "/query")[0] for _ in range(2)]
        status, headers, body = _post(base, "/query")

    assert 429 not in statuses
    assert status == 429
    assert body["code"] == "query_rate_limited"
    assert headers["Retry-After"] == str(body["retry_after_seconds"])


def test_a_429_without_a_wait_has_no_retry_after(
    multi_user: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy(self: BuilderService, body: object, *, principal: Principal) -> ServiceResponse:
        return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})

    monkeypatch.setattr(BuilderService, "query", busy)
    with _serving(multi_user) as base:
        status, headers, _body = _post(base, "/query")

    assert status == 429
    assert "Retry-After" not in headers


# ------------------------------------------------------------ the per-provider egress


class _Page:
    def __init__(self, items: list[dict[str, JsonValue]], next_page: int | None) -> None:
        self.items = items
        self.next_page = next_page
        self.next_cursor = None
        self.total_count = None


class _PagedDataset:
    """Three pages of one row, requested lazily; each request is recorded."""

    def __init__(self, provider: str, requests: list[str]) -> None:
        self._provider = provider
        self._requests = requests

    def list(self, **_params: object) -> _Page:
        self._requests.append(self._provider)
        return _Page([{"id": "1"}], None)

    def list_all(self, **_params: object) -> Iterator[_Page]:
        for page in range(1, 4):
            self._requests.append(self._provider)
            yield _Page([{"id": str(page)}], page + 1 if page < 3 else None)


class _SingleDataset:
    def __init__(self, provider: str, requests: list[str]) -> None:
        self._provider = provider
        self._requests = requests

    def list(self, **_params: object) -> _Page:
        self._requests.append(self._provider)
        return _Page([{"id": "1"}], None)


class _Client:
    def __init__(self, *, paged: bool = True) -> None:
        self.requests: list[str] = []
        self._paged = paged

    def dataset(self, source_key: str) -> _PagedDataset | _SingleDataset:
        provider = source_key.partition(".")[0]
        if self._paged:
            return _PagedDataset(provider, self.requests)
        return _SingleDataset(provider, self.requests)

    def close(self) -> None:
        pass


@pytest.fixture
def egress_window(monkeypatch: pytest.MonkeyPatch) -> tuple[_Clock, _Sleeps]:
    """The process's egress window, on a clock the test moves, at three calls a minute."""
    clock = _Clock()
    sleeps = _Sleeps(clock)
    monkeypatch.setenv(egress.PROVIDER_CALLS_PER_MINUTE_ENV, "3")
    monkeypatch.setattr(
        egress,
        "LIMITER",
        WindowLimiter(egress.provider_calls_per_minute, clock=clock, sleep=sleeps),
    )
    return clock, sleeps


def test_the_egress_limit_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(egress.PROVIDER_CALLS_PER_MINUTE_ENV, raising=False)

    assert egress.provider_calls_per_minute() == 0


def test_a_build_waits_for_its_turn_page_by_page(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    _clock, sleeps = egress_window
    client = _Client()

    first = build_bronze_artifact(client, source_key="datago.a", staging_dir=tmp_path / "1")
    second = build_bronze_artifact(client, source_key="datago.b", staging_dir=tmp_path / "2")

    # Three pages each: the first build fills the minute, the second waits for it.
    assert (first.record_count, second.record_count) == (3, 3)
    assert client.requests == ["datago"] * 6
    assert sleeps.slept == [60.0, 60.0, 60.0]


def test_a_page_that_is_never_asked_for_is_not_counted(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    _clock, sleeps = egress_window
    client = _Client(paged=False)

    for index in range(3):
        build_bronze_artifact(client, source_key="datago.a", staging_dir=tmp_path / str(index))

    assert client.requests == ["datago"] * 3
    assert sleeps.slept == []


def test_providers_have_separate_windows(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    _clock, sleeps = egress_window
    client = _Client()

    build_bronze_artifact(client, source_key="datago.a", staging_dir=tmp_path / "1")
    build_bronze_artifact(client, source_key="kosis.a", staging_dir=tmp_path / "2")

    assert sleeps.slept == []


def _spec(*keys: str) -> BuildSpec:
    return BuildSpec(
        dataset_id="egress",
        title="Egress",
        description="egress limit",
        sources=tuple(
            SourceRef(provider=key.partition(".")[0], dataset=key.partition(".")[2]) for key in keys
        ),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def test_a_preview_is_refused_rather_than_kept_waiting(
    egress_window: tuple[_Clock, _Sleeps],
) -> None:
    clock, sleeps = egress_window
    client = _Client()
    # A preview of one row asks for one page: three of them fill the minute.
    for name in ("a", "b", "c"):
        preview_build(_spec(f"datago.{name}"), client=client, limit=1)

    with pytest.raises(RateLimited) as refused:
        preview_build(_spec("datago.d"), client=client, limit=1)

    assert refused.value.retry_after_seconds == 60
    assert sleeps.slept == []
    # The refused preview asked the provider for nothing.
    assert client.requests == ["datago"] * 3
    clock.now += 60
    preview_build(_spec("datago.d"), client=client, limit=1)
    assert client.requests == ["datago"] * 4


def test_a_preview_answers_429_with_a_stable_code(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    client = _Client()
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: client, terms_lookup=lambda _id: None
    )
    spec = (
        "dataset_id: egress\ntitle: Egress\ndescription: egress\n"
        "sources:\n  - provider: datago\n    dataset: air_quality\n"
        "exports:\n  - kind: jsonl\n    output_path: out/data.jsonl\n"
    )
    answers = [service.preview(spec, limit=1, principal=who) for who in (ME, OTHER, ME, OTHER)]

    assert [answer.status_code for answer in answers] == [200, 200, 200, 429]
    refused = answers[-1].body
    assert refused["code"] == "provider_egress_limited"
    assert refused["retry_after_seconds"] == 60


def test_builds_and_previews_of_different_users_share_a_providers_window(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    _clock, sleeps = egress_window
    client = _Client()

    # One user's build takes the minute's three calls ...
    build_bronze_artifact(client, source_key="datago.a", staging_dir=tmp_path / "1")
    # ... and another user's preview of the same provider is refused.
    with pytest.raises(RateLimited):
        preview_build(_spec("datago.b"), client=client, limit=1)
    # Another provider is open.
    preview_build(_spec("kosis.b"), client=client, limit=1)
    assert sleeps.slept == []


class _Outcome:
    service_id = "svc"
    status = "available"
    detail = ""
    http_status = 200


def test_a_probe_past_the_limit_lists_what_it_did_not_reach(
    egress_window: tuple[_Clock, _Sleeps],
) -> None:
    probed: list[str] = []

    @contextmanager
    def open_probe(_provider: str, _key: str) -> Iterator[Any]:
        def probe_one(dataset_id: str) -> _Outcome:
            probed.append(dataset_id)
            return _Outcome()

        yield probe_one

    datasets = [f"datago.d{index}" for index in range(5)]
    body = provider_probe.run_probe("datago", "key", datasets, open_probe=open_probe)

    assert probed == datasets[:3]
    assert body["complete"] is False
    assert body["not_probed"] == ["d3", "d4"]


# ------------------------------------------------------- a client that leaves mid-query


def _publish_pid(table_path: str) -> None:
    Path(table_path).write_text(str(os.getpid()), encoding="utf-8")


def _slow_worker(
    connection: Connection, table_path: str, sql: str, limit: int, started: int
) -> None:
    del connection, sql, limit, started
    _publish_pid(table_path)
    time.sleep(60)


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 20 * spawn_timeout_multiplier()
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert path.exists(), "the query child never started"


def test_a_query_stops_when_its_watch_says_the_requester_has_gone(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    engine = QueryEngine(timeout_seconds=60, worker=_slow_worker)
    started = time.monotonic()

    with cancellation.watching(pid_file.exists), pytest.raises(QueryCancelledError):
        engine.execute(pid_file, "SELECT 1", limit=1)

    assert time.monotonic() - started < 30 * spawn_timeout_multiplier()
    assert not _pid_is_alive(int(pid_file.read_text(encoding="utf-8")))


def test_a_query_outside_a_request_is_not_watched() -> None:
    assert cancellation.current() is None
    with cancellation.watching(lambda: False) as watch:
        assert cancellation.current() is watch
    assert cancellation.current() is None


def test_a_client_that_disconnects_cancels_its_in_flight_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the HTTP handler's watch sees the closed socket, the engine stops the
    child, and nothing is written back."""
    pid_file = tmp_path / "pid"
    outcome: list[BaseException | None] = []
    finished = threading.Event()

    def run_query(*_args: object, **_kwargs: object) -> ServiceResponse:
        try:
            QueryEngine(timeout_seconds=60, worker=_slow_worker).execute(
                pid_file, "SELECT 1", limit=1
            )
            outcome.append(None)
        except BaseException as error:
            outcome.append(error)
        finally:
            finished.set()
        return ServiceResponse(400, {"error": "query execution failed"})

    monkeypatch.setattr(http_module, "dispatch", run_query)
    service = BuilderService(output_root=tmp_path, client_factory=_no_client)
    started = time.monotonic()
    with _serving(service) as base:
        port = int(base.rsplit(":", 1)[1])
        client = socket.create_connection(("127.0.0.1", port), timeout=10)
        client.sendall(
            b"POST /query HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n{}"
        )
        _wait_for(pid_file)
        client.close()
        assert finished.wait(30 * spawn_timeout_multiplier())

    assert len(outcome) == 1 and isinstance(outcome[0], QueryCancelledError)
    assert time.monotonic() - started < 40 * spawn_timeout_multiplier()
    assert not _pid_is_alive(int(pid_file.read_text(encoding="utf-8")))


# ------------------------------------------------------------- the build count default


def _serve_builds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> int:
    seen: dict[str, int] = {}

    def fake_serve(service: BuilderService, **_kwargs: object) -> None:
        seen["builds"] = service._async_builds.stats().capacity

    monkeypatch.setattr(http_module, "serve", fake_serve)
    assert main(["serve", "--output-dir", str(tmp_path)]) == 0
    return seen["builds"]


@pytest.mark.parametrize(("workers", "builds"), [(None, 2), ("10", 2), ("1", 1), ("2", 2)])
def test_two_builds_run_at_once_unless_set_or_fewer_request_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, workers: str | None, builds: int
) -> None:
    for name in ("MAX_WORKERS", "MAX_BUILDS", "MAX_PREVIEWS"):
        monkeypatch.delenv(f"KPUBDATA_BUILDER_{name}", raising=False)
    if workers is not None:
        monkeypatch.setenv("KPUBDATA_BUILDER_MAX_WORKERS", workers)

    assert _serve_builds(monkeypatch, tmp_path) == builds


# ------------------------------------------------------------- the load measurement


def test_the_load_measurement_runs_and_says_it_is_local_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = Path(__file__).parents[2] / "scripts" / "measure_query_load.py"
    spec = importlib.util.spec_from_file_location("measure_query_load", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.main(["--rows", "1000", "--clients", "1", "--seconds", "0.5"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["scope"].startswith("local only")
    assert report["machine"]["cpu_count"]
    assert report["runs"][0]["failed"] == 0
    assert report["runs"][0]["completed"] >= 1

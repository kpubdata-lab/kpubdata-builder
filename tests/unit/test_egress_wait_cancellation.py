"""A wait for a provider's egress slot ends when its build is cancelled (#1328).

A build waits for its turn under the per-provider egress limit (#1245). The wait
reserves a slot, so it must give the slot back on every way out — cancellation, the
time limit and shutdown (which ask through the same cancellation), or an exception —
or the slot stays counted and every later call waits behind a call that never happens.

Every test runs on a clock the test moves; nothing sleeps for real.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path

import pytest

from kpubdata_builder import egress
from kpubdata_builder.rate_limit import WINDOW_SECONDS, RateLimited, WaitCancelled, WindowLimiter
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze import build_bronze_artifact


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self.lock:
            self.now += seconds


class _Sleeps:
    """A ``sleep`` that moves the clock instead of waiting, and can act on each call."""

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.slept: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock.advance(seconds)


def _refused(limiter: WindowLimiter, key: str) -> int | None:
    try:
        limiter.admit(key)
    except RateLimited as limited:
        return limited.retry_after_seconds
    return None


def _never_more_than(limit: int, times: list[float]) -> bool:
    """Whether no 60 seconds hold more than ``limit`` of ``times``."""
    ordered = sorted(times)
    return all(
        ordered[index] - ordered[index - limit] >= WINDOW_SECONDS
        for index in range(limit, len(ordered))
    )


# ------------------------------------------------------------------ the window itself


def test_a_cancelled_wait_ends_at_once_and_gives_its_slot_back() -> None:
    clock = _Clock()
    sleeps = _Sleeps(clock)
    limiter = WindowLimiter(lambda: 1, clock=clock, sleep=sleeps)
    limiter.admit("datago")  # the minute is full until 1060

    # Cancelled five seconds into a wait of sixty.
    with pytest.raises(WaitCancelled):
        limiter.wait("datago", cancelled=lambda: clock() >= 1005.0)

    assert clock() < 1006.0
    assert max(sleeps.slept) <= 1.0
    # The next caller takes the slot the cancelled one gave back, not the one after it.
    assert _refused(limiter, "datago") == 55
    assert limiter.wait("datago") == 55.0


def test_a_wait_cancelled_before_it_starts_counts_nothing() -> None:
    clock = _Clock()
    limiter = WindowLimiter(lambda: 1, clock=clock, sleep=_Sleeps(clock))

    with pytest.raises(WaitCancelled):
        limiter.wait("datago", cancelled=lambda: True)

    assert _refused(limiter, "datago") is None


def test_a_wait_that_raises_gives_its_slot_back() -> None:
    clock = _Clock()

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    limiter = WindowLimiter(lambda: 1, clock=clock, sleep=interrupted)
    limiter.admit("datago")

    with pytest.raises(KeyboardInterrupt):
        limiter.wait("datago")

    # Only the admitted call is counted: the slot at 1060 is free again.
    assert _refused(limiter, "datago") == 60
    clock.advance(60)
    assert _refused(limiter, "datago") is None


def test_a_waiter_behind_a_cancelled_one_moves_up_in_order() -> None:
    clock = _Clock()
    first_reserved = threading.Event()
    first_may_go_on = threading.Event()
    first_cancelled = threading.Event()
    first_outcome: list[BaseException] = []

    class _Thread(_Sleeps):
        def __call__(self, seconds: float) -> None:
            if threading.current_thread().name == "first":
                first_reserved.set()
                assert first_may_go_on.wait(10)
                return
            if not first_cancelled.is_set():
                # The first waiter is cancelled while the second waits behind it.
                first_cancelled.set()
                first_may_go_on.set()
                first.join(10)
            super().__call__(seconds)

    sleeps = _Thread(clock)
    limiter = WindowLimiter(lambda: 1, clock=clock, sleep=sleeps)
    limiter.admit("datago")  # called at 1000; next slot 1060

    def wait_first() -> None:
        try:
            limiter.wait("datago", cancelled=first_cancelled.is_set)
        except BaseException as exc:  # noqa: BLE001 - recorded for the assertion below
            first_outcome.append(exc)

    first = threading.Thread(target=wait_first, name="first")
    first.start()
    assert first_reserved.wait(10)  # holds 1060

    waited = limiter.wait("datago", cancelled=lambda: False)  # reserved 1120

    assert len(first_outcome) == 1 and isinstance(first_outcome[0], WaitCancelled)
    # It takes the cancelled slot rather than its own a minute later.
    assert waited == 60.0
    assert clock() == 1060.0
    # Whoever comes next queues behind it, and the minute still holds one call.
    assert _refused(limiter, "datago") == 60
    assert _never_more_than(1, [1000.0, 1060.0])


def test_cancellable_waiters_never_exceed_the_limit() -> None:
    clock = _Clock()
    sleeps = _Sleeps(clock)
    limiter = WindowLimiter(lambda: 2, clock=clock, sleep=sleeps)
    calls: list[float] = []

    for index in range(7):
        # Every third waiter is cancelled part way through its wait.
        cancel_at = clock() + 10 if index % 3 == 2 else None
        try:
            limiter.wait(
                "datago",
                cancelled=lambda at=cancel_at: at is not None and clock() >= at,
            )
        except WaitCancelled:
            continue
        calls.append(clock())

    assert len(calls) == 5
    assert _never_more_than(2, calls)


# ------------------------------------------------------------------- a build's fetch


class _Page:
    def __init__(self, items: list[dict[str, JsonValue]], next_page: int | None) -> None:
        self.items = items
        self.next_page = next_page
        self.next_cursor = None
        self.total_count = None


class _PagedDataset:
    def __init__(self, requests: list[str]) -> None:
        self._requests = requests

    def list(self, **_params: object) -> _Page:
        self._requests.append("list")
        return _Page([{"id": "1"}], None)

    def list_all(self, **_params: object) -> Iterable[_Page]:
        for page in range(1, 4):
            self._requests.append(f"page{page}")
            yield _Page([{"id": str(page)}], page + 1 if page < 3 else None)


class _Client:
    def __init__(self) -> None:
        self.requests: list[str] = []

    def dataset(self, _source_key: str) -> _PagedDataset:
        return _PagedDataset(self.requests)

    def close(self) -> None:
        pass


@pytest.fixture
def egress_window(monkeypatch: pytest.MonkeyPatch) -> tuple[_Clock, _Sleeps]:
    clock = _Clock()
    sleeps = _Sleeps(clock)
    monkeypatch.setenv(egress.PROVIDER_CALLS_PER_MINUTE_ENV, "1")
    monkeypatch.setattr(
        egress,
        "LIMITER",
        WindowLimiter(egress.provider_calls_per_minute, clock=clock, sleep=sleeps),
    )
    return clock, sleeps


def test_a_fetch_cancelled_while_it_waits_requests_nothing_more(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    clock, _sleeps = egress_window
    client = _Client()

    with pytest.raises(WaitCancelled):
        build_bronze_artifact(
            client,
            source_key="datago.a",
            staging_dir=tmp_path / "s",
            cancelled=lambda: clock() >= 1030.0,
        )

    # The first page went at once; the second was waited for and never requested.
    assert client.requests == ["page1"]
    assert clock() < 1031.0
    assert _refused(egress.LIMITER, "datago") == 30


def test_a_build_cancelled_while_it_waits_for_the_provider_stops_and_frees_the_slot(
    egress_window: tuple[_Clock, _Sleeps], tmp_path: Path
) -> None:
    clock, _sleeps = egress_window
    egress.LIMITER.admit("datago")  # another build took this minute's call

    class _CancelledAfter:
        def cancel_requested(self) -> bool:
            # Asked for ten seconds into the wait, as a user's cancel, the time limit
            # and shutdown all do.
            return clock() >= 1010.0

        def commit(self) -> bool:
            return not self.cancel_requested()

    client = _Client()
    service = BuilderService(
        output_root=tmp_path, client_factory=lambda **_: client, async_max_workers=1
    )
    spec = (
        "dataset_id: dataset.cancel\ntitle: Cancel\ndescription: wait\n"
        "sources:\n  - provider: datago\n    dataset: a\n"
        "exports:\n  - kind: jsonl\n    output_path: out/data.jsonl\n"
    )
    response = service.build(spec, run_id="run1", cancellation=_CancelledAfter())

    assert response.status_code == 409
    manifest = json.loads((tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "cancelled"
    assert client.requests == []
    assert clock() < 1011.0
    # Only the other build's call is counted; the cancelled build's slot is free again.
    assert _refused(egress.LIMITER, "datago") == 50

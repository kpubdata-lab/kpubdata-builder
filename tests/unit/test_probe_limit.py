"""One user cannot make the server probe a provider without bound (#1059).

A refused probe must call nothing: the point is what leaves the server's address.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

from kpubdata_builder import cli
from kpubdata_builder.service import (
    BuilderService,
    ServiceResponse,
    provider_probe,
    request_credentials,
)
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.probe_limit import (
    DEFAULT_PROBE_INTERVAL_SECONDS,
    PROBE_INTERVAL_ENV,
    ProbeLimiter,
    ProbeRefused,
    probe_interval_seconds,
)
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)
_KEY = "canary-value-for-probe-limit-test"


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _limiter(clock: _Clock, interval: float = 30.0) -> ProbeLimiter:
    return ProbeLimiter(interval_seconds=lambda: interval, clock=clock)


def _refused(limiter: ProbeLimiter, owner: str, provider: str) -> int | None:
    """The wait a probe is refused with, or None when it is let through (and ends)."""
    try:
        with limiter.probing(owner, provider):
            return None
    except ProbeRefused as refused:
        return refused.retry_after_seconds


def test_the_same_provider_is_refused_until_the_interval_has_passed() -> None:
    clock = _Clock()
    limiter = _limiter(clock)

    assert _refused(limiter, "a", "datago") is None
    clock.now += 6.5
    assert _refused(limiter, "a", "datago") == 24  # 23.5 s left, rounded up
    clock.now += 23.4
    assert _refused(limiter, "a", "datago") == 1
    clock.now += 0.1
    assert _refused(limiter, "a", "datago") is None


def test_the_interval_runs_from_when_the_probe_ended() -> None:
    clock = _Clock()
    limiter = _limiter(clock)

    with limiter.probing("a", "datago"):
        clock.now += 40  # a slow probe, longer than the interval
    clock.now += 5

    assert _refused(limiter, "a", "datago") == 25


def test_a_second_probe_while_one_runs_is_refused_whatever_the_provider() -> None:
    clock = _Clock()
    limiter = _limiter(clock)

    with limiter.probing("a", "datago"):
        assert _refused(limiter, "a", "localdata") == 30
        assert _refused(limiter, "a", "datago") == 30
    # The refused attempts did not take the user's turn or start an interval.
    assert _refused(limiter, "a", "localdata") is None


def test_another_user_and_another_provider_are_not_affected() -> None:
    """Negative: the bound is per user, and the interval per provider."""
    clock = _Clock()
    limiter = _limiter(clock)

    assert _refused(limiter, "a", "datago") is None
    assert _refused(limiter, "b", "datago") is None
    assert _refused(limiter, "a", "localdata") is None
    with limiter.probing("c", "datago"):
        assert _refused(limiter, "d", "datago") is None


def test_a_probe_that_raises_still_ends_the_users_turn() -> None:
    clock = _Clock()
    limiter = _limiter(clock)

    with pytest.raises(RuntimeError), limiter.probing("a", "datago"):
        raise RuntimeError("boom")

    assert _refused(limiter, "a", "localdata") is None
    # It reached the provider before it failed, so the interval holds.
    assert _refused(limiter, "a", "datago") == 30


def test_zero_turns_the_interval_off_and_keeps_one_at_a_time() -> None:
    clock = _Clock()
    limiter = _limiter(clock, interval=0.0)

    assert _refused(limiter, "a", "datago") is None
    assert _refused(limiter, "a", "datago") is None
    with limiter.probing("a", "datago"):
        assert _refused(limiter, "a", "datago") == 1


def test_entries_past_the_interval_are_dropped() -> None:
    clock = _Clock()
    limiter = _limiter(clock)
    for index in range(50):
        assert _refused(limiter, f"user-{index}", "datago") is None

    clock.now += 30
    assert _refused(limiter, "someone", "datago") is None

    assert list(limiter._ended) == [("someone", "datago")]


def test_two_threads_of_one_user_cannot_both_probe() -> None:
    limiter = ProbeLimiter(interval_seconds=lambda: 0.0)
    inside = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def first() -> None:
        with limiter.probing("a", "datago"):
            inside.set()
            release.wait(timeout=10)
        outcomes.append("first done")

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert inside.wait(timeout=10)
        assert _refused(limiter, "a", "datago") == 1
    finally:
        release.set()
        worker.join(timeout=10)
    assert outcomes == ["first done"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, DEFAULT_PROBE_INTERVAL_SECONDS),
        ("", DEFAULT_PROBE_INTERVAL_SECONDS),
        ("5", 5.0),
        ("0", 0.0),
        ("-1", DEFAULT_PROBE_INTERVAL_SECONDS),
        ("soon", DEFAULT_PROBE_INTERVAL_SECONDS),
        ("inf", DEFAULT_PROBE_INTERVAL_SECONDS),
        ("nan", DEFAULT_PROBE_INTERVAL_SECONDS),
    ],
)
def test_the_interval_setting(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: float
) -> None:
    if raw is None:
        monkeypatch.delenv(PROBE_INTERVAL_ENV, raising=False)
    else:
        monkeypatch.setenv(PROBE_INTERVAL_ENV, raw)

    assert probe_interval_seconds() == expected


# --- Through the service ---


@dataclass(frozen=True)
class _Outcome:
    service_id: str = "svc"
    status: str = "available"
    detail: str = ""
    http_status: int | None = 200


@dataclass
class _Probe:
    asked: list[str] = field(default_factory=list)

    @contextmanager
    def open(self, provider: str, key: str) -> Iterator[provider_probe.ProbeOne]:
        del provider, key
        yield self._one

    def _one(self, dataset_id: str) -> _Outcome:
        self.asked.append(dataset_id)
        return _Outcome()


def _service(tmp_path: Path, probe: _Probe, clock: _Clock) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=cli._create_client,
        open_probe=probe.open,
        probe_datasets=lambda provider: [f"{provider}.one"],
        probe_limiter=_limiter(clock),
    )


def _user(name: str) -> Principal:
    return Principal(kind="oidc", owner_id=f"oidc:{name}")


def _probe_as(
    service: BuilderService,
    user: Principal,
    provider: str = "datago",
    body: dict[str, JsonValue] | None = None,
) -> ServiceResponse:
    with request_credentials.request_scope({provider: _KEY}):
        return service.probe_provider(provider, body, principal=user)


def test_a_repeat_within_the_interval_is_refused_and_calls_nothing(tmp_path: Path) -> None:
    probe, clock = _Probe(), _Clock()
    service = _service(tmp_path, probe, clock)

    assert _probe_as(service, _user("a")).status_code == 200
    clock.now += 10
    refused = _probe_as(service, _user("a"))

    assert refused.status_code == 429
    assert refused.body["code"] == "probe_rate_limited"
    assert refused.body["retry_after_seconds"] == 20
    schema = response_schema(_CONTRACT, "/providers/{provider}/probe", "post", 429)
    assert schema is not None
    assert validate(refused.body, schema, _CONTRACT) == []
    assert probe.asked == ["datago.one"]  # the one call of the first probe

    clock.now += 20
    assert _probe_as(service, _user("a")).status_code == 200
    assert len(probe.asked) == 2


def test_another_users_probe_goes_through(tmp_path: Path) -> None:
    probe, clock = _Probe(), _Clock()
    service = _service(tmp_path, probe, clock)

    assert _probe_as(service, _user("a")).status_code == 200
    assert _probe_as(service, _user("b")).status_code == 200
    assert _probe_as(service, _user("a")).status_code == 429
    assert len(probe.asked) == 2


def test_a_request_refused_for_its_content_does_not_use_the_users_turn(tmp_path: Path) -> None:
    probe, clock = _Probe(), _Clock()
    service = _service(tmp_path, probe, clock)

    assert _probe_as(service, _user("a"), body={"datasets": ["no_such"]}).status_code == 400
    with request_credentials.request_scope(None):
        assert service.probe_provider("datago", None, principal=_user("a")).status_code == 400

    assert _probe_as(service, _user("a")).status_code == 200

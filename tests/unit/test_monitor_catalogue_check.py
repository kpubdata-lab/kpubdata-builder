"""``monitor --check`` on datasets without a spec (#1288).

kpubdata's ``application_required`` datasets are served from adapter catalogues and
have no spec, so ``check_approval`` answered ``SPEC_NOT_FOUND`` for every one of them.
They are now asked of kpubdata's public ``Client.probe``, then of one ``list`` call.
These run against a stubbed client; no provider is reached.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from kpubdata import (
    PROBE_STATUSES,
    AuthError,
    ConfigError,
    DatasetNotFoundError,
    InvalidRequestError,
    ParseError,
    ProviderNotRegisteredError,
    RateLimitError,
    ServiceUnavailableError,
    TransportError,
    TransportTimeoutError,
)

from kpubdata_builder import egress, logging_redaction
from kpubdata_builder.agent import monitor
from kpubdata_builder.agent.monitor import MonitorState, check_approval
from kpubdata_builder.cli import main

_ID = "semas.zone_one"


@dataclass(frozen=True)
class _Probed:
    status: str


class _Dataset:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    def list(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return object()


class _Client:
    """``probe`` answers ``probed``; ``dataset(...).list`` raises ``error`` or succeeds."""

    def __init__(
        self,
        *,
        probed: _Probed | None = None,
        error: Exception | None = None,
        lookup_error: Exception | None = None,
    ) -> None:
        self.probed = probed
        self.lookup_error = lookup_error
        self.list_dataset = _Dataset(error)
        self.probed_ids: list[str] = []

    def probe(self, dataset_id: str) -> _Probed | None:
        self.probed_ids.append(dataset_id)
        return self.probed

    def dataset(self, dataset_id: str) -> _Dataset:
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.list_dataset


@pytest.fixture(autouse=True)
def _egress(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    turns: list[str] = []
    monkeypatch.setattr(egress, "wait_for_call", lambda provider: turns.append(provider) or 0.0)
    return turns


# ------------------------------------------------------------------ probe answers


_EXPECTED_PROBE = {
    "available": "HEALTHY",
    "application_required": "NEEDS_APPLICATION",
    "retired": "BROKEN_ENDPOINT",
    "auth_unknown": None,
    "params_invalid": None,
    "rate_limited": None,
    "temporarily_unavailable": None,
    "network_error": None,
    "insufficient_metadata": None,
}


def test_every_probe_status_kpubdata_gives_is_mapped() -> None:
    assert set(_EXPECTED_PROBE) == set(PROBE_STATUSES)


@pytest.mark.parametrize(("probe_status", "expected"), sorted(_EXPECTED_PROBE.items()))
def test_a_probe_answer_is_mapped_and_no_list_call_follows(
    probe_status: str, expected: str | None
) -> None:
    client = _Client(probed=_Probed(probe_status))

    assert check_approval(_ID, client=client) == expected
    assert client.list_dataset.calls == []


# ------------------------------------------------------------------ list answers


_403 = "data.go.kr returned 403. The API has not been activated (활용신청) for your key."


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (None, "HEALTHY"),
        (AuthError(_403, status_code=403), "NEEDS_APPLICATION"),
        (AuthError("forbidden", status_code=403), "NEEDS_APPLICATION"),
        (AuthError("SERVICE KEY IS NOT REGISTERED", status_code=401), "INVALID_KEY"),
        (TransportError("HTTP status error 403", status_code=403), "NEEDS_APPLICATION"),
        (TransportError("HTTP status error 401", status_code=401), "INVALID_KEY"),
        (RateLimitError("too many", status_code=429), None),
        (TransportTimeoutError("timed out"), None),
        (ServiceUnavailableError("down", status_code=503), None),
        (TransportError("HTTP status error 500", status_code=500), None),
        (ConfigError("no key for datago"), None),
        (InvalidRequestError("start_date is required"), None),
        (ParseError("not XML"), None),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_without_a_probe_answer_one_record_is_listed(
    error: Exception | None, expected: str | None, _egress: list[str]
) -> None:
    client = _Client(error=error)

    assert check_approval(_ID, client=client) == expected
    assert client.probed_ids == [_ID]
    assert client.list_dataset.calls == [{"page_size": 1}]
    # One call to the provider, counted under its egress limit.
    assert _egress == ["semas"]


@pytest.mark.parametrize(
    "error", [DatasetNotFoundError("unknown"), ProviderNotRegisteredError("unknown")]
)
def test_a_dataset_kpubdata_does_not_know_is_spec_not_found(error: Exception) -> None:
    assert check_approval("nope.unknown", client=_Client(lookup_error=error)) == "SPEC_NOT_FOUND"


def test_a_spec_dataset_keeps_the_verify_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import kpubdata_builder.verify as verify
    from kpubdata_builder.verify.models import DatasetStatus, VerifyResult

    seen: list[str] = []

    def fake_verify(spec: object, **_: object) -> VerifyResult:
        seen.append(getattr(spec, "id", ""))
        return VerifyResult(dataset_id="datago.apt_trade", status=DatasetStatus.INVALID_KEY)

    monkeypatch.setattr(verify, "verify_dataset", fake_verify)
    client = _Client(probed=_Probed("available"))

    assert check_approval("datago.apt_trade", client=client) == "INVALID_KEY"
    assert seen == ["datago.apt_trade"]
    assert client.probed_ids == []


# ------------------------------------------------------------------ the command


def _state(tmp_path: Path, *ids: str) -> Path:
    path = tmp_path / "state.yaml"
    state = MonitorState()
    for each in ids:
        state.add(each, status="NEEDS_APPLICATION")
    state.save(path)
    return path


def test_no_verdict_keeps_the_status_and_checks_are_paced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = {"semas.zone_one": None, "semas.zone_radius": "HEALTHY", "localdata.clinic": None}
    monkeypatch.setattr(monitor, "check_approval", lambda dataset_id, **_: answers[dataset_id])
    sleeps: list[float] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    path = _state(tmp_path, *answers)

    assert main(["monitor", "--state-file", str(path), "--check"]) == 0

    statuses = {p.dataset_id: p.status for p in MonitorState.load(path).pending}
    assert statuses == {
        "semas.zone_one": "NEEDS_APPLICATION",
        "semas.zone_radius": "HEALTHY",
        "localdata.clinic": "NEEDS_APPLICATION",
    }
    assert sleeps == [monitor.CHECK_INTERVAL_SECONDS] * 2
    assert "no verdict, unchanged" in capsys.readouterr().out


def test_the_key_is_used_and_never_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kpubdata.transport.http import HttpTransport

    key = "monitor-test-key-0123456789"
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", key)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    seen: list[int] = []

    def refuse(self: HttpTransport, *_: object, **__: object) -> object:
        # The one point every version's provider call passes (kpubdata 0.10 lists through
        # the dataset, main probes through the adapter first). While the client is open,
        # its key is one the log redaction rewrites.
        seen.append(logging_redaction.active_count())
        raise TransportError(f"HTTP 403 for ?serviceKey={key}", status_code=403)

    monkeypatch.setattr(HttpTransport, "request", refuse)
    path = _state(tmp_path, _ID)

    assert main(["monitor", "--state-file", str(path), "--check"]) == 0

    out = capsys.readouterr()
    assert key not in out.out + out.err
    assert key not in path.read_text(encoding="utf-8")
    assert MonitorState.load(path).pending[0].status == "NEEDS_APPLICATION"
    assert seen and seen[0] >= 1
    assert logging_redaction.active_count() == 0

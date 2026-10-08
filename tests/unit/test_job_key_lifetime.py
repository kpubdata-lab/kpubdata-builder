"""How long a queued job's keys are held, which keys it is given, and the preview's
refusal (#1070, the part left after #1082 and #1087).

A waiting job's keys were said to live for a time-to-live, but the clock was read only
when a worker took them: a job that never reached a worker kept its keys in memory for as
long as the queue took. The job was also bound every key the request carried, whichever
providers its spec called.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.build_limits import MAX_ACTIVE_BUILDS_PER_OWNER_ENV
from kpubdata_builder.service.request_credentials import CREDENTIAL_TTL_ENV, JobCredentials

from .test_ephemeral_credentials import _CANARY, _SPEC, _Recorder
from .test_job_keys_at_submission import (
    _KEYLESS_SPEC,
    _SHARED_SPEC,
    _service,
    _submit,
    _wait,
    multi_user,  # noqa: F401 - a fixture
)

_OTHER = "stand-in-value-for-another-provider"


def _until(condition: object, seconds: float = 5.0) -> bool:
    assert callable(condition)
    for _ in range(int(seconds / 0.01)):
        if condition():
            return True
        threading.Event().wait(0.01)
    return False


# ------------------------------------------------------------- the waiting clock


def test_a_waiting_binding_is_removed_when_its_time_passes_without_being_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CREDENTIAL_TTL_ENV, "0.05")
    store = JobCredentials()
    store.bind("r", "oidc:alice", {"datago": _CANARY})
    held = store._bindings["r"].keys

    # ``holds`` only looks: nothing but the timer removes the binding.
    assert _until(lambda: not store.holds("r"))
    assert held == {}
    assert store.take("r", "oidc:alice") is None


def test_taken_keys_outlive_the_waiting_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once a worker has them they last as long as the build, not the time-to-live."""
    monkeypatch.setenv(CREDENTIAL_TTL_ENV, "0.05")
    store = JobCredentials()
    store.bind("r", "oidc:alice", {"datago": _CANARY})
    timer = store._bindings["r"].timer
    assert timer is not None

    keys = store.take("r", "oidc:alice")

    assert timer.finished.is_set()  # cancelled, so it cannot empty what the worker holds
    threading.Event().wait(0.15)
    assert keys == {"datago": _CANARY}


def test_discard_empties_the_binding_and_stops_its_timer() -> None:
    store = JobCredentials()
    store.bind("r", "oidc:alice", {"datago": _CANARY})
    binding = store._bindings["r"]

    store.discard("r")

    assert binding.keys == {}
    assert binding.timer is not None and binding.timer.finished.is_set()
    assert not store.holds("r")


def test_a_wrong_owner_gets_nothing_and_the_keys_are_dropped() -> None:
    store = JobCredentials()
    store.bind("r", "oidc:alice", {"datago": _CANARY})
    binding = store._bindings["r"]

    assert store.take("r", "oidc:bob") is None
    assert binding.keys == {}
    assert store.take("r", "oidc:alice") is None


def test_an_old_timer_does_not_remove_a_newer_binding(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobCredentials()
    monkeypatch.setenv(CREDENTIAL_TTL_ENV, "0.05")
    store.bind("r", "oidc:alice", {"datago": "first-stand-in-value"})
    first = store._bindings["r"]
    monkeypatch.setenv(CREDENTIAL_TTL_ENV, "3600")
    store.bind("r", "oidc:alice", {"datago": _CANARY})

    assert first.keys == {}
    # The first binding's timer would have fired by now, had it not been cancelled; and
    # fired late, it finds another binding under the id and leaves it.
    threading.Event().wait(0.15)
    store._expire("r", first)
    assert store.take("r", "oidc:alice") == {"datago": _CANARY}


def test_a_job_that_waits_past_the_clock_holds_no_key_before_any_worker_reaches_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    gate = threading.Event()
    recorder = _Recorder(gate=gate)
    service = _service(tmp_path, monkeypatch, recorder)
    try:
        assert _submit(service, _SPEC, "job-1", (f"datago={_CANARY}",)).status_code == 202
        monkeypatch.setenv(CREDENTIAL_TTL_ENV, "0.05")
        assert _submit(service, _SPEC, "job-2", (f"datago={_CANARY}",)).status_code == 202
        # The one worker is still inside job-1: job-2 is queued, and its key goes anyway.
        assert _until(lambda: not service._job_credentials.holds("job-2"))
        snapshot = service._async_builds.get("job-2")
        assert snapshot is not None and snapshot.status == "queued"
    finally:
        gate.set()

    assert _wait(service, "job-1") == "succeeded"
    assert _wait(service, "job-2") == "failed"
    assert service.build_status("job-2").body["code"] == "credentials_required"
    assert all(call["keys"] == {"datago": _CANARY} for call in recorder.calls)


# ------------------------------------------------------- only the keys the spec uses


def _bound(service: BuilderService, run_id: str) -> dict[str, str] | None:
    binding = service._job_credentials._bindings.get(run_id)
    return None if binding is None else dict(binding.keys)


def test_a_job_is_bound_only_the_keys_its_spec_uses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    gate = threading.Event()
    recorder = _Recorder(gate=gate)
    # Four jobs of one owner: the per-owner limit (#1189) is not what this tests.
    monkeypatch.setenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "0")
    service = _service(tmp_path, monkeypatch, recorder)
    headers = (f"datago={_CANARY}", f"seoul={_OTHER}", f"bok={_OTHER}")
    try:
        assert _submit(service, _SPEC, "job-1", headers).status_code == 202
        assert _submit(service, _SPEC, "job-2", headers).status_code == 202
        assert _submit(service, _SHARED_SPEC, "job-3", headers).status_code == 202
        assert _submit(service, _KEYLESS_SPEC, "job-4", headers).status_code == 202

        assert _bound(service, "job-2") == {"datago": _CANARY}
        # localdata calls with datago's key: that entry is the one it reads.
        assert _bound(service, "job-3") == {"datago": _CANARY}
        # A spec that calls no provider needing a key is given none to hold.
        assert _bound(service, "job-4") is None
    finally:
        gate.set()

    for run_id in ("job-1", "job-2", "job-3", "job-4"):
        assert _wait(service, run_id) == "succeeded"
    # Each build still had the key it needed, and no build saw another provider's.
    assert all(_OTHER not in str(call["keys"]) for call in recorder.calls)
    assert sum(call["keys"] == {"datago": _CANARY} for call in recorder.calls) >= 3


def test_a_key_given_under_the_providers_own_name_is_bound_too(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    gate = threading.Event()
    service = _service(tmp_path, monkeypatch, _Recorder(gate=gate))
    try:
        assert _submit(service, _SPEC, "job-1", (f"datago={_CANARY}",)).status_code == 202
        accepted = _submit(service, _SHARED_SPEC, "job-2", (f"localdata={_CANARY}",))
        assert accepted.status_code == 202
        assert _bound(service, "job-2") == {"localdata": _CANARY}
    finally:
        gate.set()
    assert _wait(service, "job-2") == "succeeded"


# ------------------------------------------------------------------ POST /preview


def _preview(
    service: BuilderService,
    spec: str,
    headers: tuple[str, ...] = (),
    **extra: object,
) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        "/preview",
        {"spec": spec, **extra},  # type: ignore[dict-item]
        provider_key_headers=list(headers),
    )
    assert isinstance(response, ServiceResponse)
    return response


def test_a_preview_without_the_key_is_refused_as_a_build_is(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    recorder = _Recorder()
    service = _service(tmp_path, monkeypatch, recorder)

    preview = _preview(service, _SPEC)
    queued = _submit(service, _SPEC, "async-1")

    assert preview.status_code == queued.status_code == 400
    assert preview.body["code"] == queued.body["code"] == "provider_credential_required"
    assert preview.body["providers"] == queued.body["providers"] == ["datago"]
    assert "X-Provider-Key" in str(preview.body["error"])
    assert recorder.calls == []


def test_a_key_for_another_provider_does_not_open_a_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    recorder = _Recorder()
    service = _service(tmp_path, monkeypatch, recorder)

    refused = _preview(service, _SPEC, (f"seoul={_OTHER}",))

    assert refused.status_code == 400
    assert refused.body["code"] == "provider_credential_required"
    assert _OTHER not in str(refused.body)
    assert recorder.calls == []


def test_a_preview_with_the_key_or_needing_none_is_not_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    """Negative: the refusal is for a missing key only."""
    service = _service(tmp_path, monkeypatch, _Recorder())

    for response in (
        _preview(service, _SPEC, (f"datago={_CANARY}",)),
        _preview(service, _SHARED_SPEC, (f"datago={_CANARY}",)),
        _preview(service, _KEYLESS_SPEC),
    ):
        assert response.body.get("code") != "provider_credential_required"


def test_a_preview_parameter_error_is_answered_before_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    multi_user: None,  # noqa: F811
) -> None:
    service = _service(tmp_path, monkeypatch, _Recorder())

    refused = _preview(service, _SPEC, limit=0)

    assert refused.status_code == 400
    assert "limit" in str(refused.body["error"])
    assert "code" not in refused.body


def test_a_single_user_preview_needs_no_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-env-value-1070")
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert _preview(service, _SPEC).body.get("code") != "provider_credential_required"

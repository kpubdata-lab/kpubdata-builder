"""A build that needs a key is refused when submitted without it, and a job whose key is
gone when it starts says so (#1070).

A multi-user deployment has no key but the one a request carries. A build submitted with
none was accepted with 202 and failed later as an ordinary provider error; a job that
waited in the queue past its keys' lifetime ran keyless and failed the same way.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.providers import ProviderDescriptor

from .test_ephemeral_credentials import _ALICE, _CANARY, _SPEC, _Recorder

_KEYLESS_SPEC = _SPEC.replace("provider: datago", "provider: keyless")
_SHARED_SPEC = _SPEC.replace("provider: datago", "provider: localdata")


@pytest.fixture()
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-env-value-1070")


def _service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> BuilderService:
    service = BuilderService(output_root=tmp_path, client_factory=recorder, async_max_workers=1)
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)
    monkeypatch.setattr(
        service._providers_service,
        "runtime_providers",
        lambda: (
            ProviderDescriptor("datago", True),
            ProviderDescriptor("localdata", True),
            ProviderDescriptor("keyless", False),
        ),
    )
    return service


def _submit(
    service: BuilderService, spec: str, run_id: str, headers: tuple[str, ...] = ()
) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        "/builds",
        {"spec": spec, "run_id": run_id},
        provider_key_headers=list(headers),
    )
    assert isinstance(response, ServiceResponse)
    return response


def _wait(service: BuilderService, run_id: str) -> str:
    for _ in range(200):
        snapshot = service._async_builds.get(run_id)
        if snapshot is not None and snapshot.status in ("succeeded", "failed", "cancelled"):
            return snapshot.status
        threading.Event().wait(0.05)
    raise AssertionError("job did not finish")


def test_a_build_that_needs_a_key_is_refused_without_it_and_no_run_is_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    recorder = _Recorder()
    service = _service(tmp_path, monkeypatch, recorder)

    response = _submit(service, _SPEC, "job-1")

    assert response.status_code == 400
    assert response.body["code"] == "provider_credential_required"
    assert response.body["providers"] == ["datago"]
    # Nothing was accepted: no job, no submission record, no event, no directory.
    assert service._async_builds.get("job-1") is None
    assert service._event_store.submission("job-1") is None
    assert not service._event_store.list_for_run("job-1", limit=10, tail=False)
    assert not (tmp_path / "job-1").exists()
    assert recorder.calls == []


def test_a_key_for_another_provider_does_not_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    service = _service(tmp_path, monkeypatch, _Recorder())

    response = _submit(service, _SPEC, "job-1", (f"keyless={_CANARY}",))

    assert (response.status_code, response.body["code"]) == (400, "provider_credential_required")


def test_with_the_key_the_build_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    recorder = _Recorder()
    service = _service(tmp_path, monkeypatch, recorder)

    assert _submit(service, _SPEC, "job-1", (f"datago={_CANARY}",)).status_code == 202
    assert _wait(service, "job-1") == "succeeded"


def test_a_provider_that_shares_datagos_key_is_covered_by_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert _submit(service, _SHARED_SPEC, "job-1", (f"datago={_CANARY}",)).status_code == 202
    assert _submit(service, _SHARED_SPEC, "job-2").body["providers"] == ["localdata"]


def test_a_spec_that_needs_no_key_is_submitted_without_a_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """Negative: only a provider that requires a credential is asked for one."""
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert _submit(service, _KEYLESS_SPEC, "job-1").status_code == 202
    assert _wait(service, "job-1") == "succeeded"


def test_a_single_user_deployment_submits_without_a_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: there the stored and server keys are used, as before."""
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-env-value-1070")
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert _submit(service, _SPEC, "job-1").status_code == 202
    assert _wait(service, "job-1") == "succeeded"


def test_a_job_whose_key_is_gone_when_it_starts_ends_as_credentials_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """The key was there at submission and expired while the job waited for a worker."""
    gate = threading.Event()
    recorder = _Recorder(gate=gate)
    service = _service(tmp_path, monkeypatch, recorder)
    try:
        # The one worker is busy with job-1, so job-2 waits in the queue with its key.
        assert _submit(service, _SPEC, "job-1", (f"datago={_CANARY}",)).status_code == 202
        assert _submit(service, _SPEC, "job-2", (f"datago={_CANARY}",)).status_code == 202
        assert service._job_credentials.holds("job-2")
        # What the lifetime passing does: the binding is dropped before the job starts.
        service._job_credentials.discard("job-2")
    finally:
        gate.set()

    assert _wait(service, "job-1") == "succeeded"
    assert _wait(service, "job-2") == "failed"
    status = service.build_status("job-2")
    assert status.body["code"] == "credentials_required"
    assert str(status.body["error"]).startswith("credentials_required:")
    assert "datago" in str(status.body["error"])
    # It never called the provider keyless, and the timeline says how it ended.
    assert all(call["keys"] == {"datago": _CANARY} for call in recorder.calls)
    events = service._event_store.list_for_run("job-2", limit=20, tail=False)
    assert [event.event for event in events][-1] == "run_failed"
    assert _CANARY not in str(status.body)


# --- The synchronous route gives the same answer ---


def _build(
    service: BuilderService, spec: str, run_id: str, headers: tuple[str, ...] = ()
) -> ServiceResponse:
    response = dispatch(
        service,
        "POST",
        "/build",
        {"spec": spec, "run_id": run_id},
        provider_key_headers=list(headers),
    )
    assert isinstance(response, ServiceResponse)
    return response


def test_a_synchronous_build_without_the_key_is_refused_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """It ran, called the provider keyless and failed as an ordinary provider error."""
    recorder = _Recorder()
    service = _service(tmp_path, monkeypatch, recorder)

    sync = _build(service, _SPEC, "sync-1")
    queued = _submit(service, _SPEC, "async-1")

    assert sync.status_code == 400
    assert sync.body == queued.body
    assert sync.body["code"] == "provider_credential_required"
    assert sync.body["providers"] == ["datago"]
    # Nothing was fetched and nothing was written under the id.
    assert recorder.calls == []
    assert not (tmp_path / "sync-1").exists()


def test_a_synchronous_build_with_the_key_or_needing_none_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """Negative: the refusal is for a missing key only."""
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert _build(service, _SPEC, "sync-1", (f"datago={_CANARY}",)).status_code < 400
    assert _build(service, _KEYLESS_SPEC, "sync-2").status_code < 400


def test_a_single_user_synchronous_build_needs_no_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-env-value-1070")
    service = _service(tmp_path, monkeypatch, _Recorder())

    assert _build(service, _SPEC, "sync-1").status_code < 400

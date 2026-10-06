"""In a multi-user deployment a provider key lives only as long as its request or job (#683).

ADR 0012's 2026-09-30 amendment (D1). A single-user deployment keeps its stored and
environment credentials unchanged.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.providers import CredentialResolver, ProviderDescriptor
from kpubdata_builder.service.request_credentials import (
    JobCredentials,
    current_keys,
    parse_provider_key_headers,
    request_scope,
)
from kpubdata_builder.spec import JsonValue

_CANARY = "canary-provider-value-683"
_OPERATOR_VALUE = "operator-env-value-683"
_ALICE = Principal("oidc", "alice", "oidc:alice")
_SPEC = """\
dataset_id: eph.table
title: Ephemeral
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


class _Dataset:
    def __init__(self, gate: threading.Event | None) -> None:
        self._gate = gate

    def list(self, **_params: object) -> _Result:
        if self._gate is not None:
            self._gate.wait(timeout=10)
        return _Result()


class _Recorder:
    """Client factory recording the keys and environment access each client got."""

    def __init__(self, gate: threading.Event | None = None, fail: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.gate = gate
        self.fail = fail

    def __call__(
        self,
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> object:
        self.calls.append({"keys": dict(provider_keys or {}), "environment_keys": environment_keys})
        recorder = self

        class _Client:
            def dataset(self, _key: str) -> _Dataset:
                if recorder.fail:
                    raise RuntimeError("provider down")
                return _Dataset(recorder.gate)

        return _Client()


@pytest.fixture()
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _OPERATOR_VALUE)


def _files_hold(root: Path, secret: str) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]


# ------------------------------------------------------------------ header parsing


def test_the_header_is_parsed_and_errors_never_echo_the_key() -> None:
    assert parse_provider_key_headers(["datago=a", "Law=b, sgis=c"]) == {
        "datago": "a",
        "law": "b",
        "sgis": "c",
    }
    for bad in (["datago"], ["=x"], ["datago=a", "datago=b"]):
        with pytest.raises(ValueError) as exc:
            parse_provider_key_headers(bad)
        assert "a" not in str(exc.value).replace("datago", "").replace("same", "")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/preview", {"spec": "x"}),
        ("POST", "/build", {"spec": "x"}),
        ("POST", "/builds", {"spec": "x"}),
        ("GET", "/providers", None),
        ("GET", "/providers/datago/status", None),
        ("POST", "/providers/datago/test", None),
        ("POST", "/providers/datago/probe", None),
    ],
)
def test_a_malformed_header_is_a_400_where_a_key_is_read(
    tmp_path: Path, method: str, path: str, body: dict[str, JsonValue] | None
) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=_Recorder())

    response = dispatch(service, method, path, body, provider_key_headers=["nokey"])

    assert isinstance(response, ServiceResponse)
    assert (response.status_code, response.body["code"]) == (400, "invalid_provider_key")


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/healthz", 200),
        ("GET", "/version", 200),
        ("GET", "/builds", 200),
        # A route under /providers that reads no request key.
        ("GET", "/providers/datago/credential", None),
    ],
)
def test_a_malformed_header_does_not_fail_a_route_that_reads_no_key(
    tmp_path: Path, method: str, path: str, expected: int | None
) -> None:
    """It failed every route, the health check included (#1073)."""
    service = BuilderService(output_root=tmp_path, client_factory=_Recorder())

    with_header = dispatch(service, method, path, None, provider_key_headers=["nokey"])
    without = dispatch(service, method, path, None)

    assert isinstance(with_header, ServiceResponse) and isinstance(without, ServiceResponse)
    # The header changes nothing: the answer is the one the route gives without it.
    assert (with_header.status_code, with_header.body) == (without.status_code, without.body)
    assert with_header.body.get("code") != "invalid_provider_key"
    if expected is not None:
        assert with_header.status_code == expected


def test_a_key_with_a_comma_is_malformed() -> None:
    """The comma separates entries, so the rest is read as an entry of its own. Studio
    refuses such a key when it is typed, by the same rule."""
    with pytest.raises(ValueError) as refused:
        parse_provider_key_headers(["datago=first,second"])

    assert "first" not in str(refused.value) and "second" not in str(refused.value)


def test_the_request_scope_ends_with_the_request() -> None:
    with request_scope({"datago": _CANARY}):
        assert current_keys() == {"datago": _CANARY}
    assert current_keys() == {}


# ---------------------------------------------------------------- multi-user mode


def test_a_request_key_is_used_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    recorder = _Recorder()
    service = BuilderService(output_root=tmp_path, client_factory=recorder)
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)

    response = dispatch(
        service,
        "POST",
        "/build",
        {"spec": _SPEC, "run_id": "r1"},
        provider_key_headers=[f"datago={_CANARY}"],
    )

    assert isinstance(response, ServiceResponse) and response.status_code == 200, response.body
    build_calls = [c for c in recorder.calls if c["keys"]]
    assert build_calls and build_calls[0]["keys"] == {"datago": _CANARY}
    assert all(c["environment_keys"] is False for c in recorder.calls)
    assert _files_hold(tmp_path, _CANARY) == []
    assert _files_hold(tmp_path, _OPERATOR_VALUE) == []


def test_without_a_request_key_the_operator_key_is_never_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """Negative: no global or environment credential in multi-user mode."""
    recorder = _Recorder()
    service = BuilderService(output_root=tmp_path, client_factory=recorder)

    service.build(_SPEC, run_id="r1", owner_id=_ALICE.owner_id)

    assert recorder.calls
    assert all(c["keys"] == {} and c["environment_keys"] is False for c in recorder.calls)


def test_a_stored_credential_is_neither_saved_nor_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    repository = SQLiteCredentialRepository(
        tmp_path / "creds.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    )
    repository.put(cast(str, _ALICE.owner_id), "datago", "stored-before-the-switch")
    resolver = CredentialResolver(repository)

    assert resolver.resolve(_ALICE.owner_id, "datago").source == "none"
    with request_scope({"datago": _CANARY}):
        resolved = resolver.resolve(_ALICE.owner_id, "datago")
    assert (resolved.source, resolved.value) == ("request", _CANARY)


def test_saving_a_credential_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=_Recorder())
    monkeypatch.setattr(
        service._providers_service,
        "known_provider",
        lambda provider: ProviderDescriptor(provider, True),
    )

    put = service.put_provider_credential("datago", {"credential": _CANARY}, principal=_ALICE)
    got = service.provider_credential("datago", principal=_ALICE)

    assert (put.status_code, put.body["code"]) == (403, "credential_storage_disabled")
    assert _CANARY not in json.dumps(put.body)
    assert got.status_code == 200 and got.body["configured"] is False
    assert _files_hold(tmp_path, _CANARY) == []


def _listed(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, headers: tuple[str, ...]
) -> dict[str, bool]:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)
    monkeypatch.setattr(
        service._providers_service,
        "runtime_providers",
        lambda: (
            ProviderDescriptor("datago", True),
            ProviderDescriptor("localdata", True),
            ProviderDescriptor("seoul", True),
            ProviderDescriptor("keyless", False),
        ),
    )
    response = dispatch(service, "GET", "/providers", None, provider_key_headers=headers)
    assert isinstance(response, ServiceResponse)
    assert response.status_code == 200, response.body
    providers = cast(list[dict[str, object]], response.body["providers"])
    return {cast(str, item["provider"]): cast(bool, item["configured"]) for item in providers}


def test_the_provider_list_reads_the_keys_the_request_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    """Studio decides whether Add Data may go on from this list.

    Without the header the request is the only place a key could have been, so every
    provider that needs one reads unconfigured — even with the operator's key in the
    environment. With it, a provider that shares datago's key is covered by datago's.
    """
    service = BuilderService(output_root=tmp_path, client_factory=_Recorder())

    without = _listed(service, monkeypatch, ())
    with_key = _listed(service, monkeypatch, (f"datago={_CANARY}",))

    assert without == {"datago": False, "localdata": False, "seoul": False, "keyless": True}
    assert with_key == {"datago": True, "localdata": True, "seoul": False, "keyless": True}
    assert current_keys() == {}
    assert _files_hold(tmp_path, _CANARY) == []


# ------------------------------------------------------------------- async jobs


def _submit(
    service: BuilderService, monkeypatch: pytest.MonkeyPatch, run_id: str
) -> ServiceResponse:
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ALICE)
    response = dispatch(
        service,
        "POST",
        "/builds",
        {"spec": _SPEC, "run_id": run_id},
        provider_key_headers=[f"datago={_CANARY}"],
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


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
def test_a_job_uses_its_key_and_drops_it_when_it_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None, outcome: str
) -> None:
    recorder = _Recorder(fail=outcome == "failed")
    service = BuilderService(output_root=tmp_path, client_factory=recorder, async_max_workers=1)

    assert _submit(service, monkeypatch, "job-1").status_code == 202

    assert _wait(service, "job-1") == outcome
    assert any(c["keys"] == {"datago": _CANARY} for c in recorder.calls)
    assert not service._job_credentials.holds("job-1")
    snapshot = service._async_builds.get("job-1")
    assert _CANARY not in json.dumps(snapshot.to_body() if snapshot else {}, default=str)
    assert _files_hold(tmp_path, _CANARY) == []


def test_a_cancelled_job_drops_its_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    gate = threading.Event()
    blocker = _Recorder(gate=gate)
    service = BuilderService(output_root=tmp_path, client_factory=blocker, async_max_workers=1)
    assert _submit(service, monkeypatch, "running").status_code == 202
    assert _submit(service, monkeypatch, "queued").status_code == 202
    assert service._job_credentials.holds("queued")

    try:
        service.cancel_build("queued")
        assert not service._job_credentials.holds("queued")
    finally:
        gate.set()
    _wait(service, "running")
    assert not service._job_credentials.holds("running")


def test_job_keys_expire_and_never_cross_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobCredentials()
    store.bind("r", "oidc:alice", {"datago": _CANARY})
    assert store.take("r", "oidc:bob") is None
    assert _CANARY not in repr(store)

    monkeypatch.setenv("KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS", "0.001")
    store.bind("r2", "oidc:alice", {"datago": _CANARY})
    threading.Event().wait(0.01)
    assert store.take("r2", "oidc:alice") is None


def test_a_restart_fails_interrupted_runs_as_credentials_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, multi_user: None
) -> None:
    gate = threading.Event()
    first = BuilderService(output_root=tmp_path, client_factory=_Recorder(gate=gate))
    assert _submit(first, monkeypatch, "in-flight").status_code == 202

    # A new process: the registry and the in-memory keys are gone.
    second = BuilderService(output_root=tmp_path, client_factory=_Recorder())
    marked = second.mark_interrupted_runs()
    gate.set()

    assert "in-flight" in marked
    events = second._event_store.list_for_run("in-flight", limit=50, tail=False)
    failed = [e for e in events if e.event == "run_failed"]
    assert failed and (failed[0].message or "").startswith("credentials_required")


# ---------------------------------------------------------------- single-user mode


def test_a_single_user_deployment_keeps_its_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: stored and environment credentials work as before."""
    monkeypatch.delenv("ENFORCE_OWNERSHIP", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _OPERATOR_VALUE)
    repository = SQLiteCredentialRepository(
        tmp_path / "creds.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    )
    resolver = CredentialResolver(repository)

    assert (resolver.resolve("owner", "datago").source) == "server"
    repository.put("owner", "datago", "stored")
    assert resolver.resolve("owner", "datago").value == "stored"
    service = BuilderService(output_root=tmp_path, client_factory=_Recorder())
    assert service.mark_interrupted_runs() == ()

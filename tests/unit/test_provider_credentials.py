"""Issue #492 Provider credential/status/test regression tests."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path
from typing import cast

import pytest
from kpubdata.exceptions import AuthError, ProviderResponseError, TransportError

from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.http import _clear_cors_cache, make_handler
from kpubdata_builder.service.providers import (
    ProviderDescriptor,
    provider_descriptors,
    run_provider_test,
)
from kpubdata_builder.spec import JsonValue

_SPEC = """\
dataset_id: provider-test
title: Provider test
description: Provider credential resolution test
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
"""

_FILE_SPEC = """\
dataset_id: async-file-boundary
title: Async file boundary
description: Async file sources remain unsupported
sources:
  - kind: file
    upload_id: {upload_id}
    format: csv
    encoding: utf-8
exports:
  - kind: jsonl
    output_path: out/data.jsonl
"""


class _Ref:
    def __init__(self, provider: str, dataset: str) -> None:
        self.provider = provider
        self.dataset_key = dataset
        self.id = f"{provider}.{dataset}"
        self.name = dataset
        self.raw_metadata: dict[str, object] = {}


class _Catalog:
    def __init__(self, refs: list[_Ref]) -> None:
        self._refs = refs

    def list(self) -> list[_Ref]:
        return list(self._refs)


class _Result:
    def __init__(self) -> None:
        self.items: Iterable[dict[str, JsonValue]] = ({"id": "1", "value": 1},)


class _Dataset:
    def list(self, **params: object) -> _Result:
        return _Result()


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name


class _Client:
    def __init__(self, provider_keys: dict[str, str]) -> None:
        self.provider_keys = dict(provider_keys)
        self.datasets = _Catalog([_Ref("datago", "air_quality"), _Ref("krx", "stock")])
        self.closed = False

    def dataset(self, dataset_id: str) -> _Dataset:
        return _Dataset()

    def iter_authenticated_providers(self) -> tuple[_Provider, ...]:
        return (_Provider("datago"),)

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.calls: list[tuple[dict[str, str], float | None, bool | None, _Client]] = []

    def __call__(
        self,
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
    ) -> _Client:
        client = _Client(provider_keys or {})
        self.calls.append((dict(provider_keys or {}), timeout, cache, client))
        return client


class _LazyRegistry:
    def __iter__(self) -> Iterable[str]:
        return iter(("datago", "krx"))

    def get(self, name: str) -> _Provider:
        if name == "krx":
            raise ModuleNotFoundError("No module named 'pandas'", name="pandas")
        return _Provider(name)


class _OptionalProviderClient:
    _registry = _LazyRegistry()

    class _Catalog:
        @staticmethod
        def list(*, provider: str) -> list[_Ref]:
            return [_Ref(provider, "dataset")]

    datasets = _Catalog()

    def close(self) -> None:
        return None


def test_provider_catalog_skips_provider_with_missing_optional_dependency() -> None:
    assert provider_descriptors(cast(object, _OptionalProviderClient())) == (
        ProviderDescriptor("datago", True),
    )


def test_providers_service_keeps_available_provider_when_krx_pandas_is_missing(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda: cast(object, _OptionalProviderClient()),
        credential_repository=repository,
    )

    response = service.providers(principal=Principal("oidc", "user-a", "oidc:owner-a"))

    assert response.status_code == 200
    assert response.body["providers"] == [
        {"provider": "datago", "requires_credential": True, "configured": False}
    ]


def test_provider_catalog_does_not_hide_unexpected_missing_module() -> None:
    class BrokenRegistry(_LazyRegistry):
        def get(self, name: str) -> _Provider:
            if name == "krx":
                raise ModuleNotFoundError("No module named 'internal_krx'", name="internal_krx")
            return _Provider(name)

    class BrokenClient(_OptionalProviderClient):
        _registry = BrokenRegistry()

    with pytest.raises(ModuleNotFoundError, match="internal_krx"):
        provider_descriptors(cast(object, BrokenClient()))


@pytest.fixture()
def repository(tmp_path: Path) -> SQLiteCredentialRepository:
    cipher = AesGcmCredentialCipher(b"k" * 32)
    return SQLiteCredentialRepository(tmp_path / "credentials.sqlite3", cipher)


def _service(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    factory: _Factory | None = None,
    *,
    test_operation: object | None = None,
) -> tuple[BuilderService, _Factory]:
    resolved_factory = factory or _Factory()
    output_root = tmp_path / "build"
    output_root.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, object] = {}
    if test_operation is not None:
        kwargs["provider_test_operation"] = test_operation
    return (
        BuilderService(
            output_root=output_root,
            client_factory=resolved_factory,
            credential_repository=repository,
            **kwargs,
        ),
        resolved_factory,
    )


def _wait_for_terminal_job(service: BuilderService, run_id: str) -> dict[str, JsonValue]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = service.build_status(run_id)
        if response.body.get("status") in {"succeeded", "failed", "cancelled"}:
            return response.body
        time.sleep(0.01)
    raise AssertionError(f"async build did not finish: {run_id}")


def test_credential_crud_masks_and_never_returns_raw_secret(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    service, _ = _service(tmp_path, repository)
    principal = Principal("oidc", "user-a", "oidc:owner-a")
    secret = "raw-super-secret-value"

    put = service.put_provider_credential("datago", {"credential": secret}, principal=principal)
    assert put.status_code == 200
    assert put.body["configured"] is True
    assert put.body["masked"] == "********"
    assert secret not in json.dumps(put.body)

    get = service.provider_credential("datago", principal=principal)
    assert get.status_code == 200
    assert set(get.body) == {"configured", "masked", "updated_at"}
    assert secret not in json.dumps(get.body)
    assert "owner_id" not in get.body

    deleted = service.delete_provider_credential("datago", principal=principal)
    assert deleted.status_code == 200
    after = service.provider_credential("datago", principal=principal)
    assert after.body["configured"] is False


def test_repository_encrypts_at_rest_and_isolates_users(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    repository.put("oidc:owner-a", "datago", "secret-user-a")
    repository.put("oidc:owner-b", "datago", "secret-user-b")

    assert repository.get_secret("oidc:owner-a", "datago") == "secret-user-a"
    assert repository.get_secret("oidc:owner-b", "datago") == "secret-user-b"
    assert repository.get_metadata("oidc:owner-c", "datago").configured is False

    database = tmp_path / "credentials.sqlite3"
    raw = database.read_bytes()
    assert b"secret-user-a" not in raw
    assert b"secret-user-b" not in raw
    with sqlite3.connect(database) as connection:
        stored = connection.execute("SELECT ciphertext FROM provider_credentials").fetchall()
    assert all(bytes(row[0]) not in (b"secret-user-a", b"secret-user-b") for row in stored)


def test_user_credential_precedes_server_default_and_delete_falls_back(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPUBDATA_CACHE", "1")
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "server-default")
    principal = Principal("oidc", "user-a", "oidc:owner-a")
    repository.put(cast(str, principal.owner_id), "datago", "user-secret")
    service, factory = _service(tmp_path, repository)

    preview = service.preview(_SPEC, principal=principal)
    assert preview.status_code == 200
    assert factory.calls[-1][0] == {"datago": "user-secret"}
    assert factory.calls[-1][2] is False

    repository.put(cast(str, principal.owner_id), "datago", "updated-user-secret")
    preview_after_update = service.preview(_SPEC, principal=principal)
    assert preview_after_update.status_code == 200
    assert factory.calls[-1][0] == {"datago": "updated-user-secret"}
    assert factory.calls[-1][2] is False

    _ = service.delete_provider_credential("datago", principal=principal)
    preview_after_delete = service.preview(_SPEC, principal=principal)
    assert preview_after_delete.status_code == 200
    assert factory.calls[-1][0] == {"datago": "server-default"}
    assert factory.calls[-1][2] is False


def test_preview_build_and_test_share_resolution_without_client_cache(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    principal_a = Principal("oidc", "user-a", "oidc:owner-a")
    principal_b = Principal("oidc", "user-b", "oidc:owner-b")
    repository.put(cast(str, principal_a.owner_id), "datago", "credential-a")
    repository.put(cast(str, principal_b.owner_id), "datago", "credential-b")
    seen_by_test: list[dict[str, str]] = []

    def operation(client: object, provider: str) -> None:
        seen_by_test.append(dict(cast(_Client, client).provider_keys))

    service, factory = _service(tmp_path, repository, test_operation=operation)

    assert service.preview(_SPEC, principal=principal_a).status_code == 200
    assert service.build(_SPEC, run_id="owner-a", principal=principal_a).status_code == 200
    assert service.provider_status("datago", principal=principal_a).status_code == 200
    assert service.preview(_SPEC, principal=principal_b).status_code == 200

    credential_calls = [keys for keys, _, cache, _ in factory.calls if keys and cache is False]
    assert credential_calls[:3] == [
        {"datago": "credential-a"},
        {"datago": "credential-a"},
        {"datago": "credential-a"},
    ]
    assert credential_calls[-1] == {"datago": "credential-b"}
    assert seen_by_test == [{"datago": "credential-a"}]
    assert len({id(client) for _, _, _, client in factory.calls}) == len(factory.calls)


def test_async_public_api_uses_submitting_user_credential_and_isolates_users(
    tmp_path: Path, repository: SQLiteCredentialRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "different-server-default")
    principal_a = Principal("oidc", "user-a", "oidc:owner-a")
    principal_b = Principal("oidc", "user-b", "oidc:owner-b")
    repository.put(cast(str, principal_a.owner_id), "datago", "credential-a")
    repository.put(cast(str, principal_b.owner_id), "datago", "credential-b")
    service, factory = _service(tmp_path, repository)

    try:
        assert service.preview(_SPEC, principal=principal_a).status_code == 200
        submitted = service.submit_build(
            _SPEC,
            run_id="async-owner-a",
            created_by=principal_a.label,
            owner_id=principal_a.owner_id,
        )
        assert submitted.status_code == 202
        status = _wait_for_terminal_job(service, "async-owner-a")

        assert status["status"] == "succeeded"
        assert [keys for keys, _, _, _ in factory.calls] == [
            {"datago": "credential-a"},
            {"datago": "credential-a"},
        ]
        assert all(cache is False for _, _, cache, _ in factory.calls)
        assert all(client.closed for _, _, _, client in factory.calls)
    finally:
        service._async_builds.shutdown()


def test_async_public_api_falls_back_to_server_default(
    tmp_path: Path, repository: SQLiteCredentialRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "server-default")
    principal = Principal("oidc", "user-without-key", "oidc:owner-without-key")
    service, factory = _service(tmp_path, repository)

    try:
        submitted = service.submit_build(
            _SPEC,
            run_id="async-server-default",
            created_by=principal.label,
            owner_id=principal.owner_id,
        )
        assert submitted.status_code == 202
        status = _wait_for_terminal_job(service, "async-server-default")

        assert status["status"] == "succeeded"
        assert factory.calls[-1][0] == {"datago": "server-default"}
        assert factory.calls[-1][2] is False
        assert factory.calls[-1][3].closed is True
    finally:
        service._async_builds.shutdown()


def test_async_credential_is_not_persisted_or_exposed(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    secret = "async-credential-must-not-leak"
    principal = Principal("oidc", "user-a", "oidc:owner-a")
    repository.put(cast(str, principal.owner_id), "datago", secret)
    service, _ = _service(tmp_path, repository)

    try:
        submitted = service.submit_build(
            _SPEC,
            run_id="async-secret-boundary",
            created_by=principal.label,
            owner_id=principal.owner_id,
        )
        assert submitted.status_code == 202
        status = _wait_for_terminal_job(service, "async-secret-boundary")
        snapshot = service._async_builds.get("async-secret-boundary")
        events = service.get_build_events("async-secret-boundary", limit=100, tail=False)
        manifest = (tmp_path / "build" / "async-secret-boundary" / "manifest.json").read_text(
            encoding="utf-8"
        )

        assert snapshot is not None
        exposed = json.dumps(
            {
                "submission": submitted.body,
                "status": status,
                "snapshot": snapshot.to_body(),
                "events": events.body,
            }
        )
        assert secret not in exposed
        assert secret not in manifest
        assert "owner_id" not in submitted.body
        assert "owner_id" not in snapshot.to_body()
    finally:
        service._async_builds.shutdown()


def test_async_credential_owner_does_not_enable_file_source(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    principal = Principal("oidc", "user-a", "oidc:owner-a")
    service, _ = _service(tmp_path, repository)

    try:
        upload = service.create_upload(
            b"id,value\n1,10\n",
            format="csv",
            encoding="utf-8",
            original_filename="data.csv",
            principal=principal,
        )
        upload_id = cast(str, upload.body["upload_id"])
        submitted = service.submit_build(
            _FILE_SPEC.format(upload_id=upload_id),
            run_id="async-file-unsupported",
            created_by=principal.label,
            owner_id=principal.owner_id,
        )
        assert submitted.status_code == 202
        status = _wait_for_terminal_job(service, "async-file-unsupported")

        assert status["status"] == "failed"
        response = cast(dict[str, JsonValue], status["response"])
        outcomes = cast(list[dict[str, JsonValue]], response["outcomes"])
        assert outcomes[0]["status"] == "failed"
        assert "authenticated, stable principal owner" in cast(str, outcomes[0]["error"])
        manifest = json.loads(
            (tmp_path / "build" / "async-file-unsupported" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["owner_id"] == principal.owner_id
    finally:
        service._async_builds.shutdown()


def test_preview_build_test_and_manifest_scrub_raw_secret(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    secret = "credential-that-must-never-leak"
    principal = Principal("oidc", "user-a", "oidc:owner-a")
    repository.put(cast(str, principal.owner_id), "datago", secret)

    class _LeakyDataset(_Dataset):
        def list(self, **params: object) -> _Result:
            raise RuntimeError(f"provider rejected {secret}")

    class _LeakyClient(_Client):
        def dataset(self, dataset_id: str) -> _Dataset:
            return _LeakyDataset()

    class _LeakyFactory(_Factory):
        def __call__(
            self,
            *,
            provider_keys: dict[str, str] | None = None,
            timeout: float | None = None,
            cache: bool | None = None,
        ) -> _Client:
            client = _LeakyClient(provider_keys or {})
            self.calls.append((dict(provider_keys or {}), timeout, cache, client))
            return client

    def leaky_test(client: object, provider: str) -> None:
        raise RuntimeError(f"test rejected {secret}")

    service, _ = _service(tmp_path, repository, factory=_LeakyFactory(), test_operation=leaky_test)
    preview = service.preview(_SPEC, principal=principal)
    build = service.build(_SPEC, run_id="scrubbed", principal=principal)
    tested = service.provider_status("datago", principal=principal)

    assert secret not in json.dumps(preview.body)
    assert secret not in json.dumps(build.body)
    assert secret not in json.dumps(tested.body)
    assert secret not in (tmp_path / "build" / "scrubbed" / "manifest.json").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    ("error", "category", "response_code"),
    [
        (AuthError("denied", status_code=401), "auth", 401),
        (TransportError("offline"), "network", None),
        (TimeoutError(), "timeout", None),
        (ProviderResponseError("bad", status_code=503), "provider", 503),
        (RuntimeError("other"), "unknown", None),
    ],
)
def test_provider_error_mapping_has_no_raw_message(
    error: Exception, category: str, response_code: int | None
) -> None:
    def operation(client: object, provider: str) -> None:
        raise error

    result = run_provider_test(
        provider="datago", configured=True, client=cast(object, _Client({})), operation=operation
    )
    assert result.status == "failed"
    assert result.error_category == category
    assert result.response_code == response_code
    assert "denied" not in repr(result)
    assert "offline" not in repr(result)


def test_not_configured_status_does_not_create_credential_client(
    tmp_path: Path, repository: SQLiteCredentialRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KPUBDATA_DATAGO_API_KEY", raising=False)
    monkeypatch.delenv("DATAGO_API_KEY", raising=False)
    service, factory = _service(tmp_path, repository)
    principal = Principal("oidc", "user-a", "oidc:owner-a")

    response = service.provider_status("datago", principal=principal)

    assert response.body["status"] == "not_configured"
    assert response.body["configured"] is False
    assert all(not keys for keys, _, _, _ in factory.calls)


def test_put_delete_http_routing_and_cors(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    monkeypatch.setenv("KPUBDATA_BUILDER_ALLOWED_ORIGINS", "https://studio.example")
    _clear_cors_cache()
    service, _ = _service(tmp_path, repository)
    server = HTTPServer(("127.0.0.1", 0), make_handler(service))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{server.server_address[0]}:{server.server_address[1]}"
    try:
        put = urllib.request.Request(
            f"{base}/providers/datago/credential",
            data=json.dumps({"credential": "http-secret"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://studio.example"},
            method="PUT",
        )
        with urllib.request.urlopen(put, timeout=5) as response:
            body = cast(dict[str, object], json.loads(response.read()))
            assert response.status == 200
            assert response.headers["Access-Control-Allow-Origin"] == "https://studio.example"
            assert "http-secret" not in json.dumps(body)

        options = urllib.request.Request(
            f"{base}/providers/datago/credential",
            headers={"Origin": "https://studio.example"},
            method="OPTIONS",
        )
        with urllib.request.urlopen(options, timeout=5) as response:
            methods = response.headers["Access-Control-Allow-Methods"]
            assert "PUT" in methods
            assert "DELETE" in methods

        delete = urllib.request.Request(f"{base}/providers/datago/credential", method="DELETE")
        with urllib.request.urlopen(delete, timeout=5) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
        _clear_cors_cache()


def test_dispatch_rejects_arbitrary_credential_fields(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    service, _ = _service(tmp_path, repository)
    response = dispatch(
        service,
        "PUT",
        "/providers/datago/credential",
        {"credential": "secret", "base_url": "https://evil.example"},
    )
    assert response.status_code == 400
    assert repository.get_metadata(compute_owner_id("dev", "local"), "datago").configured is False

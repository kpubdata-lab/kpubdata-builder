"""The response cache is never authorization (#684).

kpubdata >=0.7 keys its transport cache on method, URL, params and headers with
the credential swapped for a fingerprint (kpubdata#263, closed), so it does not
leak today. The service does not rely on that: in a multi-user deployment the
cache is off for every client the service builds, not only for clients carrying
a personal key — defence in depth, and no disk cache carried across a switch of
deployment mode.

``_SharedCacheFactory`` is a hypothetical regression model, not kpubdata's cache:
one store for the whole process, keyed on the query alone (what a cache would do
if the fingerprint ever fell out of the key), consulted unless the client was
built with ``cache=False``. The tests pin the invariant against that model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.ownership import multi_user_mode
from kpubdata_builder.spec import JsonValue

_SPEC = """\
dataset_id: shared-cache
title: Shared cache
description: Response cache must not stand in for authorization
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
"""

_DATASET = "datago.air_quality"

_USER_A = Principal("oidc", "user-a", compute_owner_id("oidc", "https://issuer", "a"))
_USER_B = Principal("oidc", "user-b", compute_owner_id("oidc", "https://issuer", "b"))

_MULTI_USER_SWITCHES = (
    pytest.param({"OIDC_ISSUER": "https://issuer"}, id="oidc"),
    pytest.param({"ENFORCE_OWNERSHIP": "true"}, id="enforce-ownership"),
)


class _Ref:
    def __init__(self, provider: str, dataset: str) -> None:
        self.provider = provider
        self.id = f"{provider}.{dataset}"
        self.name = dataset
        self.raw_metadata: dict[str, object] = {}


class _Provider:
    def __init__(self, name: str) -> None:
        self.name = name


class _Catalog:
    def list(self) -> list[_Ref]:
        return [_Ref("datago", "air_quality")]


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items: Iterable[dict[str, JsonValue]] = items


class _Dataset:
    def __init__(self, client: _Client, dataset_id: str) -> None:
        self._client = client
        self._dataset_id = dataset_id

    def list(self, **params: object) -> _Result:
        return _Result(self._client.fetch(self._dataset_id))


class _Client:
    def __init__(
        self,
        cache: dict[str, list[dict[str, JsonValue]]] | None,
        provider_keys: dict[str, str],
    ) -> None:
        self._cache = cache
        self.provider_keys = provider_keys
        self.datasets = _Catalog()

    def dataset(self, dataset_id: str) -> _Dataset:
        return _Dataset(self, dataset_id)

    def fetch(self, dataset_id: str) -> list[dict[str, JsonValue]]:
        # Key on the query alone: the regression this model stands for, not kpubdata today.
        if self._cache is not None and dataset_id in self._cache:
            return self._cache[dataset_id]
        owner = self.provider_keys.get("datago", "anonymous")
        rows: list[dict[str, JsonValue]] = [{"id": "1", "fetched_with": owner}]
        if self._cache is not None:
            self._cache[dataset_id] = rows
        return rows

    def iter_authenticated_providers(self) -> tuple[_Provider, ...]:
        return (_Provider("datago"),)

    def close(self) -> None:
        return None


class _SharedCacheFactory:
    def __init__(self) -> None:
        self.store: dict[str, list[dict[str, JsonValue]]] = {}
        self.cache_args: list[bool | None] = []

    def __call__(
        self,
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> _Client:
        del environment_keys  # multi-user mode keeps clients from the environment (#683)
        self.cache_args.append(cache)
        return _Client(None if cache is False else self.store, dict(provider_keys or {}))


@pytest.fixture(autouse=True)
def _single_user_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OIDC_ISSUER", "ENFORCE_OWNERSHIP", "KPUBDATA_DATAGO_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KPUBDATA_CACHE", "1")


@pytest.fixture()
def repository(tmp_path: Path) -> SQLiteCredentialRepository:
    return SQLiteCredentialRepository(
        tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    )


def _service(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> tuple[BuilderService, _SharedCacheFactory]:
    factory = _SharedCacheFactory()
    output_root = tmp_path / "build"
    output_root.mkdir()
    service = BuilderService(
        output_root=output_root, client_factory=factory, credential_repository=repository
    )
    return service, factory


@pytest.mark.parametrize("switch", _MULTI_USER_SWITCHES)
def test_user_b_never_receives_user_a_cached_response(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    monkeypatch: pytest.MonkeyPatch,
    switch: dict[str, str],
) -> None:
    for name, value in switch.items():
        monkeypatch.setenv(name, value)
    service, factory = _service(tmp_path, repository)
    # A's response is already in the shared cache — written by any client that
    # had the cache on, e.g. before this deployment went multi-user.
    factory.store[_DATASET] = [{"id": "1", "fetched_with": "key-of-a"}]
    repository.put(cast(str, _USER_A.owner_id), "datago", "key-of-a")

    response = service.preview(_SPEC, principal=_USER_B)

    assert response.status_code == 200, response.body
    body = json.dumps(response.body)
    assert "key-of-a" not in body
    # B's preview carries rows B fetched itself — not an empty answer that proves nothing.
    assert "anonymous" in body
    assert factory.cache_args and all(arg is False for arg in factory.cache_args)


@pytest.mark.parametrize("switch", _MULTI_USER_SWITCHES)
def test_multi_user_mode_turns_the_cache_off_for_every_client(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    monkeypatch: pytest.MonkeyPatch,
    switch: dict[str, str],
) -> None:
    for name, value in switch.items():
        monkeypatch.setenv(name, value)
    assert multi_user_mode() is True
    service, factory = _service(tmp_path, repository)
    repository.put(cast(str, _USER_A.owner_id), "datago", "key-of-a")

    # With a personal key, without one, and with no principal-bound provider at all.
    assert service.preview(_SPEC, principal=_USER_A).status_code == 200
    assert service.preview(_SPEC, principal=_USER_B).status_code == 200
    assert service.providers(principal=_USER_B).status_code == 200

    assert len(factory.cache_args) >= 3
    assert all(arg is False for arg in factory.cache_args)
    assert factory.store == {}


def test_single_user_keyless_client_keeps_the_configured_cache(
    tmp_path: Path, repository: SQLiteCredentialRepository
) -> None:
    """Non-goal: one user sharing a cache with themselves is not an exposure."""
    assert multi_user_mode() is False
    service, factory = _service(tmp_path, repository)

    assert service.preview(_SPEC, principal=Principal("dev")).status_code == 200

    assert factory.cache_args == [None]


def test_multi_user_mode_refuses_a_factory_that_cannot_turn_the_cache_off(
    tmp_path: Path, repository: SQLiteCredentialRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer")
    shared = _SharedCacheFactory()

    def no_cache_keyword(*, timeout: float | None = None) -> _Client:
        return shared(timeout=timeout)

    service = BuilderService(
        output_root=tmp_path, client_factory=no_cache_keyword, credential_repository=repository
    )

    with pytest.raises(RuntimeError, match="cannot disable the shared response cache"):
        service._create_client(principal=_USER_B)


@pytest.mark.parametrize("switch", _MULTI_USER_SWITCHES)
def test_provider_status_builds_its_client_without_the_cache(
    tmp_path: Path,
    repository: SQLiteCredentialRepository,
    monkeypatch: pytest.MonkeyPatch,
    switch: dict[str, str],
) -> None:
    """The connection test is another way in: it must not read or fill the cache either."""
    for name, value in switch.items():
        monkeypatch.setenv(name, value)
    service, factory = _service(tmp_path, repository)
    repository.put(cast(str, _USER_A.owner_id), "datago", "key-of-a")

    assert service.provider_status("datago", principal=_USER_A).status_code == 200

    assert factory.cache_args and all(arg is False for arg in factory.cache_args)
    assert factory.store == {}

"""REQUIRE_OWN_PROVIDER_CREDENTIAL keeps the operator's key out of every request (#786).

The resolver said "none" correctly, but ``provider_keys()`` dropped it into an empty
dict, ``_create_client`` then passed no keys, and ``Client.from_env`` read the
operator's key from the environment on its own. The switch's decision was lost at the
one step that mattered. These go through ``BuilderService``, as the issue asks.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from kpubdata_builder.cli import _create_client
from kpubdata_builder.credentials import AesGcmCredentialCipher, SQLiteCredentialRepository
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue

_SPEC = """\
dataset_id: own.key
title: Own key
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""

_NO_KEY = Principal("oidc", "no-key", "oidc:no-key")
_HAS_KEY = Principal("oidc", "has-key", "oidc:has-key")


class _Result:
    def __init__(self) -> None:
        self.items: Iterable[dict[str, JsonValue]] = ({"id": "1"},)


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = keys

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()

    def close(self) -> None:
        return None


class _Factory:
    """Records what each client was built with, including whether env keys were allowed."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        provider_keys: dict[str, str] | None = None,
        timeout: float | None = None,
        cache: bool | None = None,
        environment_keys: bool = True,
    ) -> _Client:
        self.calls.append({"keys": dict(provider_keys or {}), "environment_keys": environment_keys})
        return _Client(dict(provider_keys or {}))


@pytest.fixture
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BuilderService, _Factory]:
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-key")
    monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", "true")
    repository = SQLiteCredentialRepository(
        tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
    )
    repository.put("oidc:has-key", "datago", "own-key")
    factory = _Factory()
    runs = tmp_path / "runs"
    runs.mkdir()
    return (
        BuilderService(output_root=runs, client_factory=factory, credential_repository=repository),
        factory,
    )


def test_build_is_refused_before_any_client_exists(
    service: tuple[BuilderService, _Factory],
) -> None:
    builder, factory = service

    response = builder.build(_SPEC, run_id="r1", principal=_NO_KEY)

    assert response.status_code == 403
    assert response.body["code"] == "provider_credential_required"
    assert response.body["providers"] == ["datago"]
    assert factory.calls == []


def test_preview_is_refused_before_any_client_exists(
    service: tuple[BuilderService, _Factory],
) -> None:
    builder, factory = service

    response = builder.preview(_SPEC, principal=_NO_KEY)

    assert response.status_code == 403
    assert factory.calls == []


def test_the_catalog_client_carries_no_operator_key(
    service: tuple[BuilderService, _Factory],
) -> None:
    """The catalog needs no key, but its client must not pick one up from the env."""
    builder, factory = service

    builder.catalog()

    assert factory.calls and all(c["environment_keys"] is False for c in factory.calls)
    assert all(c["keys"] == {} for c in factory.calls)


def test_a_requester_with_their_own_key_is_served_with_it(
    service: tuple[BuilderService, _Factory],
) -> None:
    builder, factory = service

    assert builder.build(_SPEC, run_id="r2", principal=_HAS_KEY).status_code == 200
    assert factory.calls[-1] == {"keys": {"datago": "own-key"}, "environment_keys": False}


def test_an_async_build_without_a_key_fails_without_using_the_operator_key(
    service: tuple[BuilderService, _Factory],
) -> None:
    builder, factory = service

    builder.submit_build(_SPEC, run_id="async-1", owner_id=_NO_KEY.owner_id)
    import time

    deadline = time.monotonic() + 5
    while builder.build_status("async-1").body.get("status") not in {"failed", "succeeded"}:
        assert time.monotonic() < deadline
        time.sleep(0.02)

    assert builder.build_status("async-1").body["status"] == "failed"
    assert factory.calls == []


def test_switch_off_keeps_the_operator_fallback(
    service: tuple[BuilderService, _Factory], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Single-user deployments rely on the operator key; nothing changes without the switch."""
    monkeypatch.delenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL")
    builder, factory = service

    assert builder.build(_SPEC, run_id="r3", principal=_NO_KEY).status_code == 200
    assert factory.calls[-1] == {"keys": {"datago": "operator-key"}, "environment_keys": True}


def test_the_default_factory_really_leaves_the_environment_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real kpubdata client: with environment_keys=False the env key is absent."""
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-key")

    kept_out = _create_client(environment_keys=False)
    default = _create_client()

    assert "operator-key" not in repr(vars(kept_out._config))  # type: ignore[attr-defined]
    assert default._config.provider_keys.get("datago") == "operator-key"  # type: ignore[attr-defined]


def test_a_factory_that_cannot_keep_the_env_out_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", "true")
    runs = tmp_path / "runs"
    runs.mkdir()
    builder = BuilderService(output_root=runs, client_factory=lambda **_kw: _Client({}))
    builder._client_factory = lambda provider_keys=None, cache=None: _Client({})  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="operator's credentials"):
        builder._create_client()

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

from kpubdata_builder.cli import _create_client, client_keeps_environment_keys_out
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


def test_a_kpubdata_without_env_keys_is_refused_not_silently_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kpubdata 0.8.0 swallows ``env_keys=False`` and still reads the environment (#990).

    A client built without the operator's keys in ``provider_keys`` looks them up in
    the environment when it is used, so the old check — the key is not in ``_config`` —
    passed while the key was still in reach.
    """

    class _OldClient:
        def __init__(self, *, provider_keys: object = None, **extra: object) -> None:
            raise AssertionError("a client that cannot keep the env out must not be built")

    monkeypatch.setattr("kpubdata.Client", _OldClient)
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-key")

    assert client_keeps_environment_keys_out() is False
    with pytest.raises(RuntimeError, match="cannot keep the environment's provider keys out"):
        _create_client(environment_keys=False)


def test_env_keys_false_is_passed_to_a_kpubdata_that_has_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[dict[str, object]] = []

    class _NewClient:
        def __init__(
            self,
            *,
            provider_keys: object = None,
            timeout: float = 30.0,
            cache: bool = False,
            env_keys: bool = True,
        ) -> None:
            built.append({"provider_keys": provider_keys, "env_keys": env_keys})

    monkeypatch.setattr("kpubdata.Client", _NewClient)

    assert client_keeps_environment_keys_out() is True
    _create_client(provider_keys={"datago": "own"}, environment_keys=False)

    assert built == [{"provider_keys": {"datago": "own"}, "env_keys": False}]


@pytest.mark.skipif(
    not client_keeps_environment_keys_out(),
    reason="the installed kpubdata has no env_keys (0.8.0); the refusal is tested above",
)
def test_the_real_client_cannot_reach_the_operators_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Behaviour, not storage: the key lookup itself answers None for every spelling."""
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-key")
    monkeypatch.setenv("DATAGO_API_KEY", "operator-key")

    kept_out = _create_client(environment_keys=False)
    default = _create_client()

    assert kept_out._config.get_provider_key("datago") is None  # type: ignore[attr-defined]
    assert default._config.get_provider_key("datago") == "operator-key"  # type: ignore[attr-defined]


def test_serve_refuses_to_start_when_it_cannot_keep_the_promise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Fail closed at start, not on the first keyless build (#990)."""
    from kpubdata_builder import cli

    monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", "true")
    monkeypatch.setattr(cli, "client_keeps_environment_keys_out", lambda: False)

    def _must_not_serve(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the service must not start")

    monkeypatch.setattr("kpubdata_builder.service.http.serve", _must_not_serve)

    code = cli._run_serve(output_dir=str(tmp_path), host="127.0.0.1", port=0, max_workers=1)

    assert code == 1
    assert "cannot keep the environment's provider keys out" in capsys.readouterr().err


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

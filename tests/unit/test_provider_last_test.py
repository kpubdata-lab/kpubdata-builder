"""Provider connection tests call a dataset that can succeed, and are remembered (#842).

The test used to call the provider's first LIST dataset with no parameters, so a valid key
failed whenever that dataset needed one, and its result was not kept anywhere.
"""

from __future__ import annotations

from pathlib import Path
from types import MappingProxyType
from typing import cast

import pytest
import yaml
from kpubdata import Operation

from kpubdata_builder import cli
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.providers import (
    ProviderNotTestable,
    default_provider_test,
    select_test_target,
)
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
_KEY = "canary-provider-key-842"


class _Ref:
    def __init__(
        self,
        dataset_id: str,
        params: list[dict[str, object]] | None,
        *,
        application: dict[str, object] | None = None,
        operations: frozenset[Operation] = frozenset({Operation.LIST}),
    ) -> None:
        self.id = dataset_id
        self.provider = dataset_id.split(".")[0]
        self.operations = operations
        metadata: dict[str, object] = {}
        if params is not None:
            metadata["request_parameters"] = tuple(MappingProxyType(p) for p in params)
        if application is not None:
            metadata["application"] = MappingProxyType(application)
        self.raw_metadata = MappingProxyType(metadata)


def _required(name: str, example: object, kind: str = "string") -> dict[str, object]:
    return {"name": name, "required": True, "example": example, "type": kind}


def test_the_target_is_a_dataset_that_needs_no_guess() -> None:
    refs = [
        _Ref("datago.a_undeclared", None),
        _Ref("datago.b_no_example", [{"name": "x", "required": True, "type": "string"}]),
        _Ref("datago.c_date", [_required("base_date", "20260909", "date_yyyymmdd")]),
        _Ref("datago.d_application", [], application={"required": True, "url": "https://x"}),
        _Ref("datago.e_ok", [_required("sido", "서울"), {"name": "opt", "required": False}]),
        _Ref("datago.f_ok_later", [_required("sido", "부산")]),
        _Ref("datago.g_no_list", [], operations=frozenset({Operation.RAW})),
    ]

    assert select_test_target(refs, "datago") == ("datago.e_ok", {"sido": "서울"})


def test_nothing_qualifying_is_not_testable_not_a_guess() -> None:
    """Negative: undeclared parameters are unknown, not absent."""
    assert select_test_target([_Ref("bok.base_rate", None)], "bok") is None


class _Dataset:
    def __init__(self, calls: list[tuple[str, dict[str, object]]], dataset_id: str) -> None:
        self._calls = calls
        self._id = dataset_id

    def list(self, **params: object) -> list[object]:
        self._calls.append((self._id, params))
        return []


class _Datasets:
    def __init__(self, refs: list[_Ref]) -> None:
        self._refs = refs

    def list(self) -> list[_Ref]:
        return self._refs


class _Client:
    def __init__(self, refs: list[_Ref]) -> None:
        self.datasets = _Datasets(refs)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def dataset(self, dataset_id: str) -> _Dataset:
        return _Dataset(self.calls, dataset_id)


def test_the_default_test_calls_the_target_with_its_examples() -> None:
    client = _Client([_Ref("datago.e_ok", [_required("sido", "서울")])])

    assert default_provider_test(client, "datago") == "datago.e_ok"  # type: ignore[arg-type]
    assert client.calls == [("datago.e_ok", {"page": 1, "page_size": 1, "sido": "서울"})]


def test_the_default_test_refuses_to_guess() -> None:
    client = _Client([_Ref("bok.base_rate", None)])

    with pytest.raises(ProviderNotTestable):
        default_provider_test(client, "bok")  # type: ignore[arg-type]
    assert client.calls == []


def _service(tmp_path: Path, operation: object) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=cli._create_client,
        provider_test_operation=operation,  # type: ignore[arg-type]
    )


def _providers(service: BuilderService, principal: Principal) -> dict[str, dict[str, JsonValue]]:
    response = service.providers(principal=principal)
    assert response.status_code == 200, response.body
    return {
        cast(str, p["provider"]): p
        for p in cast(list[dict[str, JsonValue]], response.body["providers"])
    }


_ALICE = Principal("oidc", "alice", "oidc:alice")
_BOB = Principal("oidc", "bob", "oidc:bob")


def test_the_last_test_is_kept_per_principal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _KEY)
    service = _service(tmp_path, lambda client, provider: "datago.e_ok")
    assert _providers(service, _ALICE)["datago"]["last_test"] is None

    tested = service.provider_status("datago", principal=_ALICE)

    assert (tested.body["status"], tested.body["dataset"]) == ("connected", "datago.e_ok")
    last = cast(dict[str, JsonValue], _providers(service, _ALICE)["datago"]["last_test"])
    assert (last["status"], last["dataset"], last["checked_at"]) == (
        "connected",
        "datago.e_ok",
        tested.body["checked_at"],
    )
    assert _providers(service, _BOB)["datago"]["last_test"] is None


def test_not_testable_and_failed_are_kept_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _KEY)

    def not_testable(client: object, provider: str) -> str:
        raise ProviderNotTestable(provider)

    service = _service(tmp_path, not_testable)
    assert service.provider_status("datago", principal=_ALICE).body["status"] == "not_testable"
    last = cast(dict[str, JsonValue], _providers(service, _ALICE)["datago"]["last_test"])
    assert last["status"] == "not_testable"

    def failing(client: object, provider: str) -> str:
        raise TimeoutError

    failed = _service(tmp_path, failing)
    failed.provider_status("datago", principal=_ALICE)
    last = cast(dict[str, JsonValue], _providers(failed, _ALICE)["datago"]["last_test"])
    assert (last["status"], last["error_category"]) == ("failed", "timeout")


def test_the_key_is_never_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative: nothing under the output root holds the key after a test."""
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _KEY)
    service = _service(tmp_path, lambda client, provider: "datago.e_ok")
    service.provider_status("datago", principal=_ALICE)
    _providers(service, _ALICE)

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert _KEY.encode() not in path.read_bytes(), path


def test_the_responses_match_the_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", _KEY)
    service = _service(tmp_path, lambda client, provider: "datago.e_ok")
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))

    tested = dispatch(service, "POST", "/providers/datago/test", None)
    listed = dispatch(service, "GET", "/providers", None)

    for response, path, method in (
        (tested, "/providers/{provider}/test", "post"),
        (listed, "/providers", "get"),
    ):
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200, response.body
        schema = response_schema(contract, path, method, 200)
        assert schema is not None
        assert validate(response.body, schema, contract) == []

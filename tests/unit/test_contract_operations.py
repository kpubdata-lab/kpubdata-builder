"""Which method and path is which operation is written once, in the contract (#1109).

Three tables kept by hand in the tests said it again, and each was compared with the
contract — a copy checked against its original. The service spelled out, twice more,
which paths read a credential header. All of that now comes from one table generated
from the contract; these tests hold the table to the contract and the lookup to the
table. Whether the service routes exactly those operations is asked of the service
itself by ``test_dispatch_answers_only_declared_operations.py`` (#1054).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kpubdata_builder.service import _contract_operations as generated
from kpubdata_builder.service.app import API_CONTRACT_VERSION
from kpubdata_builder.service.operations import (
    REST_OF_PATH_PARAMETERS,
    Operation,
    all_operations,
    find_operation,
)

_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = yaml.safe_load((_ROOT / "contract" / "builder-api.yaml").read_text(encoding="utf-8"))
_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_PARAMETER = re.compile(r"\{([^}]+)\}")


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_generate_operations", _ROOT / "scripts" / "generate_operations.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _concrete(path: str) -> str:
    """A request path for a template: every parameter filled with a plain value, and a
    rest-of-path parameter with two segments."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return "dir/file.txt" if name in REST_OF_PATH_PARAMETERS else f"some-{name}"

    return _PARAMETER.sub(fill, path)


# ------------------------------------------------------- the table is the contract


def test_the_generated_table_is_not_stale() -> None:
    """The gate: edit the contract without regenerating and this fails."""
    assert _generator().main(["--check"]) == 0
    assert generated.CONTRACT_VERSION == API_CONTRACT_VERSION == _CONTRACT["info"]["version"]


def test_the_table_holds_every_operation_of_the_contract_and_no_other() -> None:
    """Read from the YAML here a second time, without the generator."""
    declared = {
        (method.upper(), path, operation["operationId"])
        for path, item in _CONTRACT["paths"].items()
        for method, operation in item.items()
        if method.upper() in _METHODS and operation.get("x-planned") is not True
    }

    assert {(op.method, op.path, op.operation_id) for op in all_operations()} == declared
    assert len(declared) == len(all_operations())


def test_operation_ids_are_unique_and_the_generator_refuses_a_repeat() -> None:
    ids = [op.operation_id for op in all_operations()]
    assert len(ids) == len(set(ids))

    document = {
        "info": {"version": "0"},
        "paths": {"/a": {"get": {"operationId": "same"}}, "/b": {"get": {"operationId": "same"}}},
    }
    with pytest.raises(ValueError, match="share operationId same"):
        _generator().operations(document)
    with pytest.raises(ValueError, match="no operationId"):
        _generator().operations({"paths": {"/a": {"get": {}}}})


def test_a_planned_operation_is_not_one_the_service_routes() -> None:
    document = {
        "paths": {
            "/a": {
                "get": {"operationId": "now"},
                "post": {"operationId": "later", "x-planned": True},
            }
        }
    }

    assert [item["operation_id"] for item in _generator().operations(document)] == ["now"]


def test_a_stale_table_fails_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A gate nobody has watched fail is a gate nobody knows works."""
    generator = _generator()
    stale = tmp_path / "_contract_operations.py"
    stale.write_text(
        generator.OUTPUT.read_text(encoding="utf-8").replace("getVersion", "x"), "utf-8"
    )
    monkeypatch.setattr(generator, "OUTPUT", stale)
    monkeypatch.setattr(generator, "ROOT", tmp_path)

    assert generator.main(["--check"]) == 1

    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0


# ------------------------------------------------------------ asking about a request


def test_every_operation_is_found_by_a_request_for_it() -> None:
    for operation in all_operations():
        assert find_operation(operation.method, _concrete(operation.path)) == operation, operation


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/no-such-path"),
        ("GET", "/builds/run-1/no-such-thing"),
        ("POST", "/version"),
        ("GET", "/builds/"),
        ("GET", "builds"),
        ("GET", "/builds//manifest"),
        ("GET", "/providers//status"),
        ("GET", ""),
        ("GET", "/"),
        ("get", "/version"),
        ("POST", "/artifacts/run-1"),
        ("GET", "/artifacts/run-1/"),
    ],
)
def test_a_request_that_is_no_operation_is_none(method: str, path: str) -> None:
    assert find_operation(method, path) is None


def test_a_literal_segment_wins_over_a_parameter_in_the_same_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two templates can fit one request; the adapters take the more literal one."""
    from kpubdata_builder.service import operations

    by_id = Operation("GET", "/things/{thing_id}", "getThing", False, False, True)
    recent = Operation("GET", "/things/recent", "listRecentThings", False, False, True)
    nested = Operation("GET", "/things/{thing_id}/parts/{part}", "getPart", False, False, True)
    for order in ((by_id, recent, nested), (nested, recent, by_id)):
        monkeypatch.setattr(operations, "all_operations", lambda order=order: order)

        assert find_operation("GET", "/things/recent") == recent
        assert find_operation("GET", "/things/42") == by_id
        assert find_operation("GET", "/things/recent/parts/1") == nested
        assert find_operation("GET", "/things/recent/parts") is None


def test_only_the_artifact_file_path_takes_the_rest_of_a_path() -> None:
    rest = [
        (operation.path, name)
        for operation in all_operations()
        for name in _PARAMETER.findall(operation.path)
        if name in REST_OF_PATH_PARAMETERS
    ]

    assert rest == [("/artifacts/{run_id}/{file_path}", "file_path")]
    found = find_operation("GET", "/artifacts/run-1/silver/datago.air/table.parquet")
    assert found is not None and found.operation_id == "getBuildArtifactFile"


def test_the_header_flags_are_what_the_contract_declares() -> None:
    def refs(path: str, method: str) -> set[str]:
        item = _CONTRACT["paths"][path]
        declared = [*(item.get("parameters") or []), *(item[method].get("parameters") or [])]
        return {p["$ref"] for p in declared if "$ref" in p}

    for operation in all_operations():
        declared = refs(operation.path, operation.method.lower())
        assert operation.provider_key is ("#/components/parameters/ProviderKey" in declared)
        assert operation.publish_credential is (
            "#/components/parameters/PublishCredential" in declared
        )
        security = _CONTRACT["paths"][operation.path][operation.method.lower()].get("security")
        assert operation.authenticated is (security != [])
    assert sum(op.provider_key for op in all_operations()) >= 7
    assert sum(op.publish_credential for op in all_operations()) >= 5
    assert [op.operation_id for op in all_operations() if not op.authenticated] == ["healthz"]

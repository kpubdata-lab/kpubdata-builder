"""Response fixtures let a client check that it accepts additive fields (#814).

`contract/fixtures/responses.json` gives every named 2xx response example three ways: as
this contract sends it, with an unknown optional field in every declared object, and with
one required field retyped. A client's contract test consumes it; these tests keep the file
honest from this side:

- it is exactly what the generator makes from the current contract (no drift);
- `current` conforms to the contract as written;
- `with_additive_fields` really adds fields the contract does not declare — a strict parser
  rejects it — and a client that ignores unknown fields accepts it;
- `required_type_broken` is rejected even by that tolerant client.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.service import API_CONTRACT_VERSION

from ._openapi import validate

_ROOT = Path(__file__).parents[2]
_CONTRACT: dict[str, Any] = yaml.safe_load(
    (_ROOT / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)
_FIXTURES_PATH = _ROOT / "contract" / "fixtures" / "responses.json"


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "_generate_response_fixtures", _ROOT / "scripts" / "generate_response_fixtures.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixtures() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(_FIXTURES_PATH.read_text(encoding="utf-8")))


def _schema(entry: dict[str, Any]) -> dict[str, Any]:
    operation = _CONTRACT["paths"][entry["path"]][entry["method"].lower()]
    response = operation["responses"][str(entry["status"])]
    if "$ref" in response:
        name = response["$ref"].rsplit("/", 1)[1]
        response = _CONTRACT["components"]["responses"][name]
    return cast(dict[str, Any], response["content"]["application/json"]["schema"])


def _governing(entry: dict[str, Any]) -> dict[str, Any]:
    """The schema branch the example was written for, so errors point at a field
    rather than at "no oneOf branch matched"."""
    return cast(dict[str, Any], _generator()._branch(_CONTRACT, _schema(entry), entry["current"]))


def _entries() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _fixtures()["fixtures"])


def _id(entry: dict[str, Any]) -> str:
    return f"{entry['operation_id']}:{entry['example']}"


def test_the_committed_fixtures_are_what_the_contract_generates() -> None:
    expected = _generator().render(_CONTRACT)
    assert _FIXTURES_PATH.read_text(encoding="utf-8") == expected, (
        "contract/fixtures/responses.json is stale: run "
        "`uv run python scripts/generate_response_fixtures.py`"
    )


def test_the_fixture_set_names_its_contract_version_and_rules() -> None:
    fixtures = _fixtures()
    assert fixtures["contract_version"] == API_CONTRACT_VERSION
    assert re.fullmatch(r"\d+\.\d+\.\d+", fixtures["contract_version"])
    assert fixtures["fixture_format"] == 1
    assert fixtures["probe_field"] == "future_optional_field"
    assert "ignores fields it does not know" in fixtures["rules"]


def test_the_main_responses_are_covered() -> None:
    covered = {entry["operation_id"] for entry in _entries()}
    assert {
        "getVersion",
        "getCatalog",
        "previewBuild",
        "createBuild",
        "listDatasets",
        "getDataset",
        "getBuildStageDetail",
        "queryBuiltDataset",
        "listWarehouseTables",
        "queryWarehouseTable",
    } <= covered


@pytest.mark.parametrize("entry", _entries(), ids=_id)
def test_current_conforms_to_the_contract_as_written(entry: dict[str, Any]) -> None:
    assert validate(entry["current"], _schema(entry), _CONTRACT) == []


@pytest.mark.parametrize("entry", _entries(), ids=_id)
def test_additive_fields_break_a_strict_parser_and_not_a_tolerant_one(
    entry: dict[str, Any],
) -> None:
    schema = _governing(entry)
    additive = entry["with_additive_fields"]

    assert entry["additive_paths"], "every response must gain at least one field"
    assert validate(additive, schema, _CONTRACT, allow_additional=True) == []
    # Where the contract closes the object, a parser that copies `additionalProperties:
    # false` rejects the later minor's body — the failure #735 hit in Studio. Every
    # rejection is about a probe: the extra field itself, or a closed oneOf branch that
    # no longer matches the object the probe was added to.
    probed = set(entry["additive_paths"])
    for error in validate(additive, schema, _CONTRACT):
        where, _, why = error.partition(": ")
        assert (
            where.endswith(".future_optional_field") and why == "extra property not allowed"
        ) or (where in probed and why == "value matched no oneOf branch"), error


@pytest.mark.parametrize("entry", _entries(), ids=_id)
def test_additive_fields_change_nothing_else(entry: dict[str, Any]) -> None:
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "future_optional_field"}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value

    assert strip(entry["with_additive_fields"]) == entry["current"]


@pytest.mark.parametrize("entry", _entries(), ids=_id)
def test_a_retyped_required_field_is_still_rejected(entry: dict[str, Any]) -> None:
    broken = entry["required_type_broken"]
    field = entry["broken_path"].removeprefix("$.")

    errors = validate(broken, _governing(entry), _CONTRACT, allow_additional=True)
    assert validate(broken, _schema(entry), _CONTRACT, allow_additional=True)

    assert errors, f"{_id(entry)}: retyping {field} went unnoticed"
    assert any(e.startswith(f"$.{field}") for e in errors)
    assert {k: v for k, v in broken.items() if k != field} == {
        k: v for k, v in entry["current"].items() if k != field
    }


def test_the_tolerant_reading_still_requires_required_fields() -> None:
    """Ignoring unknown fields is not ignoring missing ones."""
    schema = {"$ref": "#/components/schemas/WarehouseTable"}
    body = {"table_id": "t", "logical_name": "a.b", "current_snapshot_id": None, "extra": 1}

    errors = validate(body, schema, _CONTRACT, allow_additional=True)

    assert errors == ["$: missing required property 'revision'"]


def test_the_check_mode_fails_on_a_stale_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generator = _generator()
    stale = tmp_path / "responses.json"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(generator, "OUTPUT", stale)
    monkeypatch.setattr(generator, "ROOT", tmp_path)

    assert generator.main(["--check"]) == 1
    assert generator.main([]) == 0
    assert generator.main(["--check"]) == 0


def test_the_silver_column_case_from_735_is_in_the_set() -> None:
    """Studio's strict `silverColumnInfoSchema` broke when 1.30.0 added two fields."""
    (silver,) = [e for e in _entries() if e["example"] == "SilverDetail"]

    assert "$.schema[0]" in silver["additive_paths"]
    strict = validate(silver["with_additive_fields"], _governing(silver), _CONTRACT)
    assert "$.schema[0].future_optional_field: extra property not allowed" in strict

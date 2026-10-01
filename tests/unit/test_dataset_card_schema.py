"""``card.json`` follows the contract's ``DatasetCard`` (#955).

The card's two facts a client used to read out of sentences — a licence mismatch and
"no transformation declared" — are fields: ``provenance[].license_mismatch`` (with
``license_declared`` / ``license_provider``) and ``processing_declared``. Real
``card_sections`` output, from the functions and from a build, validates against the
schema; the contract's examples do too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.catalog_info import DatasetCatalogInfo
from kpubdata_builder.pipeline import card_facts
from kpubdata_builder.pipeline.card_facts import card_source, processing_steps
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.models import SchemaContract
from kpubdata_builder.stages.gold.card import (
    NO_PROCESSING_STEP,
    build_dataset_card,
    card_sections,
    missing_sections,
)

from ._openapi import resolve_ref, validate
from .test_service import _FakeClient
from .test_service_publish import LICENSED_SPEC_YAML

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
_ROWS = [{"id": "1", "v": 10}, {"id": "2", "v": 20}]


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8")))


def _assert_is_card(value: object, contract: dict[str, Any]) -> None:
    schema = resolve_ref(contract, "#/components/schemas/DatasetCard")
    assert validate(value, schema, contract) == []


def _spec(**overrides: Any) -> BuildSpec:
    fields: dict[str, Any] = {
        "dataset_id": "d",
        "title": "t",
        "description": "d",
        "sources": (SourceRef(provider="datago", dataset="apt_trade"),),
        "exports": (ExportTarget(kind="jsonl", output_path="d.jsonl"),),
        "attribution": "국토교통부",
    }
    fields.update(overrides)
    return BuildSpec(**fields)


def _lookup(license_type: str | None) -> card_facts.CatalogLookup:
    return lambda _id: DatasetCatalogInfo("https://www.data.go.kr", license_type, None)


def _sections(spec: BuildSpec, *, provider: str | None) -> dict[str, JsonValue]:
    source = spec.sources[0]
    card = build_dataset_card(
        title=spec.title,
        provenance=(
            card_source(source, spec=spec, provenance=None, label="apt", lookup=_lookup(provider)),
        ),
        processing=processing_steps(source, spec),
        personal_information="No column was declared personal information.",
    )
    return card_sections(card)


def _provenance(sections: dict[str, JsonValue]) -> dict[str, JsonValue]:
    (entry,) = cast(list[dict[str, JsonValue]], sections["provenance"])
    return entry


# ---------------------------------------------------------------- licence fields


def test_a_licence_mismatch_is_a_field(contract: dict[str, Any]) -> None:
    sections = _sections(
        _spec(license="other", license_name="CC-BY-4.0", license_link="https://cc.org/by"),
        provider="KOGL-1",
    )

    _assert_is_card(sections, contract)
    entry = _provenance(sections)
    assert entry["license_declared"] == "CC-BY-4.0 (https://cc.org/by)"
    assert entry["license_provider"] == "KOGL-1"
    assert entry["license_mismatch"] is True
    # The README sentence is unchanged.
    assert entry["license"] == "CC-BY-4.0 (https://cc.org/by); the provider declares: KOGL-1"


def test_matching_licences_are_no_mismatch(contract: dict[str, Any]) -> None:
    sections = _sections(_spec(license="other", license_name="KOGL-1 출처표시"), provider="kogl-1")

    _assert_is_card(sections, contract)
    entry = _provenance(sections)
    assert entry["license_declared"] == "KOGL-1 출처표시"
    assert entry["license_provider"] == "kogl-1"
    assert entry["license_mismatch"] is False
    assert entry["license"] == "KOGL-1 출처표시"


def test_no_provider_terms_are_null_and_no_mismatch(contract: dict[str, Any]) -> None:
    sections = _sections(_spec(license="CC-BY-4.0"), provider=None)

    _assert_is_card(sections, contract)
    entry = _provenance(sections)
    assert entry["license_declared"] == "CC-BY-4.0"
    assert entry["license_provider"] is None
    assert entry["license_mismatch"] is False


def test_only_provider_terms_are_no_mismatch(contract: dict[str, Any]) -> None:
    sections = _sections(_spec(), provider="KOGL-1")

    _assert_is_card(sections, contract)
    entry = _provenance(sections)
    assert entry["license_declared"] is None
    assert entry["license_provider"] == "KOGL-1"
    assert entry["license_mismatch"] is False
    assert entry["license"] == "KOGL-1"


# ---------------------------------------------------------------- processing field


def test_no_processing_is_a_field(contract: dict[str, Any]) -> None:
    sections = _sections(_spec(license="CC-BY-4.0"), provider=None)

    _assert_is_card(sections, contract)
    assert sections["processing"] == [NO_PROCESSING_STEP]
    assert sections["processing_declared"] is False


def test_declared_processing_is_a_field(contract: dict[str, Any]) -> None:
    shaped = SourceRef(
        provider="datago", dataset="apt_trade", schema=SchemaContract(rename={"v": "value"})
    )
    sections = _sections(_spec(license="CC-BY-4.0", sources=(shaped,)), provider=None)

    _assert_is_card(sections, contract)
    assert sections["processing"] == ["Renamed v to value"]
    assert sections["processing_declared"] is True


def test_processing_declared_can_be_stated_explicitly() -> None:
    card = build_dataset_card(title="t", processing=["x"], processing_declared=False)
    assert card_sections(card)["processing_declared"] is False
    assert build_dataset_card(title="t").processing_declared is False


# ---------------------------------------------------------------- built cards


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": _ROWS, "datago.apt_trade": _ROWS})
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: client)


def _built_card(tmp_path: Path, run_id: str, key: str) -> dict[str, JsonValue]:
    text = (tmp_path / run_id / "gold" / key / "card.json").read_text(encoding="utf-8")
    return cast(dict[str, JsonValue], json.loads(text))


def test_a_built_card_is_a_dataset_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: dict[str, Any]
) -> None:
    monkeypatch.setattr(card_facts, "catalog_info", _lookup("KOGL-1"))
    service = _service(tmp_path)

    assert service.build(LICENSED_SPEC_YAML, run_id="r1").status_code == 200

    card = _built_card(tmp_path, "r1", "datago.air_quality")
    _assert_is_card(card, contract)
    assert missing_sections(card) == []
    entry = _provenance(card)
    assert (entry["license_declared"], entry["license_provider"]) == ("CC-BY-4.0", "KOGL-1")
    assert entry["license_mismatch"] is True
    assert card["processing_declared"] is False


def test_a_built_composed_card_is_a_dataset_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, contract: dict[str, Any]
) -> None:
    monkeypatch.setattr(card_facts, "catalog_info", _lookup(None))
    spec = """\
dataset_id: combined.card
title: Combined
description: d
sources:
  - provider: datago
    dataset: air_quality
    alias: air
  - provider: datago
    dataset: apt_trade
    alias: apt
composition:
  name: joined
  join:
    left: air
    right: apt
    left_key: id
    right_key: id
exports:
  - kind: jsonl
    output_path: out/data.jsonl
license: CC-BY-4.0
attribution: 여러 기관
"""
    response = dispatch(_service(tmp_path), "POST", "/build", {"spec": spec, "run_id": "r1"})

    assert response.status_code == 200
    card = _built_card(tmp_path, "r1", "joined")
    _assert_is_card(card, contract)
    # The join is a declared transformation even when neither side declares any.
    assert card["processing_declared"] is True
    for entry in cast(list[dict[str, JsonValue]], card["provenance"]):
        assert entry["license_provider"] is None
        assert entry["license_mismatch"] is False


# ---------------------------------------------------------------- contract


def test_the_contract_examples_are_dataset_cards(contract: dict[str, Any]) -> None:
    media = contract["paths"]["/artifacts/{run_id}/{file_path}"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]
    examples = media["examples"]
    assert set(examples) == {"DatasetCard", "DatasetCardNoProcessing"}
    for example in examples.values():
        _assert_is_card(example["value"], contract)
    assert examples["DatasetCard"]["value"]["provenance"][0]["license_mismatch"] is True
    assert examples["DatasetCardNoProcessing"]["value"]["processing_declared"] is False


def test_the_schema_names_every_field_the_writer_sends(contract: dict[str, Any]) -> None:
    """Drift guard: a field added to ``card_sections`` without the contract fails."""
    sections = _sections(_spec(license="CC-BY-4.0"), provider="KOGL-1")
    card_schema = resolve_ref(contract, "#/components/schemas/DatasetCard")
    source_schema = resolve_ref(contract, "#/components/schemas/DatasetCardSource")
    assert set(sections) == set(card_schema["properties"])
    assert set(_provenance(sections)) == set(source_schema["properties"])


def test_the_route_schema_tells_a_card_from_other_json(contract: dict[str, Any]) -> None:
    """``card.json`` is held to ``DatasetCard``; a manifest is not, and a broken card
    is not passed off as "other JSON"."""
    route = contract["paths"]["/artifacts/{run_id}/{file_path}"]["get"]["responses"]["200"]
    schema = route["content"]["application/json"]["schema"]
    card = _sections(_spec(license="CC-BY-4.0"), provider=None)

    assert validate(card, schema, contract) == []
    assert validate({"run_id": "r1", "outputs": []}, schema, contract) == []
    assert validate({**card, "card_version": "1"}, schema, contract) != []
    assert validate({**card, "processing_declared": "no"}, schema, contract) != []

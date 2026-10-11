"""The catalog's query definition against the real kpubdata catalogue (#1281)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from kpubdata import Client

from kpubdata_builder.service.app import BuilderService

_Dataset = dict[str, object]


def _catalog(tmp_path: Path) -> dict[str, _Dataset]:
    body = BuilderService(output_root=tmp_path, client_factory=Client).catalog().body
    found: dict[str, _Dataset] = {}
    for provider in cast(list[dict[str, object]], body["providers"]):
        for dataset in cast(list[_Dataset], provider["datasets"]):
            found[f"{provider['name']}.{dataset['name']}"] = dataset
    return found


def test_a_spec_dataset_with_an_enum_keeps_type_and_enum(tmp_path: Path) -> None:
    parameters = cast(list[_Dataset], _catalog(tmp_path)["datago.air_station"]["query_parameters"])
    assert parameters
    assert all(parameter["type"] is not None for parameter in parameters)
    with_enum = [parameter for parameter in parameters if parameter["enum"] is not None]
    assert len(with_enum) == 1
    assert isinstance(with_enum[0]["enum"], list)


def test_a_dataset_without_a_definition_is_null_not_empty(tmp_path: Path) -> None:
    assert _catalog(tmp_path)["datago.hospital_info"]["query_parameters"] is None


def test_a_catalogue_dataset_has_parameters_and_no_type(tmp_path: Path) -> None:
    declared = [
        ref
        for ref in Client().datasets.list()
        if any(
            "type" not in parameter
            for parameter in cast(list[_Dataset], ref.to_dict()["request_parameters"] or [])
        )
    ]
    assert declared, "kpubdata no longer has a catalogue entry with parameters"
    catalog = _catalog(tmp_path)
    for ref in declared:
        parameters = cast(list[_Dataset], catalog[ref.id]["query_parameters"])
        assert parameters
        assert all(parameter["type"] is None for parameter in parameters)


def test_no_secret_name_reaches_the_definition(tmp_path: Path) -> None:
    for dataset in _catalog(tmp_path).values():
        for parameter in cast(list[_Dataset] | None, dataset["query_parameters"]) or []:
            assert str(parameter["name"]).lower() not in {"servicekey", "apikey", "api_key"}
    assert (
        "servicekey"
        not in json.dumps([d["query_parameters"] for d in _catalog(tmp_path).values()]).lower()
    )

"""A build that finds no rows does not replace a table's rows (#1186).

Adding ``datago.air_station`` for station A (22 rows) and then again for station B
(no rows) gave the same table id, and the second build committed an empty snapshot
over the first. With no rows there were no columns, so the table could not be queried.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.spec.loader import parse_spec
from kpubdata_builder.spec.serializer import canonical_spec_mapping

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
_DEV = Principal("dev")
_STATION_A: list[dict[str, JsonValue]] = [{"stationName": "A", "pm10": 30 + i} for i in range(22)]


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    """One provider dataset whose answer the test changes between builds."""

    def __init__(self) -> None:
        self.items: list[dict[str, JsonValue]] = list(_STATION_A)

    def list(self, **_params: object) -> _Result:
        return _Result(list(self.items))

    def dataset(self, _key: str) -> _Source:
        return self


def _spec(*, allow_empty: bool | None = None, gold: str = "") -> str:
    source = "  - provider: datago\n    dataset: air_station\n    alias: m\n"
    if allow_empty is not None:
        source += f"    allow_empty: {str(allow_empty).lower()}\n"
    return (
        "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
        + source
        + gold
        + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )


def _service(tmp_path: Path, source: _Source) -> BuilderService:
    return BuilderService(
        output_root=tmp_path, client_factory=lambda **_: source, warehouse_root=tmp_path / "wh"
    )


def _table(service: BuilderService) -> dict[str, Any]:
    response = service.get_warehouse_table("air.m", principal=_DEV)
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.body)


def _select_all(service: BuilderService) -> dict[str, Any]:
    response = service.query_warehouse(
        {"table": "air.m", "sql": "SELECT * FROM dataset"}, principal=_DEV
    )
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], cast(dict[str, Any], response.body)["result"])


def test_a_build_of_no_rows_keeps_the_table_it_would_replace(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="station-a").status_code == 200

    source.items = []
    response = service.build(_spec(), run_id="station-b")

    assert response.status_code == 409, response.body
    body = cast(dict[str, Any], response.body)
    failure = body["warehouse_failures"]["m"]
    assert failure["reason"] == "empty_result"
    assert "22 rows" in failure["detail"]
    assert "allow_empty" in failure["detail"]
    table = _table(service)
    assert table["revision"] == 1
    assert [(s["run_id"], s["row_count"]) for s in table["snapshots"]] == [("station-a", 22)]
    result = _select_all(service)
    assert result["columns"] == ["stationName", "pm10"]
    assert len(result["rows"]) == 22
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/build", "post", 409)
    assert schema is not None
    assert validate(cast(JsonValue, body), schema, contract) == []


def test_a_gold_filter_that_keeps_no_rows_is_refused_too(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    filters = (
        "    gold:\n      filters:\n        - column: stationName\n          op: eq\n"
        "          value: {station}\n"
    )
    assert service.build(_spec(gold=filters.format(station="A")), run_id="r1").status_code == 200

    response = service.build(_spec(gold=filters.format(station="Z")), run_id="r2")

    assert response.status_code == 409, response.body
    assert cast(dict[str, Any], response.body)["warehouse_failures"]["m"]["reason"] == (
        "empty_result"
    )
    assert _table(service)["revision"] == 1


def test_a_source_that_allows_empty_commits_it_with_the_columns_it_had(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert service.build(_spec(allow_empty=True), run_id="station-a").status_code == 200

    source.items = []
    response = service.build(_spec(allow_empty=True), run_id="quiet-day")

    assert response.status_code == 200, response.body
    table = _table(service)
    assert table["revision"] == 2
    assert table["snapshots"][0]["run_id"] == "quiet-day"
    assert table["snapshots"][0]["row_count"] == 0
    result = _select_all(service)
    assert result["rows"] == []
    assert result["columns"] == ["stationName", "pm10"]
    assert [(c["name"], c["logical_type"]) for c in result["column_meta"]] == [
        ("stationName", "string"),
        ("pm10", "int64"),
    ]


def test_a_first_build_of_no_rows_is_committed(tmp_path: Path) -> None:
    source = _Source()
    source.items = []
    service = _service(tmp_path, source)

    response = service.build(_spec(), run_id="r1")

    assert response.status_code == 200, response.body
    assert _table(service)["revision"] == 1


def test_a_build_with_rows_still_replaces_the_table(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="r1").status_code == 200

    source.items = [{"stationName": "B", "pm10": 5}]
    response = service.build(_spec(), run_id="r2")

    assert response.status_code == 200, response.body
    assert _table(service)["revision"] == 2


def test_allow_empty_is_part_of_the_recipe_only_when_declared() -> None:
    def mapping(text: str) -> dict[str, JsonValue]:
        return canonical_spec_mapping(parse_spec(cast(dict[str, object], yaml.safe_load(text))))

    plain = mapping(_spec())
    declared = mapping(_spec(allow_empty=True))
    off = mapping(_spec(allow_empty=False))

    (plain_source,) = cast(list[dict[str, JsonValue]], plain["sources"])
    (declared_source,) = cast(list[dict[str, JsonValue]], declared["sources"])
    assert "allow_empty" not in plain_source
    assert declared_source["allow_empty"] is True
    # Writing the default out does not change the digest of an existing spec.
    assert off == plain


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_allow_empty_must_be_a_boolean(value: object) -> None:
    data = yaml.safe_load(_spec())
    data["sources"][0]["allow_empty"] = value

    with pytest.raises(SpecLoadError, match="allow_empty must be true or false"):
        parse_spec(data)


def test_the_refusal_is_a_failed_run_in_the_list(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="r1").status_code == 200
    source.items = []
    assert service.build(_spec(), run_id="r2").status_code == 409

    response = service.list_builds()

    assert isinstance(response, ServiceResponse)
    statuses = {
        b["run_id"]: b["status"] for b in cast(list[dict[str, Any]], response.body["builds"])
    }
    assert statuses == {"r1": "ok", "r2": "failed"}

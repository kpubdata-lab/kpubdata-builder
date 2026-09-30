"""GET /builds names each run's table and snapshot (#844).

Studio's Refresh History showed ``—`` for Table and Snapshot: ``BuildSummary`` had only
run id, status and times, and tracing each run back was one call per run.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import gc as warehouse_gc

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


class _Result:
    def __init__(self) -> None:
        self.items = [{"id": "1", "pm10": 30}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


def _spec(dataset_id: str, title: str, *, sources: int = 1) -> str:
    body = f"dataset_id: {dataset_id}\ntitle: {title}\ndescription: d\nsources:\n"
    for index in range(sources):
        body += f"  - provider: datago\n    dataset: air_quality\n    alias: s{index}\n"
    return body + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"


def _service(tmp_path: Path, *, warehouse: bool = True) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: _Client(),
        warehouse_root=tmp_path / "wh" if warehouse else None,
    )


def _builds(service: BuilderService, query: str = "") -> dict[str, dict[str, JsonValue]]:
    response = dispatch(service, "GET", "/builds", None, query)
    assert isinstance(response, ServiceResponse)
    assert response.status_code == 200, response.body
    return {
        cast(str, b["run_id"]): b for b in cast(list[dict[str, JsonValue]], response.body["builds"])
    }


def test_each_run_names_its_dataset_and_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    built = service.build(_spec("seoul.air", "Seoul air"), run_id="r1")
    (committed,) = cast(dict[str, dict[str, JsonValue]], built.body["materialized"]).values()

    run = _builds(service)["r1"]

    assert (run["dataset_id"], run["dataset_title"]) == ("seoul.air", "Seoul air")
    assert run["snapshot_id"] == committed["snapshot_id"]
    assert run["snapshots"] == [
        {"logical_name": committed["logical_name"], "snapshot_id": committed["snapshot_id"]}
    ]


def test_several_tables_list_every_snapshot_and_no_single_id(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.build(_spec("multi", "Multi", sources=2), run_id="m1").status_code == 200

    run = _builds(service)["m1"]

    assert run["snapshot_id"] is None
    assert len(cast(list[JsonValue], run["snapshots"])) == 2


def test_nothing_committed_is_null_not_guessed(tmp_path: Path) -> None:
    """Negative: without a warehouse a run has no snapshot, and says so."""
    service = _service(tmp_path, warehouse=False)
    assert service.build(_spec("plain", "Plain"), run_id="p1").status_code == 200

    run = _builds(service)["p1"]

    assert (run["snapshot_id"], run["snapshots"]) == (None, [])
    assert run["dataset_id"] == "plain"


def test_a_reclaimed_snapshot_is_no_longer_named(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.build(_spec("air", "Air"), run_id="old").status_code == 200
    assert service.build(_spec("air", "Air"), run_id="new").status_code == 200
    catalog = service._table_catalog()
    assert catalog is not None
    warehouse_gc.collect(catalog, catalog.list_tables()[0].id, keep=1)

    builds = _builds(service)

    assert builds["old"]["snapshot_id"] is None
    assert builds["new"]["snapshot_id"] is not None


def test_the_dataset_filter_keeps_one_datasets_runs(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for run_id, dataset in (("a1", "alpha"), ("b1", "beta"), ("a2", "alpha")):
        assert service.build(_spec(dataset, dataset), run_id=run_id).status_code == 200

    assert set(_builds(service, "dataset_id=alpha")) == {"a1", "a2"}
    assert set(_builds(service, "dataset_id=alpha&limit=1")) <= {"a1", "a2"}
    assert len(_builds(service, "dataset_id=alpha&limit=1")) == 1
    assert _builds(service, "dataset_id=nothing") == {}


def test_an_empty_filter_is_refused(tmp_path: Path) -> None:
    response = dispatch(_service(tmp_path), "GET", "/builds", None, "dataset_id=")

    assert isinstance(response, ServiceResponse)
    assert response.status_code == 400


@pytest.mark.parametrize("warehouse", [True, False])
def test_the_list_matches_the_contract(tmp_path: Path, warehouse: bool) -> None:
    service = _service(tmp_path, warehouse=warehouse)
    assert service.build(_spec("air", "Air"), run_id="r1").status_code == 200
    response = dispatch(service, "GET", "/builds", None)
    assert isinstance(response, ServiceResponse)

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/builds", "get", 200)
    assert schema is not None
    assert validate(response.body, schema, contract) == []

"""GET /warehouse/tables summarises each table's current snapshot (#841).

Studio's Tables list called ``GET /warehouse/tables/{name}`` once per table for the row
count and commit time, and split ``logical_name`` to join it with ``GET /datasets``.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import polars as pl
import yaml

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import materialize

from ._openapi import response_schema, validate

_DEV = Principal("dev")
_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


class _Result:
    def __init__(self) -> None:
        self.items = [{"id": "1", "pm10": 30}, {"id": "2", "pm10": 50}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


_SPEC = """\
dataset_id: seoul.air.v2
title: Air
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(
        output_root=tmp_path, client_factory=lambda **_: _Client(), warehouse_root=tmp_path / "wh"
    )


def _tables(service: BuilderService) -> list[dict[str, JsonValue]]:
    response = service.list_warehouse_tables(principal=_DEV)
    assert response.status_code == 200
    return cast(list[dict[str, JsonValue]], response.body["tables"])


def test_one_call_gives_each_tables_current_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    built = service.build(_SPEC, run_id="r1")
    assert built.status_code == 200, built.body
    (committed,) = cast(dict[str, dict[str, JsonValue]], built.body["materialized"]).values()

    (table,) = _tables(service)

    current = cast(dict[str, JsonValue], table["current_snapshot"])
    assert current["snapshot_id"] == committed["snapshot_id"]
    assert current["row_count"] == 2
    assert isinstance(current["committed_at"], str)
    # A dataset id with dots is read from the run's spec, not split out of the name.
    assert table["dataset_id"] == "seoul.air.v2"


def test_a_table_without_a_commit_says_null_not_zero(tmp_path: Path) -> None:
    """Negative: nothing committed means null, never a zero row count."""
    service = _service(tmp_path)
    catalog = service._table_catalog()
    assert catalog is not None
    catalog.create_table(PERSONAL_WORKSPACE, "empty.table")

    (table,) = _tables(service)

    assert table["current_snapshot"] is None
    assert table["dataset_id"] is None


def test_unknown_values_stay_null(tmp_path: Path) -> None:
    """A snapshot committed without a row count or a spec reports null for both."""
    service = _service(tmp_path)
    catalog = service._table_catalog()
    assert catalog is not None
    gold = tmp_path / "gold"
    gold.mkdir()
    pl.DataFrame({"v": [1]}).write_parquet(gold / "table.parquet")
    materialize(
        catalog,
        workspace_id=PERSONAL_WORKSPACE,
        logical_name="no.spec",
        source_dir=gold,
        run_id="no-such-run",
    )

    (table,) = _tables(service)

    current = cast(dict[str, JsonValue], table["current_snapshot"])
    assert current["row_count"] is None
    assert current["coverage"] is None
    assert table["dataset_id"] is None


def test_the_list_matches_the_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.build(_SPEC, run_id="r1").status_code == 200
    catalog = service._table_catalog()
    assert catalog is not None
    catalog.create_table(PERSONAL_WORKSPACE, "empty.table")

    response = service.list_warehouse_tables(principal=_DEV)

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/warehouse/tables", "get", 200)
    assert schema is not None
    assert validate(response.body, schema, contract) == []

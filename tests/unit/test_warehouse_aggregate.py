"""Validated aggregates over a pinned snapshot (#818).

Aggregates used to be user SQL only, which cannot tell an additive column from a rate,
cannot see that rows are counted in different units, and lets a client aggregate the
first N rows or confuse count(*) with count(column). These pin what
`POST /warehouse/aggregate` promises:

- a group that mixes units is refused, or split by unit when asked — never added up;
- every row is aggregated before the top N groups are taken, and the response says how
  many groups there were, so a top-N result is not mistaken for the full one;
- count_rows, count and count_null are different numbers when a column has nulls;
- sum is never assumed, and a sum of nothing is null, not 0;
- a re-run against the same snapshot gives the same answer after a new commit;
- limits refuse instead of cutting the result short;
- another owner's table stays a 404.
"""

from __future__ import annotations

import multiprocessing
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.query import aggregate as aggregate_module
from kpubdata_builder.query import engine as engine_module
from kpubdata_builder.query.aggregate import aggregate_worker
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog, materialize

from ._openapi import response_schema, validate

_NAME = "stats.population"
_DEV = Principal("dev")
_CONTRACT: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)


class _InProcessAggregateEngine:
    """Runs the real aggregate worker in this process, so the tests need no child process."""

    def execute(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        parent, child = multiprocessing.Pipe(duplex=False)
        aggregate_worker(child, str(table_path), plan_json, limit, time.monotonic_ns())
        payload = parent.recv()
        parent.close()
        assert payload["ok"] is True, "the aggregate worker failed"
        return QueryResult(
            columns=tuple(payload["columns"]),
            column_meta=tuple(payload["column_meta"]),
            rows=tuple(payload["rows"]),
            truncated=payload["truncated"],
            execution_ms=0,
            startup_ms=payload["startup_ms"],
            engine_execution_ms=payload["engine_execution_ms"],
            meta=payload["meta"],
        )


def _service(tmp_path: Path, *, real_engine: bool = False) -> BuilderService:
    engine = (
        None if real_engine else QueryService(aggregate_engine=_InProcessAggregateEngine())  # type: ignore[arg-type]
    )
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: None,
        warehouse_root=tmp_path / "wh",
        query_service=engine,
    )


def _catalog(service: BuilderService) -> TableCatalog:
    catalog = service._table_catalog()
    assert catalog is not None
    return catalog


_seq = 0


def _commit(
    service: BuilderService,
    tmp_path: Path,
    frame: pl.DataFrame,
    *,
    workspace: str = PERSONAL_WORKSPACE,
) -> str:
    global _seq
    _seq += 1
    gold = tmp_path / f"gold-{_seq}"
    gold.mkdir()
    frame.write_parquet(gold / "table.parquet")
    result = materialize(
        _catalog(service),
        workspace_id=workspace,
        logical_name=_NAME,
        source_dir=gold,
        run_id=f"run-{_seq}",
        row_count=frame.height,
    )
    return result.snapshot.id


def _aggregate(
    service: BuilderService, principal: Principal = _DEV, **body: Any
) -> ServiceResponse:
    return service.aggregate_warehouse({"table": _NAME, **body}, principal=principal)


def _ok(response: ServiceResponse) -> dict[str, Any]:
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.body)


# Region "b" has the largest total but its rows come last: aggregating the first rows
# and then ranking would pick "a".
_POP = pl.DataFrame(
    {
        "region": ["a", "a", "c", "a", "c", "b", "b", "b"],
        "people": [10, 20, 5, 30, 1, 40, 50, None],
        "unit": ["명"] * 8,
    }
)


# ---------------------------------------------------------------- units


def test_a_group_mixing_units_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(
        service,
        tmp_path,
        pl.DataFrame(
            {
                "region": ["a", "a", "b", "b"],
                "amount": [3, 2000, 4, 5],
                "unit": ["천원", "원", "원", "원"],
            }
        ),
    )
    measure = {"fn": "sum", "column": "amount", "additive": True, "as": "total"}

    refused = _aggregate(service, group_by=["region"], measures=[measure], unit_column="unit")
    split = _ok(
        _aggregate(
            service,
            group_by=["region"],
            measures=[measure],
            unit_column="unit",
            unit_policy="split",
        )
    )
    single = _ok(
        _aggregate(
            service,
            group_by=["region"],
            measures=[measure],
            unit_column="unit",
            filters=[{"column": "unit", "op": "eq", "value": "원"}],
        )
    )

    assert refused.status_code == 422
    assert refused.body["code"] == "mixed_units"
    assert refused.body["mixed_group_count"] == 1
    assert refused.body["samples"] == [{"group": {"region": "a"}, "units": ["원", "천원"]}]
    assert "rows" not in refused.body
    # Split: one row per unit, never 3 + 2000 added up.
    assert split["rows"] == [
        {"region": "a", "unit": "원", "total": 2000},
        {"region": "a", "unit": "천원", "total": 3},
        {"region": "b", "unit": "원", "total": 9},
    ]
    assert split["unit"] == {"column": "unit", "policy": "split", "check": "split"}
    # One unit per group: the unit is sent beside the total.
    assert single["rows"] == [
        {"region": "a", "unit": "원", "total": 2000},
        {"region": "b", "unit": "원", "total": 9},
    ]
    assert single["unit"] == {"column": "unit", "policy": "reject", "check": "single_unit"}


def test_a_missing_unit_beside_a_known_one_is_mixed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, pl.DataFrame({"amount": [1, 2], "unit": ["명", None]}))

    response = _aggregate(service, measures=[{"fn": "avg", "column": "amount"}], unit_column="unit")

    assert (response.status_code, response.body["code"]) == (422, "mixed_units")
    assert response.body["samples"] == [{"group": {}, "units": ["명", None]}]


def test_without_a_unit_column_the_units_are_reported_unchecked(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    body = _ok(_aggregate(service, measures=[{"fn": "max", "column": "people"}]))

    assert body["unit"] == {"column": None, "policy": "reject", "check": "not_checked"}
    assert body["measures"] == [
        {"as": "max_people", "fn": "max", "column": "people", "additive": None, "unit_column": None}
    ]


# ---------------------------------------------------------------- top N


def test_the_top_n_is_taken_after_every_row_is_aggregated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    body = _ok(
        _aggregate(
            service,
            group_by=["region"],
            measures=[{"fn": "sum", "column": "people", "additive": True, "as": "total"}],
            order_by=[{"key": "total", "direction": "desc"}],
            limit=1,
        )
    )
    full = _ok(
        _aggregate(
            service,
            group_by=["region"],
            measures=[{"fn": "sum", "column": "people", "additive": True, "as": "total"}],
            order_by=[{"key": "total", "direction": "desc"}],
        )
    )

    assert body["rows"] == [{"region": "b", "total": 90}]
    assert body["result"] == {
        "completeness": "top_n",
        "group_count": 3,
        "returned": 1,
        "limit": 1,
    }
    assert body["input"] == {"row_count": 8, "sampled": False}
    assert full["rows"] == [
        {"region": "b", "total": 90},
        {"region": "a", "total": 60},
        {"region": "c", "total": 6},
    ]
    assert full["result"]["completeness"] == "full"


def test_groups_that_tie_are_ordered_by_their_keys(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, pl.DataFrame({"k": ["z", "y", "x", None, "y"]}))

    body = _ok(
        _aggregate(
            service,
            group_by=["k"],
            measures=[{"fn": "count_rows", "as": "n"}],
            order_by=[{"key": "n", "direction": "desc"}],
        )
    )

    assert body["rows"] == [
        {"k": "y", "n": 2},
        {"k": "x", "n": 1},
        {"k": "z", "n": 1},
        {"k": None, "n": 1},
    ]


# ---------------------------------------------------------------- counts and sums


def test_counts_of_rows_values_and_nulls_are_distinct(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    body = _ok(
        _aggregate(
            service,
            group_by=["region"],
            measures=[
                {"fn": "count_rows", "as": "rows"},
                {"fn": "count", "column": "people", "as": "values"},
                {"fn": "count_null", "column": "people", "as": "nulls"},
                {"fn": "count_distinct", "column": "people", "as": "distinct"},
            ],
            filters=[{"column": "region", "op": "eq", "value": "b"}],
        )
    )

    assert body["rows"] == [{"region": "b", "rows": 3, "values": 2, "nulls": 1, "distinct": 2}]


def test_sum_is_never_assumed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    response = _aggregate(service, measures=[{"fn": "sum", "column": "people"}])

    assert (response.status_code, response.body["code"]) == (400, "invalid_request")
    assert "additive" in response.body["error"]


def test_a_sum_of_nothing_is_null_not_zero(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(
        service,
        tmp_path,
        pl.DataFrame({"k": ["a", "b"], "v": [None, 2]}, schema={"k": pl.String, "v": pl.Int64}),
    )
    measures = [{"fn": "sum", "column": "v", "additive": True, "as": "total"}]

    grouped = _ok(_aggregate(service, group_by=["k"], measures=measures))
    nothing = _ok(
        _aggregate(
            service,
            measures=[*measures, {"fn": "count_rows", "as": "n"}],
            filters=[{"column": "k", "op": "eq", "value": "zzz"}],
        )
    )

    assert grouped["rows"] == [{"k": "a", "total": None}, {"k": "b", "total": 2}]
    # No group_by is one group over every filtered row, even when there are none.
    assert nothing["rows"] == [{"total": None, "n": 0}]
    assert nothing["input"]["row_count"] == 0


def test_decimal_sums_stay_exact(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(
        service,
        tmp_path,
        pl.DataFrame(
            {"amount": [Decimal("0.10"), Decimal("0.20")]}, schema={"amount": pl.Decimal(10, 2)}
        ),
    )

    body = _ok(_aggregate(service, measures=[{"fn": "sum", "column": "amount", "additive": True}]))

    assert body["rows"] == [{"sum_amount": "0.30"}]
    assert body["column_meta"][0]["wire_encoding"] == "decimal_string"


# ---------------------------------------------------------------- snapshot binding


def test_the_same_snapshot_gives_the_same_aggregate_after_a_new_commit(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old = _commit(service, tmp_path, _POP)
    request: dict[str, Any] = {
        "group_by": ["region"],
        "measures": [
            {"fn": "sum", "column": "people", "additive": True},
            {"fn": "avg", "column": "people"},
        ],
    }

    first = _ok(_aggregate(service, **request))
    _commit(service, tmp_path, _POP.with_columns(pl.col("people") * 100))
    again = _ok(_aggregate(service, snapshot=old, **request))
    current = _ok(_aggregate(service, **request))

    assert first["snapshot"]["snapshot_id"] == old
    assert again["snapshot"]["snapshot_id"] == old
    assert again["rows"] == first["rows"]
    assert current["rows"] != first["rows"]


# ---------------------------------------------------------------- limits


def test_too_many_groups_is_refused_not_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(aggregate_module, "MAX_GROUPS", 2)
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    response = _aggregate(service, group_by=["region"], measures=[{"fn": "count_rows"}])

    assert response.status_code == 422
    assert response.body["code"] == "too_many_groups"
    assert response.body["group_count"] == 3


def test_a_result_too_large_to_send_is_refused_not_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", 10)
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    response = _aggregate(service, group_by=["region"], measures=[{"fn": "count_rows"}])

    assert (response.status_code, response.body["code"]) == (422, "result_too_large")


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "body",
    [
        {"measures": []},
        {"measures": [{"fn": "median", "column": "people"}]},
        {"measures": [{"fn": "count_rows", "column": "people"}]},
        {"measures": [{"fn": "avg", "column": "region"}]},
        {"measures": [{"fn": "sum", "column": "region", "additive": True}]},
        {"measures": [{"fn": "avg", "column": "people", "additive": True}]},
        {"measures": [{"fn": "count", "column": "nope"}]},
        {"measures": [{"fn": "count_rows"}], "group_by": ["nope"]},
        {"measures": [{"fn": "count_rows", "as": "region"}], "group_by": ["region"]},
        {"measures": [{"fn": "count_rows"}, {"fn": "count_rows"}]},
        {"measures": [{"fn": "count_rows"}], "order_by": [{"key": "people"}]},
        {"measures": [{"fn": "count_rows"}], "limit": 0},
        {"measures": [{"fn": "count_rows"}], "limit": 1001},
        {"measures": [{"fn": "count_rows"}], "unit_policy": "split"},
        {"measures": [{"fn": "count_rows"}], "unit_column": "unit", "unit_policy": "convert"},
        {"measures": [{"fn": "count_rows"}], "sql": "SELECT 1"},
        {"measures": [{"fn": "count_rows"}], "group_by": ["a", "b", "c", "d", "e"]},
        {
            "measures": [{"fn": "count_rows"}],
            "filters": [{"column": "people", "op": "eq", "value": "many"}],
        },
    ],
)
def test_invalid_requests_are_400(tmp_path: Path, body: dict[str, Any]) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    response = _aggregate(service, **body)

    assert (response.status_code, response.body["code"]) == (400, "invalid_request")


def test_another_owners_table_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP, workspace=warehouse_workspace("oidc:bob"))

    response = _aggregate(
        service, Principal("oidc", "alice", "oidc:alice"), measures=[{"fn": "count_rows"}]
    )

    assert (response.status_code, response.body["code"]) == (404, "table_not_found")


def test_the_lease_is_released(tmp_path: Path) -> None:
    service = _service(tmp_path)
    snapshot = _commit(service, tmp_path, _POP)

    _ok(_aggregate(service, measures=[{"fn": "count_rows"}]))
    _aggregate(service, measures=[{"fn": "count", "column": "nope"}])

    assert _catalog(service).live_lease_count(snapshot) == 0


# ---------------------------------------------------------------- contract


def test_the_response_conforms_to_the_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _POP)

    response = dispatch(
        service,
        "POST",
        "/warehouse/aggregate",
        {
            "table": _NAME,
            "group_by": ["region"],
            "measures": [
                {"fn": "count_rows", "as": "n"},
                {"fn": "sum", "column": "people", "additive": True, "as": "total"},
            ],
            "unit_column": "unit",
            "order_by": [{"key": "total", "direction": "desc"}],
            "limit": 2,
        },
    )

    schema = response_schema(_CONTRACT, "/warehouse/aggregate", "post", 200)
    assert schema is not None
    assert response.status_code == 200, response.body
    assert validate(cast(JsonValue, response.body), schema, _CONTRACT) == []


def test_the_real_engine_aggregates_in_a_child_process(tmp_path: Path) -> None:
    service = _service(tmp_path, real_engine=True)
    _commit(service, tmp_path, _POP)

    body = _ok(
        _aggregate(
            service,
            group_by=["region"],
            measures=[{"fn": "count", "column": "people", "as": "n"}],
            order_by=[{"key": "n", "direction": "desc"}],
            limit=2,
        )
    )

    assert body["rows"] == [{"region": "a", "n": 3}, {"region": "b", "n": 2}]
    assert body["result"]["completeness"] == "top_n"
    assert body["engine_execution_ms"] >= 0

"""Paged row reads over a pinned snapshot (#815).

`POST /warehouse/query` was the only way to read a committed table, so a table screen had
to assemble SQL to fake pages: ties on the sort key moved rows across page boundaries, and
a count it never computed looked like 0. These pin what `POST /warehouse/rows` promises:

- every page after the first reads the snapshot the first one pinned, whatever is
  committed in between;
- rows that tie on the sort keys are neither repeated nor skipped across pages;
- a count that was not computed is null with status `not_computed`, never 0;
- codes, Decimals, large integers, dates and nulls arrive as they were;
- another owner's table stays a 404.
"""

from __future__ import annotations

import multiprocessing
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.rows import ROW_ORDER_COLUMN, rows_worker
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog, materialize

from ._openapi import response_schema, validate

_NAME = "air.station"
_DEV = Principal("dev")
_CONTRACT: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)


class _InProcessRowsEngine:
    """Runs the real rows worker in this process, so the tests need no child process."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        self.calls += 1
        parent, child = multiprocessing.Pipe(duplex=False)
        rows_worker(child, str(table_path), plan_json, limit, time.monotonic_ns())
        payload = parent.recv()
        parent.close()
        assert payload["ok"] is True, "the rows worker failed"
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
    engine = None if real_engine else QueryService(rows_engine=_InProcessRowsEngine())  # type: ignore[arg-type]
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


def _rows(service: BuilderService, principal: Principal = _DEV, **body: Any) -> ServiceResponse:
    return service.read_warehouse_rows({"table": _NAME, **body}, principal=principal)


def _ok(response: ServiceResponse) -> dict[str, Any]:
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.body)


def _all_pages(service: BuilderService, **body: Any) -> tuple[list[dict[str, Any]], str]:
    first = _ok(_rows(service, **body))
    snapshot = first["snapshot"]["snapshot_id"]
    rows = list(first["rows"])
    page = first["page"]
    while page["has_more"]:
        nxt = _ok(_rows(service, **{**body, "snapshot": snapshot, "offset": page["next_offset"]}))
        assert nxt["snapshot"]["snapshot_id"] == snapshot
        rows.extend(nxt["rows"])
        page = nxt["page"]
    return rows, snapshot


# ---------------------------------------------------------------- snapshot binding


def test_later_pages_read_the_pinned_snapshot_after_a_new_commit(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old = _commit(service, tmp_path, pl.DataFrame({"id": [1, 2, 3, 4]}))

    first = _ok(_rows(service, page_size=2))
    assert first["snapshot"]["snapshot_id"] == old
    new = _commit(service, tmp_path, pl.DataFrame({"id": [100, 200, 300, 400]}))

    second = _ok(_rows(service, page_size=2, snapshot=old, offset=first["page"]["next_offset"]))
    current = _ok(_rows(service, page_size=2))

    assert [r["id"] for r in first["rows"] + second["rows"]] == [1, 2, 3, 4]
    assert second["snapshot"]["snapshot_id"] == old
    assert current["snapshot"]["snapshot_id"] == new
    assert second["page"] == {
        "offset": 2,
        "page_size": 2,
        "returned": 2,
        "has_more": False,
        "next_offset": None,
    }


# ---------------------------------------------------------------- stable order


_TIES = pl.DataFrame(
    {
        "id": list(range(9)),
        "k": [1, 1, 1, 2, 2, 1, None, 1, None],
    }
)


@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("page_size", [1, 2, 4])
def test_ties_are_neither_repeated_nor_skipped_across_pages(
    tmp_path: Path, direction: str, page_size: int
) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)
    sort = [{"column": "k", "direction": direction}]

    rows, _ = _all_pages(service, page_size=page_size, sort=sort)
    again, _ = _all_pages(service, page_size=page_size, sort=sort)

    ids = [r["id"] for r in rows]
    assert sorted(ids) == list(range(9))  # no row repeated, none skipped
    assert ids == [r["id"] for r in again]  # the same order every time
    ones, twos = [0, 1, 2, 5, 7], [3, 4]
    # Ties keep snapshot order; nulls come last in either direction.
    assert ids == (ones + twos if direction == "asc" else twos + ones) + [6, 8]


def test_the_tie_breaker_is_not_sent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)

    body = _ok(_rows(service, sort=[{"column": "k"}]))

    assert ROW_ORDER_COLUMN not in body["columns"]
    assert all(ROW_ORDER_COLUMN not in row for row in body["rows"])
    assert body["order"] == [{"column": "k", "direction": "asc"}]


def test_a_table_with_the_reserved_column_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, pl.DataFrame({ROW_ORDER_COLUMN: [1]}))

    response = _rows(service)

    assert (response.status_code, response.body["code"]) == (400, "invalid_request")


# ---------------------------------------------------------------- count


def test_a_count_not_computed_is_null_not_zero(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)
    filtered = [{"column": "k", "op": "eq", "value": 1}]

    skipped = _ok(_rows(service, filters=filtered))
    counted = _ok(_rows(service, filters=filtered, count="exact"))
    none_match = _ok(
        _rows(service, filters=[{"column": "k", "op": "eq", "value": 9}], count="exact")
    )
    unfiltered = _ok(_rows(service, count="none"))

    assert skipped["count"] == {"status": "not_computed", "value": None}
    assert counted["count"] == {"status": "exact", "value": 5}
    assert none_match["count"] == {"status": "exact", "value": 0}
    # Without a filter the snapshot's own row count is the answer, and it is free.
    assert unfiltered["count"] == {"status": "exact", "value": 9}


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ([{"column": "k", "op": "is_null"}], [6, 8]),
        ([{"column": "k", "op": "is_not_null"}, {"column": "k", "op": "gt", "value": 1}], [3, 4]),
        ([{"column": "k", "op": "in", "values": [2]}], [3, 4]),
        ([{"column": "k", "op": "ne", "value": 1}], [3, 4]),
        ([{"column": "id", "op": "lte", "value": 1}], [0, 1]),
    ],
)
def test_filters(tmp_path: Path, filters: list[dict[str, Any]], expected: list[int]) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)

    body = _ok(_rows(service, filters=filters, count="exact"))

    assert [r["id"] for r in body["rows"]] == expected
    assert body["count"]["value"] == len(expected)


# ---------------------------------------------------------------- values


def test_codes_decimals_dates_and_nulls_are_preserved(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(
        service,
        tmp_path,
        pl.DataFrame(
            {
                "code": ["0011010100", None, "4113510900"],
                "amount": [Decimal("12.50"), None, Decimal("0.10")],
                "big": [9007199254740993, None, 1],
                "day": [date(2025, 1, 2), None, date(2024, 12, 31)],
            },
            schema={
                "code": pl.String,
                "amount": pl.Decimal(10, 2),
                "big": pl.Int64,
                "day": pl.Date,
            },
        ),
    )

    body = _ok(_rows(service))
    filtered = _ok(
        _rows(
            service,
            columns=["day", "code"],
            filters=[
                {"column": "amount", "op": "eq", "value": "12.50"},
                {"column": "day", "op": "gte", "value": "2025-01-01"},
                {"column": "code", "op": "in", "values": ["0011010100"]},
            ],
            count="exact",
        )
    )

    assert body["rows"] == [
        {"code": "0011010100", "amount": "12.50", "big": "9007199254740993", "day": "2025-01-02"},
        {"code": None, "amount": None, "big": None, "day": None},
        {"code": "4113510900", "amount": "0.10", "big": "1", "day": "2024-12-31"},
    ]
    assert {c["name"]: c["wire_encoding"] for c in body["column_meta"]} == {
        "code": "string",
        "amount": "decimal_string",
        "big": "decimal_string",
        "day": "string",
    }
    assert filtered["columns"] == ["day", "code"]
    assert filtered["rows"] == [{"day": "2025-01-02", "code": "0011010100"}]


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    "body",
    [
        {"columns": ["nope"]},
        {"sort": [{"column": "nope"}]},
        {"filters": [{"column": "k", "op": "eq", "value": "not a number"}]},
        {"filters": [{"column": "k", "op": "eq"}]},
        {"filters": [{"column": "k", "op": "like", "value": 1}]},
        {"page_size": 0},
        {"page_size": 501},
        {"offset": -1},
        {"sql": "SELECT 1"},
        {"count": "estimated"},
    ],
)
def test_invalid_requests_are_400(tmp_path: Path, body: dict[str, Any]) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)

    response = _rows(service, **body)

    assert (response.status_code, response.body["code"]) == (400, "invalid_request")


def test_another_owners_table_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES, workspace=warehouse_workspace("oidc:bob"))

    response = _rows(service, Principal("oidc", "alice", "oidc:alice"))

    assert (response.status_code, response.body["code"]) == (404, "table_not_found")


def test_a_snapshot_of_another_table_is_not_readable(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)

    response = _rows(service, snapshot="snap_not_this_tables")

    assert (response.status_code, response.body["code"]) == (404, "snapshot_not_found")


def test_the_lease_is_released_after_each_page(tmp_path: Path) -> None:
    service = _service(tmp_path)
    snapshot = _commit(service, tmp_path, _TIES)

    _ok(_rows(service, page_size=2))
    _rows(service, columns=["nope"])

    assert _catalog(service).live_lease_count(snapshot) == 0


# ---------------------------------------------------------------- contract


def test_the_response_conforms_to_the_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _TIES)

    response = dispatch(
        service,
        "POST",
        "/warehouse/rows",
        {"table": _NAME, "page_size": 3, "sort": [{"column": "k", "direction": "desc"}]},
    )

    schema = response_schema(_CONTRACT, "/warehouse/rows", "post", 200)
    assert schema is not None
    assert response.status_code == 200, response.body
    assert validate(cast(JsonValue, response.body), schema, _CONTRACT) == []


def test_the_real_engine_reads_a_page_in_a_child_process(tmp_path: Path) -> None:
    service = _service(tmp_path, real_engine=True)
    _commit(service, tmp_path, _TIES)

    body = _ok(_rows(service, page_size=3, sort=[{"column": "k"}], count="exact"))

    assert [r["id"] for r in body["rows"]] == [0, 1, 2]
    assert body["page"]["next_offset"] == 3
    assert body["count"] == {"status": "exact", "value": 9}
    assert body["engine_execution_ms"] >= 0

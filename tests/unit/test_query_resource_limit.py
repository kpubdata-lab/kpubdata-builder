"""A query over the deployment's memory or spill limit says so (#961).

Every query-path worker (SQL, rows, aggregate, profile, export) runs in a child process
that sends only ``ok: False`` on failure, so DuckDB's text — sizes, the spill directory —
never reaches a client. A memory or spill-quota failure now also sends the fixed reason
``resource_limit``; the parent raises ``QueryResourceLimitError`` with the same
``RESOURCE_LIMIT_MESSAGE`` a build or preview gives, and every route answers 400
``query_resource_limit`` instead of the ``query_execution_failed`` a syntax error gets.
"""

from __future__ import annotations

from multiprocessing import Pipe
from pathlib import Path
from typing import Any, NoReturn, cast

import duckdb
import polars as pl
import pytest

from kpubdata_builder.query import aggregate, export, profile, rows
from kpubdata_builder.query.engine import (
    QueryEngine,
    QueryExecutionError,
    QueryResourceLimitError,
    _query_worker,
    failure_payload,
)
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE
from kpubdata_builder.service.query_service_api import execute_query
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.duckdb_runtime import (
    MAX_TEMP_SIZE_ENV,
    MEMORY_LIMIT_ENV,
    RESOURCE_LIMIT_MESSAGE,
    THREADS_ENV,
    ResourceLimitError,
)
from kpubdata_builder.warehouse import materialize
from tests.support.polars_bridge import handle_from_frame

_DEV = Principal("dev")
_NAME = "air.station"
#: A sort of four million rows: past 64MB of buffer memory it spills, past 4MB of spill
#: it fails. Every relation is ``dataset``, so the validator lets it through.
_SPILLING_SQL = (
    "SELECT max(rn) AS m FROM (SELECT row_number() OVER "
    "(ORDER BY md5(CAST(a.v AS VARCHAR) || '-' || CAST(b.v AS VARCHAR))) AS rn "
    "FROM dataset AS a CROSS JOIN dataset AS b) AS t"
)


@pytest.fixture
def tight_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    # The child process is spawned with this environment and opens its sandbox from it.
    monkeypatch.setenv(MEMORY_LIMIT_ENV, "64MB")
    monkeypatch.setenv(MAX_TEMP_SIZE_ENV, "4MB")
    monkeypatch.setenv(THREADS_ENV, "1")


@pytest.fixture
def table(tmp_path: Path) -> Path:
    handle = handle_from_frame(pl.DataFrame({"v": list(range(2000))}), workdir=tmp_path / "w")
    path = tmp_path / "table.parquet"
    handle.write_parquet(path)
    handle.close()
    return path


# ----------------------------------------------------------------- the child's message


@pytest.mark.parametrize(
    "error",
    [
        duckdb.OutOfMemoryException("could not allocate 1.2 GiB in /srv/data/.tmp/spill"),
        ResourceLimitError(RESOURCE_LIMIT_MESSAGE),
        MemoryError(),
    ],
)
def test_a_limit_is_sent_as_the_fixed_reason_only(error: BaseException) -> None:
    assert failure_payload(error) == {"ok": False, "reason": "resource_limit"}


@pytest.mark.parametrize(
    "error",
    [duckdb.BinderException("column x not found in /srv/data/t.parquet"), ValueError("bad")],
)
def test_any_other_failure_is_sent_without_a_reason(error: BaseException) -> None:
    assert failure_payload(error) == {"ok": False}


def _out_of_memory(*_: object, **__: object) -> NoReturn:
    raise duckdb.OutOfMemoryException("failed to offload data block of size 256 KiB (/tmp/x)")


@pytest.mark.parametrize(
    ("worker", "owner", "attribute"),
    [
        (rows.rows_worker, rows.RowsPlan, "from_json"),
        (aggregate.aggregate_worker, aggregate.AggregatePlan, "from_json"),
        (profile.profile_worker, profile.ProfilePlan, "from_json"),
        (export.export_worker, export.ExportPlan, "from_json"),
    ],
)
def test_every_worker_sends_the_reason_and_nothing_of_duckdb(
    worker: Any, owner: object, attribute: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(owner, attribute, _out_of_memory)
    receive, send = Pipe(duplex=False)

    worker(send, "/srv/data/t.parquet", "{}", 10, 0)

    assert receive.recv() == {"ok": False, "reason": "resource_limit"}


# --------------------------------------------------------------- the real child process


def test_a_query_past_the_spill_quota_raises_the_limit_error(
    table: Path, tight_limits: None
) -> None:
    from kpubdata_builder.query.security import validate_read_only_sql

    canonical = validate_read_only_sql(_SPILLING_SQL).canonical_sql

    with pytest.raises(QueryResourceLimitError) as raised:
        QueryEngine(timeout_seconds=120, worker=_query_worker).execute(table, canonical, limit=1)

    assert str(raised.value) == RESOURCE_LIMIT_MESSAGE
    assert isinstance(raised.value, QueryExecutionError)  # existing handlers still catch it


def test_the_query_route_answers_query_resource_limit(table: Path, tight_limits: None) -> None:
    response = execute_query(
        cast(QueryService, QueryEngine(timeout_seconds=120)), table, _SPILLING_SQL, limit=1
    )

    assert response.status_code == 400
    assert response.body == {"error": RESOURCE_LIMIT_MESSAGE, "code": "query_resource_limit"}


def test_the_same_query_within_the_limits_still_answers(table: Path) -> None:
    # Without the tight limits the same SQL runs: the code is about the deployment.
    response = execute_query(
        cast(QueryService, QueryEngine(timeout_seconds=120)), table, _SPILLING_SQL, limit=1
    )

    assert response.status_code == 200, response.body
    assert response.body["rows"] == [{"m": 4_000_000}]


def test_a_failing_query_is_still_query_execution_failed(table: Path, tight_limits: None) -> None:
    response = execute_query(
        cast(QueryService, QueryEngine(timeout_seconds=120)),
        table,
        "SELECT CAST('x' AS INTEGER) AS n FROM dataset",
        limit=1,
    )

    assert response.status_code == 400
    assert response.body["code"] == "query_execution_failed"


# --------------------------------------------------------------------- every route


class _OverLimit(QueryService):
    def _raise(self, *_: object, **__: object) -> NoReturn:
        raise QueryResourceLimitError(RESOURCE_LIMIT_MESSAGE)

    execute = execute_rows = execute_aggregate = execute_profile = execute_export = _raise


def _no_client(**_: object) -> NoReturn:
    raise AssertionError("no provider client")


def test_every_warehouse_route_answers_query_resource_limit(tmp_path: Path) -> None:
    service = BuilderService(
        output_root=tmp_path,
        client_factory=_no_client,
        warehouse_root=tmp_path / "wh",
        query_service=_OverLimit(),
    )
    catalog = service._table_catalog()
    assert catalog is not None
    gold = tmp_path / "gold"
    gold.mkdir()
    pl.DataFrame({"v": list(range(20))}).write_parquet(gold / "table.parquet")
    materialize(
        catalog, workspace_id=PERSONAL_WORKSPACE, logical_name=_NAME, source_dir=gold, run_id="r"
    )
    query: dict[str, JsonValue] = {"table": _NAME, "sql": "SELECT * FROM dataset"}

    responses: dict[str, ServiceResponse] = {
        "query": service.query_warehouse(query, principal=_DEV),
        "rows": service.read_warehouse_rows({"table": _NAME}, principal=_DEV),
        "aggregate": service.aggregate_warehouse(
            {"table": _NAME, "measures": [{"fn": "count_rows", "as": "n"}]}, principal=_DEV
        ),
        "export": service.create_warehouse_export(query, principal=_DEV),
        "profile": service.get_warehouse_profile(_NAME, "current", principal=_DEV),
    }

    assert {route: (r.status_code, r.body.get("code")) for route, r in responses.items()} == {
        route: (400, "query_resource_limit") for route in responses
    }
    assert {r.body["error"] for r in responses.values()} == {RESOURCE_LIMIT_MESSAGE}

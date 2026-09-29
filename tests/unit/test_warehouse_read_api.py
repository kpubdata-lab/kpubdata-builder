"""Committed warehouse tables are readable through the API, pinned for the query (#797).

Builds committed snapshots but nothing read them: ``resolve_current`` and ``pin`` had no
caller outside ``warehouse/``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, cast

import polars as pl
import pytest

from kpubdata_builder.cli import main as cli_main
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog, materialize

_NAME = "air.station"
_DEV = Principal("dev")


def _commit(catalog: TableCatalog, tmp_path: Path, values: list[int], *, workspace: str) -> str:
    gold = tmp_path / f"gold-{workspace}-{len(catalog.list_tables())}-{values[0]}"
    gold.mkdir()
    pl.DataFrame({"v": values}).write_parquet(gold / "table.parquet")
    result = materialize(
        catalog,
        workspace_id=workspace,
        logical_name=_NAME,
        source_dir=gold,
        run_id=f"run-{values[0]}",
        row_count=len(values),
    )
    return result.snapshot.id


def _no_client(**_: object) -> NoReturn:
    raise AssertionError("reading the warehouse must not open a provider client")


def _service(tmp_path: Path, engine: QueryService | None = None) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=_no_client,
        warehouse_root=tmp_path / "wh",
        query_service=engine,
    )


def _catalog(service: BuilderService) -> TableCatalog:
    catalog = service._table_catalog()
    assert catalog is not None
    return catalog


def _body(response: ServiceResponse) -> dict[str, JsonValue]:
    return response.body


class _RecordingEngine:
    """Stands in for the query engine; runs ``during`` while the query is "running"."""

    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.during: list[Callable[[], object]] = []

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del canonical_sql, limit
        self.paths.append(table_path)
        for step in self.during:
            step()
        values = pl.read_parquet(table_path)["v"].to_list()
        return QueryResult(
            columns=("v",),
            column_meta=({"name": "v", "logical_type": "int64", "wire_encoding": "number"},),
            rows=tuple({"v": v} for v in values),
            truncated=False,
            execution_ms=1,
            startup_ms=0,
            engine_execution_ms=1,
        )


def _recording_service(tmp_path: Path) -> tuple[BuilderService, _RecordingEngine]:
    engine = _RecordingEngine()
    return _service(tmp_path, QueryService(engine=engine)), engine  # type: ignore[arg-type]


def _query(service: BuilderService, **body: JsonValue) -> ServiceResponse:
    return service.query_warehouse({"sql": "SELECT * FROM dataset", **body}, principal=_DEV)


def test_list_and_detail_show_the_committed_table(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = _commit(_catalog(service), tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    second = _commit(_catalog(service), tmp_path, [2, 3], workspace=PERSONAL_WORKSPACE)

    (listed,) = cast(
        list[dict[str, JsonValue]], _body(service.list_warehouse_tables(principal=_DEV))["tables"]
    )
    detail = _body(service.get_warehouse_table(_NAME, principal=_DEV))

    assert (listed["logical_name"], listed["current_snapshot_id"]) == (_NAME, second)
    snapshots = cast(list[dict[str, JsonValue]], detail["snapshots"])
    assert [s["snapshot_id"] for s in snapshots] == [second, first]
    assert snapshots[0]["row_count"] == 2


def test_the_real_engine_reads_the_current_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    current = _commit(_catalog(service), tmp_path, [5, 6, 7], workspace=PERSONAL_WORKSPACE)

    response = service.query_warehouse(
        {"table": _NAME, "sql": "SELECT COUNT(*) AS n FROM dataset"}, principal=_DEV
    )

    assert response.status_code == 200, response.body
    body = _body(response)
    assert cast(dict[str, JsonValue], body["snapshot"])["snapshot_id"] == current
    assert cast(dict[str, JsonValue], body["result"])["rows"] == [{"n": 3}]


def test_a_commit_during_the_query_does_not_change_what_it_reads(tmp_path: Path) -> None:
    service, engine = _recording_service(tmp_path)
    catalog = _catalog(service)
    pinned = _commit(catalog, tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    engine.during.append(lambda: _commit(catalog, tmp_path, [9, 9], workspace=PERSONAL_WORKSPACE))

    body = _body(_query(service, table=_NAME))

    assert cast(dict[str, JsonValue], body["snapshot"])["snapshot_id"] == pinned
    assert cast(dict[str, JsonValue], body["result"])["rows"] == [{"v": 1}]
    assert pinned in str(engine.paths[0])
    assert catalog.get_table(catalog.list_tables()[0].id).current_snapshot_id != pinned


def test_the_lease_is_held_while_querying_and_released_after(tmp_path: Path) -> None:
    service, engine = _recording_service(tmp_path)
    catalog = _catalog(service)
    snapshot = _commit(catalog, tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    seen: list[int] = []
    engine.during.append(lambda: seen.append(catalog.live_lease_count(snapshot)))

    assert _query(service, table=_NAME).status_code == 200

    assert seen == [1]
    assert catalog.live_lease_count(snapshot) == 0


def test_a_named_snapshot_reads_the_past_after_a_refresh(tmp_path: Path) -> None:
    service, _ = _recording_service(tmp_path)
    old = _commit(_catalog(service), tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    _commit(_catalog(service), tmp_path, [2], workspace=PERSONAL_WORKSPACE)

    body = _body(_query(service, table=_NAME, snapshot=old))

    assert cast(dict[str, JsonValue], body["result"])["rows"] == [{"v": 1}]


def test_a_snapshot_of_another_table_is_not_readable_through_this_one(tmp_path: Path) -> None:
    """Negative: a snapshot id is honoured only for the table it belongs to."""
    service, engine = _recording_service(tmp_path)
    catalog = _catalog(service)
    _commit(catalog, tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    gold = tmp_path / "other"
    gold.mkdir()
    pl.DataFrame({"v": [42]}).write_parquet(gold / "table.parquet")
    foreign = materialize(
        catalog, workspace_id="ws_other", logical_name="secret.t", source_dir=gold, run_id="x"
    ).snapshot.id

    response = _query(service, table=_NAME, snapshot=foreign)

    assert (response.status_code, _body(response)["code"]) == (404, "snapshot_not_found")
    assert engine.paths == []


def test_another_owners_table_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative: under ownership, Bob's table is a 404 for Alice, not a 403."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    alice = Principal("oidc", "alice", "oidc:alice")
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [1], workspace=warehouse_workspace("oidc:bob"))

    listed = service.list_warehouse_tables(principal=alice)
    detail = service.get_warehouse_table(_NAME, principal=alice)
    queried = service.query_warehouse(
        {"table": _NAME, "sql": "SELECT * FROM dataset"}, principal=alice
    )

    assert _body(listed)["tables"] == []
    assert (detail.status_code, _body(detail)["code"]) == (404, "table_not_found")
    assert (queried.status_code, _body(queried)["code"]) == (404, "table_not_found")


def test_a_retiring_snapshot_is_refused(tmp_path: Path) -> None:
    service, engine = _recording_service(tmp_path)
    catalog = _catalog(service)
    old = _commit(catalog, tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    _commit(catalog, tmp_path, [2], workspace=PERSONAL_WORKSPACE)
    catalog.begin_retiring(old)

    response = _query(service, table=_NAME, snapshot=old)

    assert (response.status_code, _body(response)["code"]) == (409, "snapshot_unavailable")
    assert engine.paths == []


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"table": _NAME, "sql": "SELECT 1", "path": "/etc"}, "invalid_request"),
        ({"sql": "SELECT 1"}, "invalid_request"),
        ({"table": _NAME, "sql": "DROP TABLE dataset"}, "unsafe_query"),
        ({"table": _NAME, "sql": "SELECT * FROM read_parquet('/etc/x')"}, "unsafe_query"),
        ({"table": _NAME, "sql": "SELECT 1", "limit": 501}, "invalid_request"),
    ],
    ids=["unknown-field", "no-table", "write", "file-scan", "limit"],
)
def test_bad_requests_are_refused_before_the_engine_runs(
    tmp_path: Path, body: dict[str, JsonValue], code: str
) -> None:
    service, engine = _recording_service(tmp_path)
    _commit(_catalog(service), tmp_path, [1], workspace=PERSONAL_WORKSPACE)

    response = service.query_warehouse(body, principal=_DEV)

    assert (response.status_code, _body(response)["code"]) == (400, code)
    assert engine.paths == []


def test_without_a_warehouse_every_route_says_so(tmp_path: Path) -> None:
    service = BuilderService(output_root=tmp_path, client_factory=_no_client)

    for response in (
        service.list_warehouse_tables(principal=_DEV),
        service.get_warehouse_table(_NAME, principal=_DEV),
        service.query_warehouse({"table": _NAME, "sql": "SELECT 1"}, principal=_DEV),
    ):
        assert (response.status_code, _body(response)["code"]) == (
            404,
            "warehouse_not_configured",
        )


def test_a_table_with_nothing_committed_is_a_404(tmp_path: Path) -> None:
    service, _ = _recording_service(tmp_path)
    _catalog(service).create_table(PERSONAL_WORKSPACE, _NAME)

    response = _query(service, table=_NAME)

    assert (response.status_code, _body(response)["code"]) == (404, "snapshot_not_found")


def test_holds_have_a_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    catalog = TableCatalog(tmp_path)
    snapshot = _commit(catalog, tmp_path, [1], workspace=PERSONAL_WORKSPACE)
    root = str(tmp_path)

    assert (
        cli_main(
            ["warehouse-hold", root, "place", snapshot, "--kind", "audit", "--reason", "2026 audit"]
        )
        == 0
    )
    hold_id = capsys.readouterr().out.strip()
    assert cli_main(["warehouse-hold", root, "list", snapshot]) == 0
    assert "2026 audit" in capsys.readouterr().out
    assert [h.hold_id for h in catalog.live_holds(snapshot)] == [hold_id]

    assert cli_main(["warehouse-hold", root, "release", hold_id]) == 0
    assert catalog.live_holds(snapshot) == []


def test_the_hold_cli_refuses_an_unknown_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    TableCatalog(tmp_path)

    code = cli_main(
        ["warehouse-hold", str(tmp_path), "place", "nope", "--kind", "audit", "--reason", "r"]
    )

    assert code == 1
    assert "no such snapshot" in capsys.readouterr().err


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
dataset_id: e2e.air
title: E2E
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def test_a_build_is_queryable_by_its_table_name(tmp_path: Path) -> None:
    """End to end: what ``POST /build`` reports as materialised is what the API reads."""
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: _Client(),
        warehouse_root=tmp_path / "wh",
    )
    built = service.build(_SPEC, run_id="r1")
    assert built.status_code == 200, built.body
    (committed,) = cast(dict[str, dict[str, JsonValue]], _body(built)["materialized"]).values()

    response = service.query_warehouse(
        {"table": cast(str, committed["logical_name"]), "sql": "SELECT COUNT(*) AS n FROM dataset"},
        principal=_DEV,
    )

    assert response.status_code == 200, response.body
    snapshot = cast(dict[str, JsonValue], _body(response)["snapshot"])
    assert snapshot["snapshot_id"] == committed["snapshot_id"]
    assert cast(dict[str, JsonValue], _body(response)["result"])["rows"] == [{"n": 2}]

"""Saved analyses are stored with the snapshot they read and re-run against it (#783)."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn, cast

import polars as pl
import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog, materialize
from kpubdata_builder.warehouse import gc as warehouse_gc

_NAME = "air.station"
_DEV = Principal("dev")
_SQL = "SELECT SUM(v) AS total FROM dataset"


def _no_client(**_: object) -> NoReturn:
    raise AssertionError("saved analyses must not open a provider client")


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(
        output_root=tmp_path, client_factory=_no_client, warehouse_root=tmp_path / "wh"
    )


def _catalog(service: BuilderService) -> TableCatalog:
    catalog = service._table_catalog()
    assert catalog is not None
    return catalog


def _commit(catalog: TableCatalog, tmp_path: Path, values: list[int], workspace: str) -> str:
    gold = tmp_path / f"gold-{len(list(tmp_path.iterdir()))}"
    gold.mkdir()
    pl.DataFrame({"v": values}).write_parquet(gold / "table.parquet")
    return materialize(
        catalog,
        workspace_id=workspace,
        logical_name=_NAME,
        source_dir=gold,
        run_id=f"run-{values[0]}",
    ).snapshot.id


def _body(response: ServiceResponse) -> dict[str, JsonValue]:
    assert isinstance(response.body, dict)
    return response.body


def _save(service: BuilderService, principal: Principal = _DEV, **extra: JsonValue) -> str:
    response = service.create_analysis(
        {"name": "PM10 total", "table": _NAME, "sql": _SQL, **extra}, principal=principal
    )
    assert response.status_code == 200, response.body
    return cast(str, cast(dict[str, JsonValue], _body(response)["analysis"])["analysis_id"])


def _total(response: ServiceResponse) -> JsonValue:
    assert response.status_code == 200, response.body
    result = cast(dict[str, JsonValue], _body(response)["result"])
    return cast(list[dict[str, JsonValue]], result["rows"])[0]["total"]


def test_saving_stores_the_concrete_snapshot_and_result_metadata(tmp_path: Path) -> None:
    service = _service(tmp_path)
    snapshot = _commit(_catalog(service), tmp_path, [1, 2], PERSONAL_WORKSPACE)

    created = service.create_analysis(
        {"name": "PM10 total", "table": _NAME, "sql": _SQL}, principal=_DEV
    )

    assert _total(created) == 3
    analysis = cast(dict[str, JsonValue], _body(created)["analysis"])
    assert analysis["bindings"] == [{"table": _NAME, "snapshot_id": snapshot}]
    meta = cast(dict[str, JsonValue], analysis["result_meta"])
    assert (meta["columns"], meta["row_count"], meta["truncated"]) == (["total"], 1, False)
    assert "rows" not in meta
    stored = _body(service.get_analysis(cast(str, analysis["analysis_id"]), principal=_DEV))
    assert stored == analysis


def test_a_rerun_after_a_refresh_reads_the_saved_snapshot(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [1, 2], PERSONAL_WORKSPACE)
    analysis_id = _save(service)
    _commit(_catalog(service), tmp_path, [100], PERSONAL_WORKSPACE)

    rerun = service.run_analysis(analysis_id, principal=_DEV)
    today = service.query_warehouse({"table": _NAME, "sql": _SQL}, principal=_DEV)

    assert _total(rerun) == 3
    assert _total(today) == 100


def test_the_saved_snapshot_is_held_past_garbage_collection(tmp_path: Path) -> None:
    service = _service(tmp_path)
    catalog = _catalog(service)
    saved = _commit(catalog, tmp_path, [1], PERSONAL_WORKSPACE)
    analysis_id = _save(service)
    for value in (2, 3, 4):
        _commit(catalog, tmp_path, [value], PERSONAL_WORKSPACE)

    report = warehouse_gc.collect(catalog, catalog.list_tables()[0].id, keep=1)

    assert saved in report.kept_held
    assert _total(service.run_analysis(analysis_id, principal=_DEV)) == 1


def test_deleting_releases_the_hold(tmp_path: Path) -> None:
    service = _service(tmp_path)
    catalog = _catalog(service)
    saved = _commit(catalog, tmp_path, [1], PERSONAL_WORKSPACE)
    analysis_id = _save(service)
    assert len(catalog.live_holds(saved)) == 1

    deleted = service.delete_analysis(analysis_id, principal=_DEV)

    assert _body(deleted) == {"analysis_id": analysis_id, "deleted": True}
    assert catalog.live_holds(saved) == []
    assert service.get_analysis(analysis_id, principal=_DEV).status_code == 404


def test_a_failed_query_saves_nothing_and_holds_nothing(tmp_path: Path) -> None:
    """Negative: nothing is stored or held for a query that did not run."""
    service = _service(tmp_path)
    snapshot = _commit(_catalog(service), tmp_path, [1], PERSONAL_WORKSPACE)

    response = service.create_analysis(
        {"name": "bad", "table": _NAME, "sql": "DROP TABLE dataset"}, principal=_DEV
    )

    assert (response.status_code, _body(response)["code"]) == (400, "unsafe_query")
    assert _body(service.list_analyses(principal=_DEV))["analyses"] == []
    assert _catalog(service).live_holds(snapshot) == []


@pytest.mark.parametrize(
    "body",
    [
        {"table": _NAME, "sql": _SQL},
        {"name": " ", "table": _NAME, "sql": _SQL},
        {"name": "x" * 201, "table": _NAME, "sql": _SQL},
        {"name": "n", "table": _NAME, "sql": _SQL, "rows": []},
    ],
    ids=["no-name", "blank-name", "long-name", "unknown-field"],
)
def test_invalid_bodies_are_refused(tmp_path: Path, body: dict[str, JsonValue]) -> None:
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [1], PERSONAL_WORKSPACE)

    response = service.create_analysis(body, principal=_DEV)

    assert (response.status_code, _body(response)["code"]) == (400, "invalid_request")


def test_another_owners_analysis_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative: Alice cannot list, read, run or delete Bob's analysis."""
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    alice = Principal("oidc", "alice", "oidc:alice")
    bob = Principal("oidc", "bob", "oidc:bob")
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [1], warehouse_workspace(bob.owner_id))
    analysis_id = _save(service, principal=bob)

    assert _body(service.list_analyses(principal=alice))["analyses"] == []
    for response in (
        service.get_analysis(analysis_id, principal=alice),
        service.run_analysis(analysis_id, principal=alice),
        service.delete_analysis(analysis_id, principal=alice),
    ):
        assert (response.status_code, _body(response)["code"]) == (404, "analysis_not_found")
    assert service.get_analysis(analysis_id, principal=bob).status_code == 200


def test_the_routes_are_dispatched(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [5], PERSONAL_WORKSPACE)

    def call(method: str, path: str, body: dict[str, JsonValue] | None = None) -> ServiceResponse:
        response = dispatch(service, method, path, body)
        assert isinstance(response, ServiceResponse)
        return response

    created = call("POST", "/analyses", {"name": "n", "table": _NAME, "sql": _SQL})
    analysis_id = cast(dict[str, JsonValue], _body(created)["analysis"])["analysis_id"]

    assert call("GET", "/analyses").status_code == 200
    assert call("GET", f"/analyses/{analysis_id}").status_code == 200
    assert _total(call("POST", f"/analyses/{analysis_id}/run")) == 5
    assert call("DELETE", f"/analyses/{analysis_id}").status_code == 200
    assert call("GET", "/analyses/a/b").status_code == 404


# ------------------------------------------------------------- SQL dialect (#875)


def test_a_new_analysis_records_its_dialect_and_engine(tmp_path: Path) -> None:
    import duckdb

    from kpubdata_builder.service.app import API_CONTRACT_VERSION

    service = _service(tmp_path)
    _commit(_catalog(service), tmp_path, [1, 2], PERSONAL_WORKSPACE)

    analysis_id = _save(service)
    stored = _body(service.get_analysis(analysis_id, principal=_DEV))

    assert {k: stored[k] for k in ("sql_dialect", "engine", "engine_version")} == {
        "sql_dialect": "duckdb",
        "engine": "duckdb",
        "engine_version": duckdb.__version__,
    }
    assert stored["query_contract_version"] == API_CONTRACT_VERSION
    assert stored["migration_required"] is False
    assert _total(service.run_analysis(analysis_id, principal=_DEV)) == 3


def _legacy_store(tmp_path: Path, snapshot_id: str, hold_id: str) -> None:
    """A store as #783 created it, before the dialect columns, with one analysis."""
    import sqlite3

    path = tmp_path / ".service" / "analyses.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE analyses ("
            " analysis_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_id TEXT,"
            " name TEXT NOT NULL, sql TEXT NOT NULL, row_limit INTEGER NOT NULL,"
            " table_name TEXT NOT NULL, snapshot_id TEXT NOT NULL, hold_id TEXT NOT NULL,"
            " result_meta TEXT NOT NULL, created_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO analyses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "ana_legacy",
                PERSONAL_WORKSPACE,
                None,
                "old total",
                _SQL,
                100,
                _NAME,
                snapshot_id,
                hold_id,
                '{"columns": ["total"], "column_meta": [], "row_count": 1, '
                '"truncated": false, "executed_at": "2026-09-01T00:00:00+00:00"}',
                "2026-09-01T00:00:00+00:00",
            ),
        )


def test_an_existing_store_gains_the_columns_and_its_rows_are_legacy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    catalog = _catalog(service)
    snapshot = _commit(catalog, tmp_path, [1, 2], PERSONAL_WORKSPACE)
    hold = catalog.place_hold(snapshot, kind="saved_analysis", reason="legacy").hold_id
    _legacy_store(tmp_path, snapshot, hold)

    listed = cast(
        list[dict[str, JsonValue]], _body(service.list_analyses(principal=_DEV))["analyses"]
    )

    (legacy,) = listed
    assert legacy["sql_dialect"] == "legacy-polars"
    assert legacy["engine"] == "polars"
    assert legacy["engine_version"] is None and legacy["query_contract_version"] is None
    assert legacy["migration_required"] is True
    # Saving a new analysis next to it works on the migrated store.
    _save(service)
    assert len(cast(list[JsonValue], _body(service.list_analyses(principal=_DEV))["analyses"])) == 2


def test_a_legacy_analysis_is_not_rerun_silently(tmp_path: Path) -> None:
    """Negative: the same text may mean something else on DuckDB; it is refused, not run."""
    service = _service(tmp_path)
    catalog = _catalog(service)
    snapshot = _commit(catalog, tmp_path, [1, 2], PERSONAL_WORKSPACE)
    hold = catalog.place_hold(snapshot, kind="saved_analysis", reason="legacy").hold_id
    _legacy_store(tmp_path, snapshot, hold)

    response = service.run_analysis("ana_legacy", principal=_DEV)

    assert response.status_code == 409
    assert _body(response)["code"] == "analysis_migration_required"
    assert _body(response)["sql_dialect"] == "legacy-polars"
    # Deleting it still releases the snapshot it held.
    assert service.delete_analysis("ana_legacy", principal=_DEV).status_code == 200
    assert hold not in {h.hold_id for h in catalog.live_holds(snapshot)}

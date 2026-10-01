"""An administrator sees a run's metadata, never its bytes (#679, option a).

ADR 0012's 2026-09-30 amendment. ``Principal.is_admin`` opens the admin routes only; on
every route that serves a run's bytes an administrator is one more user who does not
own the run, so it gets the answer a missing run gets (#796).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service import app as app_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue

_SPEC = """\
dataset_id: secret.table
title: Secret
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""
_ROW_VALUE = "alice-private-value-679"
_ADMIN = Principal("oidc", "admin", "oidc:admin", is_admin=True)


class _Result:
    items = [{"id": "1", "note": _ROW_VALUE}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


@pytest.fixture()
def built(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[BuilderService, str, str]:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: _Client(),
        warehouse_root=tmp_path / "wh",
    )
    response = service.build(
        _SPEC, run_id="alice-run", owner_id="oidc:alice", manifest_owner_id="oidc:alice"
    )
    assert response.status_code == 200, response.body
    (committed,) = cast(dict[str, dict[str, JsonValue]], response.body["materialized"]).values()
    monkeypatch.setattr(app_module, "authenticate", lambda **_: _ADMIN)
    return service, "alice-run", cast(str, committed["logical_name"])


def _call(
    service: BuilderService, method: str, path: str, body: dict[str, JsonValue] | None = None
) -> ServiceResponse:
    route, _, query = path.partition("?")
    response = dispatch(service, method, route, body, query)
    assert isinstance(response, ServiceResponse)
    return response


def _no_bytes(response: ServiceResponse) -> None:
    assert _ROW_VALUE not in json.dumps(response.body, ensure_ascii=False)


@pytest.mark.parametrize(
    "path",
    [
        "/artifacts/{run}",
        "/artifacts/{run}/gold/datago.air_quality/data.jsonl",
        "/builds/{run}/manifest",
        "/builds/{run}/stages",
        "/builds/{run}/stages/silver?source=datago.air_quality",
        "/builds/{run}/spec",
    ],
)
def test_an_administrator_gets_no_run_bytes(
    built: tuple[BuilderService, str, str], path: str
) -> None:
    """Negative: artifacts, stage samples and manifests answer 404, as for anyone."""
    service, run, _ = built

    response = _call(service, "GET", path.format(run=run))

    assert response.status_code == 404
    _no_bytes(response)


def test_an_administrator_cannot_query_the_run(built: tuple[BuilderService, str, str]) -> None:
    service, run, _ = built

    response = _call(
        service,
        "POST",
        "/query",
        {
            "dataset_id": "secret.table",
            "run_id": run,
            "stage": "silver",
            "sql": "SELECT * FROM dataset",
        },
    )

    assert (response.status_code, cast(dict[str, JsonValue], response.body)["code"]) == (
        400,
        "invalid_context",
    )
    _no_bytes(response)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/warehouse/query", {"sql": "SELECT * FROM dataset"}),
        ("POST", "/warehouse/rows", {}),
    ],
)
def test_an_administrator_cannot_read_the_owners_tables(
    built: tuple[BuilderService, str, str],
    method: str,
    path: str,
    body: dict[str, JsonValue],
) -> None:
    service, _, table = built

    listed = _call(service, "GET", "/warehouse/tables")
    response = _call(service, method, path, {"table": table, **body})

    assert cast(list[JsonValue], listed.body["tables"]) == []
    assert (response.status_code, cast(dict[str, JsonValue], response.body)["code"]) == (
        404,
        "table_not_found",
    )
    _no_bytes(response)


def test_the_admin_view_is_metadata_with_the_reason(
    built: tuple[BuilderService, str, str],
) -> None:
    service, run, _ = built

    response = _call(service, "GET", "/admin/runs")

    assert response.status_code == 200
    (row,) = [
        r for r in cast(list[dict[str, JsonValue]], response.body["runs"]) if r["run_id"] == run
    ]
    assert set(row) == {"run_id", "status", "started_at", "finished_at", "owner_id", "error"}
    assert row["status"] == "ok"
    assert row["error"] is None
    # #948: `count` is this response's runs; `total` the runs before `limit`.
    runs = cast(list[JsonValue], response.body["runs"])
    assert response.body["count"] == len(runs)
    assert response.body["total"] == len(runs)
    _no_bytes(response)

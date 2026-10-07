"""Every contract operation, counted against the API key (#1091, ADR 0012).

Where ownership is enforced the ``service`` principal — the deployment's ``X-API-Key`` —
is like an administrator: the administration routes show it every run's metadata, and
another user's runs, tables, files and rows are not there for it (#1072). #1088 tested
that on eight run paths. This classifies **every** operation the contract declares, so a
new route fails here until someone says what it lets out, and asks each operation that
answers with something a user owns whether the API key gets Alice's.

Classes:

- ``public``: the same answer for everyone; no user's data (health, version, catalog,
  provider status, the system aggregates of the monitoring summary).
- ``caller``: works on what the request itself carries or on the caller's own records
  (validate, preview, a new build or upload, the caller's own provider credentials).
- ``admin``: the administration routes; the API key is an administrator there.
- ``owner``: answers with, or changes, something a user owns — a run, a dataset's runs,
  a table, an upload, an export, a saved analysis, a document revision. Each has a probe
  below: Alice's own request reaches her record, and the API key's gets what a missing
  record gets (or a list without it).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, FileResponse, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal, compute_owner_id
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

from .test_service import _FakeClient
from .test_service_publish import LICENSED_SPEC_YAML

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

_ALICE = Principal(kind="oidc", identifier="alice", owner_id="oidc:alice")
#: The principal an ``X-API-Key`` request gets (``auth._verify_api_key``).
_API_KEY = Principal(kind="service", owner_id=compute_owner_id("service", "default"), is_admin=True)

_PUBLIC = "public"
_CALLER = "caller"
_ADMIN = "admin"
_OWNER = "owner"

#: What each contract operation answers with. Keep in the contract's order.
CLASSIFICATION: dict[str, str] = {
    "GET /healthz": _PUBLIC,
    "GET /version": _PUBLIC,
    "GET /catalog": _PUBLIC,
    "GET /providers": _PUBLIC,
    "GET /providers/{provider}/status": _PUBLIC,
    "POST /providers/{provider}/test": _CALLER,
    "POST /providers/{provider}/probe": _CALLER,
    "GET /providers/{provider}/credential": _CALLER,
    "PUT /providers/{provider}/credential": _CALLER,
    "DELETE /providers/{provider}/credential": _CALLER,
    "GET /uploads": _OWNER,
    "POST /uploads": _CALLER,
    "GET /uploads/{upload_id}": _OWNER,
    "DELETE /uploads/{upload_id}": _OWNER,
    "POST /validate": _CALLER,
    "POST /preview": _CALLER,
    # Naming a run_id that is already someone else's (test_service_principal_metadata_only).
    "POST /build": _OWNER,
    "GET /artifacts/{run_id}": _OWNER,
    "GET /artifacts/{run_id}/{file_path}": _OWNER,
    "GET /builds/{run_id}": _OWNER,
    "POST /builds/{run_id}/cancel": _OWNER,
    "GET /builds/{run_id}/manifest": _OWNER,
    "GET /builds/{run_id}/spec": _OWNER,
    "POST /builds": _CALLER,
    "GET /builds": _OWNER,
    "GET /datasets": _OWNER,
    "GET /datasets/{dataset_id}": _OWNER,
    "GET /datasets/{dataset_id}/runs": _OWNER,
    "GET /datasets/{dataset_id}/runs/{run_id}": _OWNER,
    "GET /builds/{run_id}/events": _OWNER,
    "GET /builds/{run_id}/publish/readiness": _OWNER,
    "GET /builds/{run_id}/publish/receipt": _OWNER,
    "DELETE /builds/{run_id}/publish/receipt": _OWNER,
    "POST /builds/{run_id}/publish/reconcile": _OWNER,
    "GET /builds/{run_id}/publish/audit": _OWNER,
    "POST /builds/{run_id}/publish": _OWNER,
    "GET /builds/{run_id}/stages": _OWNER,
    "GET /builds/{run_id}/stages/{stage}": _OWNER,
    "GET /datasets/{dataset_id}/quality/history": _OWNER,
    "GET /builds/{run_id}/quality": _OWNER,
    "GET /quality/issues": _OWNER,
    "GET /quality/summary": _OWNER,
    "POST /query": _OWNER,
    "GET /warehouse/tables": _OWNER,
    "GET /warehouse/tables/{name}/profile": _OWNER,
    "GET /warehouse/tables/{name}": _OWNER,
    "POST /warehouse/query": _OWNER,
    "POST /warehouse/rows": _OWNER,
    "POST /warehouse/aggregate": _OWNER,
    "POST /warehouse/exports": _OWNER,
    "GET /warehouse/exports": _OWNER,
    "GET /warehouse/exports/{export_id}": _OWNER,
    "DELETE /warehouse/exports/{export_id}": _OWNER,
    "GET /warehouse/exports/{export_id}/download": _OWNER,
    "GET /analyses": _OWNER,
    "POST /analyses": _OWNER,
    "GET /analyses/{analysis_id}": _OWNER,
    "DELETE /analyses/{analysis_id}": _OWNER,
    "POST /analyses/{analysis_id}/run": _OWNER,
    "PUT /revisions/{kind}/{doc_id}": _OWNER,
    "GET /revisions/{kind}/{doc_id}": _OWNER,
    "GET /revisions/{kind}/{doc_id}/history": _OWNER,
    "POST /revisions/{kind}/{doc_id}/revert": _OWNER,
    # System aggregates only — queue, workers, artifact store (monitoring_api.monitoring_summary).
    "GET /monitoring/summary": _PUBLIC,
    "GET /monitoring/builds": _OWNER,
    "GET /admin/runs": _ADMIN,
    "GET /admin/users": _ADMIN,
    "POST /admin/users/{user_id}/approve": _ADMIN,
    "POST /admin/users/{user_id}/reject": _ADMIN,
    "GET /admin/config": _ADMIN,
}


def _contract_operations() -> list[str]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return [
        f"{method.upper()} {path}"
        for path, operations in contract["paths"].items()
        for method in operations
        if method in ("get", "post", "put", "delete", "patch")
    ]


def test_every_contract_operation_is_classified() -> None:
    """A new operation fails here until it is classified; a removed one, until it is dropped."""
    assert sorted(_contract_operations()) == sorted(CLASSIFICATION)


# ------------------------------------------------------------------------------ world


@dataclass
class World:
    """Alice's records, made through the routes while ownership is enforced."""

    service: BuilderService
    run_id: str
    dataset_id: str
    table: str
    upload_id: str
    export_id: str
    analysis_id: str


@dataclass(frozen=True)
class Request:
    method: str
    path: str
    body: dict[str, JsonValue] | None = None
    query: str = ""


def _as(monkeypatch: pytest.MonkeyPatch, principal: Principal) -> None:
    monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal)


def _call(world: World, request: Request) -> ServiceResponse | FileResponse:
    return dispatch(world.service, request.method, request.path, request.body, request.query)


def _ok(response: ServiceResponse | FileResponse) -> ServiceResponse | FileResponse:
    assert response.status_code < 400, getattr(response, "body", response)
    return response


def _body(response: ServiceResponse | FileResponse) -> dict[str, JsonValue]:
    assert isinstance(response, ServiceResponse)
    return response.body


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: client,
        warehouse_root=tmp_path / "wh",
        terms_lookup=lambda _id: "allowed",
        publish_visibility_probe=lambda *_: "absent",
    )
    _as(monkeypatch, _ALICE)
    built = _body(
        _ok(dispatch(service, "POST", "/build", {"spec": LICENSED_SPEC_YAML, "run_id": "alice-1"}))
    )
    materialized = cast(dict[str, dict[str, JsonValue]], built["materialized"])
    table = cast(str, next(iter(materialized.values()))["logical_name"])
    upload = _body(
        _ok(dispatch(service, "POST", "/uploads", None, query="format=csv", raw_body=b"a,b\n1,2\n"))
    )
    query: dict[str, JsonValue] = {"table": table, "sql": "SELECT * FROM dataset"}
    export = _body(_ok(dispatch(service, "POST", "/warehouse/exports", query)))
    analysis = _body(_ok(dispatch(service, "POST", "/analyses", {"name": "alice's", **query})))
    _ok(
        dispatch(
            service,
            "PUT",
            "/revisions/spec/alice-doc",
            {"content": {"yaml": LICENSED_SPEC_YAML}, "expected_revision": 0},
        )
    )
    return World(
        service=service,
        run_id="alice-1",
        dataset_id="dataset.publish",
        table=table,
        upload_id=cast(str, upload["upload_id"]),
        export_id=cast(str, export["export_id"]),
        analysis_id=cast(str, cast(dict[str, JsonValue], analysis["analysis"])["analysis_id"]),
    )


_BOB = Principal(kind="oidc", identifier="bob", owner_id="oidc:bob")
_GOLD_FILE = "gold/datago.air_quality/out/data.jsonl"
_RECEIPT = "target=huggingface&destination=kpubdata%2Fair-quality"


def _sql(world: World) -> dict[str, JsonValue]:
    return {"table": world.table, "sql": "SELECT * FROM dataset"}


#: For each ``owner`` operation, a request that names Alice's record.
PROBES: dict[str, Callable[[World], Request]] = {
    "GET /uploads": lambda w: Request("GET", "/uploads"),
    "GET /uploads/{upload_id}": lambda w: Request("GET", f"/uploads/{w.upload_id}"),
    "DELETE /uploads/{upload_id}": lambda w: Request("DELETE", f"/uploads/{w.upload_id}"),
    "POST /build": lambda w: Request(
        "POST", "/build", {"spec": LICENSED_SPEC_YAML, "run_id": w.run_id}
    ),
    "GET /artifacts/{run_id}": lambda w: Request("GET", f"/artifacts/{w.run_id}"),
    "GET /artifacts/{run_id}/{file_path}": lambda w: Request(
        "GET", f"/artifacts/{w.run_id}/{_GOLD_FILE}"
    ),
    "GET /builds/{run_id}": lambda w: Request("GET", f"/builds/{w.run_id}"),
    "POST /builds/{run_id}/cancel": lambda w: Request("POST", f"/builds/{w.run_id}/cancel"),
    "GET /builds/{run_id}/manifest": lambda w: Request("GET", f"/builds/{w.run_id}/manifest"),
    "GET /builds/{run_id}/spec": lambda w: Request("GET", f"/builds/{w.run_id}/spec"),
    "GET /builds": lambda w: Request("GET", "/builds"),
    "GET /datasets": lambda w: Request("GET", "/datasets"),
    "GET /datasets/{dataset_id}": lambda w: Request("GET", f"/datasets/{w.dataset_id}"),
    "GET /datasets/{dataset_id}/runs": lambda w: Request("GET", f"/datasets/{w.dataset_id}/runs"),
    "GET /datasets/{dataset_id}/runs/{run_id}": lambda w: Request(
        "GET", f"/datasets/{w.dataset_id}/runs/{w.run_id}"
    ),
    "GET /builds/{run_id}/events": lambda w: Request("GET", f"/builds/{w.run_id}/events"),
    "GET /builds/{run_id}/publish/readiness": lambda w: Request(
        "GET", f"/builds/{w.run_id}/publish/readiness", query="target=huggingface"
    ),
    "GET /builds/{run_id}/publish/receipt": lambda w: Request(
        "GET", f"/builds/{w.run_id}/publish/receipt", query=_RECEIPT
    ),
    "DELETE /builds/{run_id}/publish/receipt": lambda w: Request(
        "DELETE", f"/builds/{w.run_id}/publish/receipt", query=_RECEIPT
    ),
    "POST /builds/{run_id}/publish/reconcile": lambda w: Request(
        "POST",
        f"/builds/{w.run_id}/publish/reconcile",
        {"target": "huggingface", "destination": "kpubdata/air-quality"},
    ),
    "GET /builds/{run_id}/publish/audit": lambda w: Request(
        "GET", f"/builds/{w.run_id}/publish/audit"
    ),
    "POST /builds/{run_id}/publish": lambda w: Request(
        "POST",
        f"/builds/{w.run_id}/publish",
        {
            "target": "huggingface",
            "destination": "kpubdata/air-quality",
            "options": {"private": True},
        },
    ),
    "GET /builds/{run_id}/stages": lambda w: Request("GET", f"/builds/{w.run_id}/stages"),
    "GET /builds/{run_id}/stages/{stage}": lambda w: Request(
        "GET", f"/builds/{w.run_id}/stages/silver", query="source=datago.air_quality"
    ),
    "GET /datasets/{dataset_id}/quality/history": lambda w: Request(
        "GET", f"/datasets/{w.dataset_id}/quality/history"
    ),
    "GET /builds/{run_id}/quality": lambda w: Request("GET", f"/builds/{w.run_id}/quality"),
    "GET /quality/issues": lambda w: Request("GET", "/quality/issues"),
    "GET /quality/summary": lambda w: Request("GET", "/quality/summary"),
    "POST /query": lambda w: Request(
        "POST",
        "/query",
        {
            "dataset_id": w.dataset_id,
            "run_id": w.run_id,
            "stage": "silver",
            "sql": "SELECT * FROM dataset",
        },
    ),
    "GET /warehouse/tables": lambda w: Request("GET", "/warehouse/tables"),
    "GET /warehouse/tables/{name}/profile": lambda w: Request(
        "GET", f"/warehouse/tables/{w.table}/profile"
    ),
    "GET /warehouse/tables/{name}": lambda w: Request("GET", f"/warehouse/tables/{w.table}"),
    "POST /warehouse/query": lambda w: Request("POST", "/warehouse/query", _sql(w)),
    "POST /warehouse/rows": lambda w: Request("POST", "/warehouse/rows", {"table": w.table}),
    "POST /warehouse/aggregate": lambda w: Request(
        "POST",
        "/warehouse/aggregate",
        {"table": w.table, "measures": [{"fn": "count_rows", "as": "n"}]},
    ),
    "POST /warehouse/exports": lambda w: Request("POST", "/warehouse/exports", _sql(w)),
    "GET /warehouse/exports": lambda w: Request("GET", "/warehouse/exports"),
    "GET /warehouse/exports/{export_id}": lambda w: Request(
        "GET", f"/warehouse/exports/{w.export_id}"
    ),
    "DELETE /warehouse/exports/{export_id}": lambda w: Request(
        "DELETE", f"/warehouse/exports/{w.export_id}"
    ),
    "GET /warehouse/exports/{export_id}/download": lambda w: Request(
        "GET", f"/warehouse/exports/{w.export_id}/download"
    ),
    "GET /analyses": lambda w: Request("GET", "/analyses"),
    "POST /analyses": lambda w: Request("POST", "/analyses", {"name": "key's", **_sql(w)}),
    "GET /analyses/{analysis_id}": lambda w: Request("GET", f"/analyses/{w.analysis_id}"),
    "DELETE /analyses/{analysis_id}": lambda w: Request("DELETE", f"/analyses/{w.analysis_id}"),
    "POST /analyses/{analysis_id}/run": lambda w: Request("POST", f"/analyses/{w.analysis_id}/run"),
    "PUT /revisions/{kind}/{doc_id}": lambda w: Request(
        "PUT", "/revisions/spec/alice-doc", {"content": {"yaml": "x: 1\n"}, "expected_revision": 1}
    ),
    "GET /revisions/{kind}/{doc_id}": lambda w: Request("GET", "/revisions/spec/alice-doc"),
    "GET /revisions/{kind}/{doc_id}/history": lambda w: Request(
        "GET", "/revisions/spec/alice-doc/history"
    ),
    "POST /revisions/{kind}/{doc_id}/revert": lambda w: Request(
        "POST", "/revisions/spec/alice-doc/revert", {"to_revision": 1, "expected_revision": 1}
    ),
    "GET /monitoring/builds": lambda w: Request("GET", "/monitoring/builds"),
}


#: Fields that change between two calls whoever makes them: the time an answer was
#: generated.
_VOLATILE = frozenset({"generated_at"})


def _answer(response: ServiceResponse | FileResponse) -> tuple[int, object]:
    if isinstance(response, FileResponse):
        return response.status_code, ("file", response.file_path.name)
    return response.status_code, _without_volatile(response.body)


def _without_volatile(value: object) -> object:
    if isinstance(value, dict):
        return {k: _without_volatile(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_without_volatile(v) for v in value]
    return value


def test_every_owner_operation_has_a_probe() -> None:
    owner = {op for op, kind in CLASSIFICATION.items() if kind == _OWNER}
    assert set(PROBES) == owner


@pytest.mark.parametrize("operation", sorted(PROBES))
def test_the_api_key_gets_what_a_user_with_nothing_gets(
    operation: str, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bob signed in and owns nothing. The API key's answer is his, and Alice's is not."""
    request = PROBES[operation](world)

    answers = {}
    # Alice last: a DELETE of hers must find her record still there.
    for name, principal in (("api key", _API_KEY), ("bob", _BOB), ("alice", _ALICE)):
        _as(monkeypatch, principal)
        answers[name] = _answer(_call(world, request))

    assert answers["api key"] == answers["bob"], operation
    assert answers["alice"] != answers["bob"], (
        f"{operation}: the probe does not reach Alice's record"
    )


def test_nothing_the_api_key_called_changed_alices_records(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _as(monkeypatch, _API_KEY)
    for operation in sorted(PROBES):
        _call(world, PROBES[operation](world))

    _as(monkeypatch, _ALICE)
    assert _call(world, Request("GET", f"/uploads/{world.upload_id}")).status_code == 200
    assert _call(world, Request("GET", f"/warehouse/exports/{world.export_id}")).status_code == 200
    assert _call(world, Request("GET", f"/analyses/{world.analysis_id}")).status_code == 200
    revision = _body(_call(world, Request("GET", "/revisions/spec/alice-doc")))
    assert revision["revision"] == 1
    assert cast(dict[str, JsonValue], revision["content"])["yaml"] == LICENSED_SPEC_YAML
    assert _body(_call(world, Request("GET", f"/builds/{world.run_id}")))["status"] != "cancelled"


def test_the_api_key_still_lists_its_own_runs(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: the lists drop other owners' runs, not the key's own."""
    _as(monkeypatch, _API_KEY)
    _ok(_call(world, Request("POST", "/build", {"spec": LICENSED_SPEC_YAML, "run_id": "key-1"})))

    listed = _body(_call(world, Request("GET", "/builds")))["builds"]

    assert [b["run_id"] for b in cast(list[dict[str, JsonValue]], listed)] == ["key-1"]
    datasets = cast(
        list[dict[str, JsonValue]], _body(_call(world, Request("GET", "/datasets")))["datasets"]
    )
    assert [d["dataset_id"] for d in datasets] == ["dataset.publish"]


def test_in_a_single_user_deployment_the_api_key_lists_every_run(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: where ownership is not enforced the lists are as before."""
    monkeypatch.delenv(_OWNERSHIP_ENV)
    _as(monkeypatch, _API_KEY)

    listed = _body(_call(world, Request("GET", "/builds")))["builds"]

    assert [b["run_id"] for b in cast(list[dict[str, JsonValue]], listed)] == ["alice-1"]

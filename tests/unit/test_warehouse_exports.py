"""Policy-checked exports of pinned query results (#819).

A client that makes its own CSV from rows it was sent skips the licence, the PII policy
and the snapshot the rows came from, and a result cut at a page or size limit becomes a
file that looks complete. These pin what `/warehouse/exports` promises:

- an export holds exactly what the same query returns from the same snapshot, and
  names that snapshot, also after a newer one is committed;
- over the row or byte limit, nothing is exported and the answer says so;
- a no-derivatives licence and PII values the source's policy does not allow out are
  refused;
- the licence terms travel with the file verbatim, with provenance and user_derived;
- the spreadsheet profile's formula guard is counted as an alteration; the machine
  profile alters nothing;
- who may download, and whether the export expired, is checked at download time.
"""

from __future__ import annotations

import csv
import io
import json
import multiprocessing
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import polars as pl
import pytest
import yaml

from kpubdata_builder.query.export import export_worker
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService, FileResponse, ServiceResponse, dispatch
from kpubdata_builder.service import exports_api as exports_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets import read_snapshot_spec
from kpubdata_builder.service.ownership import PERSONAL_WORKSPACE, warehouse_workspace
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import TableCatalog, materialize

from ._openapi import response_schema, validate

_DATASET = "demo-air"
_NAME = f"{_DATASET}.air"
_DEV = Principal("dev")
_SQL = "SELECT code, pm10 FROM dataset ORDER BY code"
_CONTRACT: dict[str, Any] = yaml.safe_load(
    (Path(__file__).parents[2] / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
)


class _InProcessExportEngine:
    """Runs the real export worker in this process, so the tests need no child process."""

    def execute(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        parent, child = multiprocessing.Pipe(duplex=False)
        export_worker(child, str(table_path), plan_json, limit, time.monotonic_ns())
        payload = parent.recv()
        parent.close()
        assert payload["ok"] is True, "the export worker failed"
        return QueryResult(
            columns=tuple(payload["columns"]),
            column_meta=(),
            rows=(),
            truncated=False,
            execution_ms=0,
            startup_ms=payload["startup_ms"],
            engine_execution_ms=payload["engine_execution_ms"],
            meta=payload["meta"],
        )


def _service(tmp_path: Path, *, real_engine: bool = False) -> BuilderService:
    engine = (
        None if real_engine else QueryService(export_engine=_InProcessExportEngine())  # type: ignore[arg-type]
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


_AIR = pl.DataFrame(
    {
        "code": ["00123", "00456", "00789"],
        "pm10": [Decimal("12.50"), None, Decimal("-3.00")],
        "note": ["=SUM(A1)", "plain", "-trimmed"],
    },
    schema={"code": pl.String, "pm10": pl.Decimal(10, 2), "note": pl.String},
)

_seq = 0


def _commit(
    service: BuilderService,
    tmp_path: Path,
    frame: pl.DataFrame = _AIR,
    *,
    workspace: str = PERSONAL_WORKSPACE,
    spec: dict[str, Any] | None = None,
) -> str:
    """Commit ``frame`` from a run whose spec snapshot and manifest are on disk."""
    global _seq
    _seq += 1
    run_id = f"run-{_seq}"
    run_dir = tmp_path / run_id
    run_dir.mkdir()
    document = {
        "dataset_id": _DATASET,
        "title": "Air",
        "description": "Air quality",
        "license": "other",
        "license_name": "kogl-type-1",
        "license_link": "https://www.kogl.or.kr/info/licenseType1.do",
        "attribution": "한국환경공단, 에어코리아 대기오염정보",
        "sources": [{"provider": "datago", "dataset": "air", "alias": "air"}],
        "exports": [{"kind": "jsonl", "output_path": "air.jsonl"}],
        **(spec or {}),
    }
    (run_dir / "buildspec.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True), encoding="utf-8"
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "provenance": [
                    {
                        "provider": "datago",
                        "dataset": "air",
                        "fetched_at": "2026-09-30T00:00:00Z",
                        "record_count": 3,
                        "data_checksum": "sha256:0",
                        "api_version": "unknown",
                        "params": {"sidoName": "서울"},
                    },
                    {"provider": "datago", "dataset": "other", "params": {}},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert read_snapshot_spec(tmp_path, run_id) is not None, "the test spec must parse"
    gold = tmp_path / f"gold-{_seq}"
    gold.mkdir()
    frame.write_parquet(gold / "table.parquet")
    result = materialize(
        _catalog(service),
        workspace_id=workspace,
        logical_name=_NAME,
        source_dir=gold,
        run_id=run_id,
        row_count=frame.height,
    )
    return result.snapshot.id


def _export(service: BuilderService, principal: Principal = _DEV, **body: Any) -> ServiceResponse:
    return service.create_warehouse_export(
        {"table": _NAME, "sql": _SQL, **body}, principal=principal
    )


def _ok(response: ServiceResponse) -> dict[str, Any]:
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.body)


def _bundle(
    service: BuilderService, export_id: str, principal: Principal = _DEV
) -> zipfile.ZipFile:
    response = service.download_warehouse_export(export_id, principal=principal)
    assert isinstance(response, FileResponse), getattr(response, "body", response)
    return zipfile.ZipFile(response.file_path)


def _csv_rows(archive: zipfile.ZipFile, name: str = "data.csv") -> list[list[str]]:
    text = archive.read(name).decode("utf-8")
    return list(csv.reader(io.StringIO(text.removeprefix("﻿"))))


def _export_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / ".service" / "exports"
    return sorted(root.iterdir()) if root.is_dir() else []


# ---------------------------------------------------------------- same snapshot, same rows


def test_an_export_holds_what_the_same_query_returns(tmp_path: Path) -> None:
    service = _service(tmp_path)
    old = _commit(service, tmp_path)
    query = _ok(service.query_warehouse({"table": _NAME, "sql": _SQL}, principal=_DEV))

    exported = _ok(_export(service))
    _commit(service, tmp_path, _AIR.with_columns(pl.lit("99999").alias("code")))
    again = _ok(_export(service, snapshot=old))

    expected = [["code", "pm10"]] + [
        ["" if row[c] is None else str(row[c]) for c in ("code", "pm10")]
        for row in query["result"]["rows"]
    ]
    assert expected[1:] == [["00123", "12.50"], ["00456", ""], ["00789", "-3.00"]]
    for body in (exported, again):
        assert body["manifest"]["snapshot"]["snapshot_id"] == old
        assert _csv_rows(_bundle(service, body["export_id"])) == expected
    assert query["snapshot"]["snapshot_id"] == old
    assert exported["manifest"]["output"]["row_count"] == 3
    assert exported["manifest"]["output"]["completeness"] == "full"


def test_jsonl_keeps_the_wire_encoding(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)

    body = _ok(_export(service, format="jsonl"))
    lines = _bundle(service, body["export_id"]).read("data.jsonl").decode("utf-8").splitlines()

    assert [json.loads(line) for line in lines] == [
        {"code": "00123", "pm10": "12.50"},
        {"code": "00456", "pm10": None},
        {"code": "00789", "pm10": "-3.00"},
    ]


# ---------------------------------------------------------------- limits


def test_more_rows_than_the_limit_is_a_failure_not_a_short_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)

    response = _export(service, max_rows=2)

    assert response.status_code == 422
    assert response.body["code"] == "row_limit_exceeded"
    assert response.body["limit"] == 2
    assert _export_dirs(tmp_path) == []
    assert _ok(service.list_warehouse_exports(principal=_DEV))["exports"] == []
    # Exactly at the limit is complete.
    assert _ok(_export(service, max_rows=3))["manifest"]["output"]["row_count"] == 3


def test_more_bytes_than_the_limit_is_a_failure_not_a_short_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_EXPORT_BYTES", 20)
    service = _service(tmp_path)
    _commit(service, tmp_path)

    response = _export(service)

    assert (response.status_code, response.body["code"]) == (422, "byte_limit_exceeded")
    assert _export_dirs(tmp_path) == []


def test_a_workspace_keeps_a_bounded_number_of_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_LIVE_EXPORTS", 1)
    service = _service(tmp_path)
    _commit(service, tmp_path)

    first = _ok(_export(service))
    refused = _export(service)
    service.delete_warehouse_export(first["export_id"], principal=_DEV)

    assert (refused.status_code, refused.body["code"]) == (429, "export_quota_exceeded")
    _ok(_export(service))


class _GatedExportEngine(_InProcessExportEngine):
    """Holds each export in the engine until every request has entered it or returned.

    Under a check made apart from the reservation, every request reaches the engine
    before any records its export, which is the race (#1343). Under an atomic
    reservation the requests that found no slot return first, and the gate opens.
    """

    def __init__(self, requests: int) -> None:
        self._cond = threading.Condition()
        self._requests = requests
        self._settled = 0
        self._entered: set[int] = set()

    def _settle(self) -> None:
        with self._cond:
            self._settled += 1
            self._cond.notify_all()

    def returned(self) -> None:
        """Called by a request that is done; it counts if it never entered the engine."""
        if threading.get_ident() not in self._entered:
            self._settle()

    def execute(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        self._entered.add(threading.get_ident())
        self._settle()
        with self._cond:
            assert self._cond.wait_for(lambda: self._settled >= self._requests, timeout=30)
        return super().execute(table_path, plan_json, limit=limit)


def _handles(
    service: BuilderService, tmp_path: Path, engine: _InProcessExportEngine, count: int
) -> list[exports_module.ExportsApiService]:
    """Separate export services over one output directory and one warehouse."""
    catalog = _catalog(service)
    query = QueryService(export_engine=engine, max_concurrency=count)  # type: ignore[arg-type]
    return [
        exports_module.ExportsApiService(
            output_root=tmp_path, table_catalog=lambda: catalog, engine=query
        )
        for _ in range(count)
    ]


def _race(
    handles: list[exports_module.ExportsApiService],
    gate: _GatedExportEngine,
    bodies: list[dict[str, Any]],
) -> list[ServiceResponse]:
    """Send one request per handle at once, released together by a barrier."""
    barrier = threading.Barrier(len(handles))
    responses: list[ServiceResponse | None] = [None] * len(handles)

    def send(index: int) -> None:
        barrier.wait(timeout=30)
        try:
            responses[index] = handles[index].create(
                {"table": _NAME, "sql": _SQL, **bodies[index]}, principal=_DEV
            )
        finally:
            gate.returned()

    threads = [threading.Thread(target=send, args=(i,)) for i in range(len(handles))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in threads)
    assert all(response is not None for response in responses)
    return cast(list[ServiceResponse], responses)


def test_concurrent_requests_cannot_take_more_slots_than_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_LIVE_EXPORTS", 2)
    service = _service(tmp_path)
    snapshot = _commit(service, tmp_path)
    _ok(_export(service))  # one slot remains
    gate = _GatedExportEngine(requests=6)
    handles = _handles(service, tmp_path, gate, 6)

    responses = _race(handles, gate, [{}] * 6)

    codes = sorted((r.status_code, r.body.get("code")) for r in responses)
    assert codes == [(200, None)] + [(429, "export_quota_exceeded")] * 5
    refused = next(r for r in responses if r.status_code == 429)
    assert refused.body == {
        "error": "at most 2 exports are kept; delete one first",
        "code": "export_quota_exceeded",
    }
    assert len(_ok(service.list_warehouse_exports(principal=_DEV))["exports"]) == 2
    assert len(_export_dirs(tmp_path)) == 2
    assert _catalog(service).live_lease_count(snapshot) == 0


def test_a_sibling_failure_does_not_drop_a_concurrent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_LIVE_EXPORTS", 2)
    service = _service(tmp_path)
    snapshot = _commit(service, tmp_path)
    gate = _GatedExportEngine(requests=2)
    handles = _handles(service, tmp_path, gate, 2)

    ok, failed = _race(handles, gate, [{}, {"max_rows": 1}])

    assert (failed.status_code, failed.body["code"]) == (422, "row_limit_exceeded")
    export_id = _ok(ok)["export_id"]
    assert [d.name for d in _export_dirs(tmp_path)] == [export_id]
    assert _csv_rows(_bundle(service, export_id))[1] == ["00123", "12.50"]
    assert _catalog(service).live_lease_count(snapshot) == 0
    # The failed request's slot is free again.
    _ok(_export(service))


class _Cancelled(BaseException):
    """Stands in for a request torn down mid-export (a client gone, a worker stopped)."""


def test_a_cancelled_export_releases_its_slot_and_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_LIVE_EXPORTS", 1)
    service = _service(tmp_path)
    snapshot = _commit(service, tmp_path)

    def cancel(*_: object, **__: object) -> QueryResult:
        raise _Cancelled

    with monkeypatch.context() as patch:
        patch.setattr(_InProcessExportEngine, "execute", cancel)
        with pytest.raises(_Cancelled):
            _export(service)

    assert _export_dirs(tmp_path) == []
    assert _catalog(service).live_lease_count(snapshot) == 0
    _ok(_export(service))


def test_a_refused_pin_releases_the_reserved_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_LIVE_EXPORTS", 1)
    service = _service(tmp_path)
    _commit(service, tmp_path)

    response = _export(service, snapshot="snap_missing")

    assert (response.status_code, response.body["code"]) == (404, "snapshot_not_found")
    assert _export_dirs(tmp_path) == []
    _ok(_export(service))


def test_an_abandoned_reservation_stops_counting_once_it_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(exports_module, "MAX_LIVE_EXPORTS", 1)
    service = _service(tmp_path)
    _commit(service, tmp_path)
    exports = service._exports_api
    # A process that died between reserving and finishing leaves its reservation behind.
    assert exports._reserve(warehouse_workspace(_DEV.owner_id), _DEV) is not None

    refused = _export(service)
    assert (refused.status_code, refused.body["code"]) == (429, "export_quota_exceeded")
    assert _ok(service.list_warehouse_exports(principal=_DEV))["exports"] == []

    later = datetime.now(timezone.utc) + exports_module.RESERVATION_TTL + timedelta(seconds=1)
    exports._now = lambda: later
    _ok(_export(service))


# ---------------------------------------------------------------- licence and PII


@pytest.mark.parametrize(
    "terms",
    [
        {"license_name": "kogl-type-3"},
        {"license_name": "kogl-type-4"},
        {"license_name": "공공누리 제3유형 (출처표시, 변경금지)"},
        {"license": "cc-by-nd-4.0", "license_name": None, "license_link": None},
        {"license": "cc-by-nc-nd-4.0", "license_name": None, "license_link": None},
    ],
)
def test_a_no_derivatives_licence_refuses_the_export(tmp_path: Path, terms: dict[str, Any]) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, spec=terms)

    response = _export(service)

    assert response.status_code == 403
    assert response.body["code"] == "export_forbidden_by_license"
    assert _export_dirs(tmp_path) == []


_PHONES = pl.DataFrame({"code": ["00123", "00456"], "contact": ["010-1234-5678", "none"]})


@pytest.mark.parametrize(
    ("pii", "allowed"),
    [
        (None, False),
        ({"mode": "block"}, False),
        ({"mode": "warn"}, False),
        ({"mode": "block", "allow_columns": ["contact"]}, True),
        ({"mode": "allow"}, True),
    ],
)
def test_pii_values_follow_the_sources_policy(
    tmp_path: Path, pii: dict[str, Any] | None, allowed: bool
) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path, _PHONES, spec={"pii": pii} if pii else None)

    response = _export(service, sql="SELECT * FROM dataset")
    not_selected = _export(service, sql="SELECT code FROM dataset")

    if allowed:
        assert response.status_code == 200, response.body
    else:
        assert response.status_code == 403
        assert response.body["code"] == "export_blocked_pii"
        # Column and kind only: the value itself never appears in the answer.
        assert response.body["findings"] == [{"column": "contact", "kind": "phone", "count": 1}]
        assert "010-1234-5678" not in json.dumps(response.body)
    assert not_selected.status_code == 200


def test_the_terms_and_provenance_travel_with_the_file(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)

    body = _ok(_export(service))
    archive = _bundle(service, body["export_id"])
    manifest = json.loads(archive.read("manifest.json"))
    notice = archive.read("NOTICE.md").decode("utf-8")

    assert sorted(archive.namelist()) == ["NOTICE.md", "data.csv", "manifest.json"]
    assert manifest == body["manifest"]
    assert manifest["source"]["terms"] == {
        "status": "declared",
        "license": "other",
        "license_name": "kogl-type-1",
        "license_link": "https://www.kogl.or.kr/info/licenseType1.do",
        "attribution": "한국환경공단, 에어코리아 대기오염정보",
    }
    # Only this source's provenance, not the run's other source.
    assert [p["dataset"] for p in manifest["source"]["provenance"]] == ["air"]
    assert manifest["source"]["provenance"][0]["params"] == {"sidoName": "서울"}
    assert manifest["query"] == {"sql": _SQL, "user_derived": True}
    assert "kogl-type-1" in notice and "cc-by" not in notice
    assert manifest["output"]["file"]["sha256"].startswith("sha256:")


def test_undeclared_terms_are_said_to_be_undeclared(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(
        service,
        tmp_path,
        spec={"license": None, "license_name": None, "license_link": None, "attribution": None},
    )

    body = _ok(_export(service))
    notice = _bundle(service, body["export_id"]).read("NOTICE.md").decode("utf-8")

    assert body["manifest"]["source"]["terms"]["status"] == "undeclared"
    assert "No licence was declared" in notice


# ---------------------------------------------------------------- profiles


def test_the_spreadsheet_profile_counts_what_its_formula_guard_changes(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)
    sql = "SELECT code, pm10, note FROM dataset ORDER BY code"

    machine = _ok(_export(service, sql=sql))
    sheet = _ok(_export(service, sql=sql, profile="spreadsheet"))
    machine_bytes = _bundle(service, machine["export_id"]).read("data.csv")
    sheet_bytes = _bundle(service, sheet["export_id"]).read("data.csv")

    assert not machine_bytes.startswith(b"\xef\xbb\xbf")
    assert sheet_bytes.startswith(b"\xef\xbb\xbf")
    assert _csv_rows(_bundle(service, machine["export_id"]))[1:] == [
        ["00123", "12.50", "=SUM(A1)"],
        ["00456", "", "plain"],
        ["00789", "-3.00", "-trimmed"],
    ]
    # Text that could run as a formula is prefixed; a negative decimal is not text.
    assert _csv_rows(_bundle(service, sheet["export_id"]))[1:] == [
        ["00123", "12.50", "'=SUM(A1)"],
        ["00456", "", "plain"],
        ["00789", "-3.00", "'-trimmed"],
    ]
    assert machine["manifest"]["output"]["values_altered"] == []
    assert sheet["manifest"]["output"]["values_altered"] == [
        {"column": "note", "count": 2, "reason": "formula_prefix"}
    ]
    assert sheet["manifest"]["output"]["bom"] is True


# ---------------------------------------------------------------- download-time checks


def test_another_owner_cannot_read_download_or_delete_an_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = _service(tmp_path)
    alice = Principal("oidc", "alice", "oidc:alice")
    bob = Principal("oidc", "bob", "oidc:bob")
    _commit(service, tmp_path, workspace=warehouse_workspace("oidc:alice"))
    export_id = _ok(_export(service, alice))["export_id"]

    for response in (
        service.get_warehouse_export(export_id, principal=bob),
        service.download_warehouse_export(export_id, principal=bob),
        service.delete_warehouse_export(export_id, principal=bob),
    ):
        assert isinstance(response, ServiceResponse)
        assert (response.status_code, response.body["code"]) == (404, "export_not_found")
    assert _ok(service.list_warehouse_exports(principal=bob))["exports"] == []
    assert isinstance(service.download_warehouse_export(export_id, principal=alice), FileResponse)


def test_an_expired_export_cannot_be_downloaded(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)
    export_id = _ok(_export(service))["export_id"]
    later = datetime.now(timezone.utc) + exports_module.EXPORT_RETENTION + timedelta(seconds=1)
    service._exports_api._now = lambda: later

    body = _ok(service.get_warehouse_export(export_id, principal=_DEV))
    response = service.download_warehouse_export(export_id, principal=_DEV)

    assert body["status"] == "expired"
    assert body["download_path"] is None
    assert isinstance(response, ServiceResponse)
    assert (response.status_code, response.body["code"]) == (410, "export_expired")
    assert not (tmp_path / ".service" / "exports" / export_id / "export.zip").exists()


def test_a_changed_file_is_not_served(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)
    export_id = _ok(_export(service))["export_id"]
    (tmp_path / ".service" / "exports" / export_id / "export.zip").write_bytes(b"tampered")

    response = service.download_warehouse_export(export_id, principal=_DEV)

    assert isinstance(response, ServiceResponse)
    assert (response.status_code, response.body["code"]) == (409, "export_unavailable")


def test_delete_removes_the_export(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)
    export_id = _ok(_export(service))["export_id"]

    deleted = _ok(service.delete_warehouse_export(export_id, principal=_DEV))
    response = service.get_warehouse_export(export_id, principal=_DEV)

    assert deleted == {"export_id": export_id, "deleted": True}
    assert response.status_code == 404
    assert _export_dirs(tmp_path) == []


def test_the_lease_is_released(tmp_path: Path) -> None:
    service = _service(tmp_path)
    snapshot = _commit(service, tmp_path)

    _ok(_export(service))
    _export(service, max_rows=1)

    assert _catalog(service).live_lease_count(snapshot) == 0


# ---------------------------------------------------------------- refusals


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"format": "parquet"}, "invalid_request"),
        ({"format": "jsonl", "profile": "spreadsheet"}, "invalid_request"),
        ({"profile": "excel"}, "invalid_request"),
        ({"max_rows": 0}, "invalid_request"),
        ({"max_rows": 1_000_001}, "invalid_request"),
        ({"limit": 10}, "invalid_request"),
        ({"sql": "DROP TABLE dataset"}, "unsafe_query"),
    ],
)
def test_invalid_requests_are_400(tmp_path: Path, body: dict[str, Any], code: str) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)

    response = _export(service, **body)

    assert (response.status_code, response.body["code"]) == (400, code)


# ---------------------------------------------------------------- contract and HTTP


def test_the_responses_conform_to_the_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _commit(service, tmp_path)

    created = dispatch(
        service,
        "POST",
        "/warehouse/exports",
        {"table": _NAME, "sql": _SQL, "profile": "spreadsheet"},
    )
    assert isinstance(created, ServiceResponse) and created.status_code == 200, created
    export_id = created.body["export_id"]
    listed = dispatch(service, "GET", "/warehouse/exports", None)
    one = dispatch(service, "GET", f"/warehouse/exports/{export_id}", None)
    download = dispatch(service, "GET", f"/warehouse/exports/{export_id}/download", None)
    deleted = dispatch(service, "DELETE", f"/warehouse/exports/{export_id}", None)

    for path, method, response in (
        ("/warehouse/exports", "post", created),
        ("/warehouse/exports", "get", listed),
        ("/warehouse/exports/{export_id}", "get", one),
        ("/warehouse/exports/{export_id}", "delete", deleted),
    ):
        assert isinstance(response, ServiceResponse) and response.status_code == 200
        schema = response_schema(_CONTRACT, path, method, 200)
        assert schema is not None
        assert validate(cast(JsonValue, response.body), schema, _CONTRACT) == [], path
    assert isinstance(download, FileResponse)
    assert download.filename == f"{export_id}.zip"


def test_the_real_engine_writes_the_export_in_a_child_process(tmp_path: Path) -> None:
    service = _service(tmp_path, real_engine=True)
    _commit(service, tmp_path)

    body = _ok(_export(service))

    assert _csv_rows(_bundle(service, body["export_id"]))[1] == ["00123", "12.50"]

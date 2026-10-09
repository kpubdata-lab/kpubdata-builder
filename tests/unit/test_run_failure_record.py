"""A failed run says why, in its manifest and wherever it is listed (#1120).

The build index has an ``error`` column that nothing wrote: a build and a rebuild both
left it null, so ``/admin/runs`` showed a failed run with no reason, and so did every
list read from the index. The manifest now records each failure with its stage and a
stable code, and the index holds a one-line projection of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import kpubdata
import pytest
import yaml

from kpubdata_builder.manifest import run_failure_summary
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.routes import admin
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.build_index import rebuild_index

_CANARY = "canary-secret-1120"
_ADMIN = Principal(kind="oidc", identifier="admin123", owner_id="oidc:admin", is_admin=True)
_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    """A provider whose answer the test sets: rows, or an exception."""

    def __init__(self) -> None:
        self.answer: list[dict[str, JsonValue]] | Exception = [{"id": "1", "v": 1}]

    def list(self, **_params: object) -> _Result:
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Result(list(self.answer))

    def dataset(self, _key: str) -> _Source:
        return self


def _spec(schema: str = "") -> str:
    return (
        "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: m\n"
        + schema
        + "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )


def _service(tmp_path: Path, source: _Source, *, warehouse: bool = False) -> BuilderService:
    return BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: source,
        warehouse_root=tmp_path / "wh" if warehouse else None,
    )


def _manifest(tmp_path: Path, run_id: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((tmp_path / run_id / "manifest.json").read_text()))


def _admin_reasons(service: BuilderService) -> dict[str, Any]:
    response = admin.route(service, "GET", "/admin/runs", None, "", _ADMIN)
    assert response is not None and response.status_code == 200, response
    runs = cast(list[dict[str, Any]], cast(dict[str, Any], response.body)["runs"])
    return {run["run_id"]: run.get("error") for run in runs}


def _indexed(service: BuilderService) -> dict[str, str | None]:
    return {entry.run_id: entry.error for entry in service._build_index.list_builds(limit=None)}


def test_a_refused_source_is_recorded_with_its_stage_and_code(tmp_path: Path) -> None:
    source = _Source()
    source.answer = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    service = _service(tmp_path, source)

    assert service.build(_spec(), run_id="r1").status_code == 502

    (failure,) = _manifest(tmp_path, "r1")["failures"]
    assert failure["source_key"] == "m"
    assert failure["stage"] == "bronze"
    assert failure["code"] == "application_required"
    assert "활용신청" in failure["summary"]
    line = f"m: {failure['summary']}"
    assert _indexed(service)["r1"] == line
    assert _admin_reasons(service)["r1"] == line


def test_a_later_stage_failure_names_that_stage(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    missing = "    schema:\n      required: [not_there]\n"

    assert service.build(_spec(missing), run_id="r1").status_code == 502

    (failure,) = _manifest(tmp_path, "r1")["failures"]
    assert failure["stage"] == "silver"
    assert failure["code"] == "pipeline_failed"
    assert _indexed(service)["r1"] is not None


def test_a_refused_table_commit_is_a_failure_too(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source, warehouse=True)
    assert service.build(_spec(), run_id="r1").status_code == 200
    source.answer = []

    assert service.build(_spec(), run_id="r2").status_code == 409

    (failure,) = _manifest(tmp_path, "r2")["failures"]
    assert (failure["stage"], failure["code"]) == ("warehouse", "empty_result")
    assert _indexed(service)["r2"] == f"m: {failure['summary']}"
    assert _indexed(service)["r1"] is None


def test_a_successful_run_has_no_failures(tmp_path: Path) -> None:
    service = _service(tmp_path, _Source())

    assert service.build(_spec(), run_id="r1").status_code == 200

    assert "failures" not in _manifest(tmp_path, "r1")
    assert _indexed(service)["r1"] is None


def test_the_reason_survives_an_index_rebuild(tmp_path: Path) -> None:
    source = _Source()
    source.answer = kpubdata.RateLimitError("slow", provider="datago", provider_code="22")
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="r1").status_code == 502
    before = _indexed(service)["r1"]
    assert before is not None and "request limit" in before
    service._build_index.close()

    assert rebuild_index(tmp_path) == 1

    rebuilt = _service(tmp_path, source)
    assert _indexed(rebuilt)["r1"] == before
    assert _admin_reasons(rebuilt)["r1"] == before


def test_a_secret_in_an_error_reaches_neither_the_record_nor_the_lists(tmp_path: Path) -> None:
    source = _Source()
    source.answer = RuntimeError(f"connect to https://x.example/?key={_CANARY} failed")
    service = _service(tmp_path, source)

    assert service.build(_spec(), run_id="r1").status_code == 502

    manifest_text = (tmp_path / "r1" / "manifest.json").read_text()
    assert _CANARY not in manifest_text
    assert _CANARY not in str(_indexed(service))
    assert _CANARY not in str(_admin_reasons(service))
    (failure,) = _manifest(tmp_path, "r1")["failures"]
    assert failure["summary"] == "pipeline failed for source 'm'"


@pytest.mark.parametrize(
    ("manifest", "line"),
    [
        ({}, None),
        ({"errors": ["a: broke"]}, "a: broke"),
        ({"warehouse_failures": {"m": {"reason": "conflict", "detail": "newer"}}}, "m: newer"),
        (
            {
                "failures": [{"source_key": "a", "stage": "gold", "code": "x", "summary": "s"}],
                "errors": ["a: other"],
            },
            "a: s",
        ),
    ],
)
def test_the_summary_reads_older_manifests_too(
    manifest: dict[str, object], line: str | None
) -> None:
    assert run_failure_summary(manifest) == line


def test_the_contract_declares_the_failures_field() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    failures = contract["components"]["schemas"]["BuildManifest"]["properties"]["failures"]

    item = failures["items"]
    assert set(item["required"]) == {"source_key", "stage", "code", "summary"}

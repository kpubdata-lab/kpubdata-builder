"""A failed run says why, in its manifest and in the administrator's run list (#1120).

The build index has an ``error`` column that nothing wrote: a build and a rebuild both
left it null, so ``/admin/runs`` showed a failed run with no reason. The manifest now
records each failure with its stage and a stable code, and the index holds a one-line
projection of it.

``/admin/runs`` serves that line for every owner's runs, so it is made of fixed
sentences only. An error's own message — which can name the data's columns and, for a
failed join, a key's value — stays in the manifest's ``errors``, which only the run's
owner reads. Who may call ``/admin/runs`` is tested in ``test_admin_role.py``
(``TestAccessGate``).
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
_USER = Principal(kind="oidc", identifier="user1234", owner_id="oidc:user")
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
    assert failure["summary"] == "the source failed at the silver stage"
    assert _indexed(service)["r1"] == "m: the source failed at the silver stage"


def test_a_public_message_stays_with_the_owner(tmp_path: Path) -> None:
    """A validation error's message is public to the run's owner and names a column of
    the data. The owner reads it in ``errors``; the record an administrator is served
    from does not hold it."""
    service = _service(tmp_path, _Source())
    column = f"col_{_CANARY}".replace("-", "_")
    missing = f"    schema:\n      required: [{column}]\n"

    assert service.build(_spec(missing), run_id="r1").status_code == 502

    manifest = _manifest(tmp_path, "r1")
    assert column in str(manifest["errors"])
    assert column not in str(manifest["failures"])
    assert column not in str(_indexed(service))
    assert column not in str(_admin_reasons(service))


def test_a_failed_join_does_not_put_a_key_value_in_the_admin_list(tmp_path: Path) -> None:
    """A join refused for a repeated key names the key's value in its message. That
    value is a row's data: it reaches the owner's ``errors`` and nothing an
    administrator is served from, before or after a rebuild of the index."""
    source = _Source()
    source.answer = [{"id": _CANARY, "v": 1}, {"id": _CANARY, "v": 2}]
    service = _service(tmp_path, source)
    spec = (
        "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: a\n"
        "  - provider: datago\n    dataset: air_station\n    alias: b\n"
        "composition:\n  name: combined\n"
        "  join: {left: a, right: b, left_key: id, right_key: id, on_duplicate_key: fail}\n"
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )

    assert service.build(spec, run_id="r1").status_code == 502

    manifest = _manifest(tmp_path, "r1")
    assert _CANARY in str(manifest["errors"])
    (failure,) = manifest["failures"]
    assert (failure["source_key"], failure["stage"]) == ("combined", "composition")
    assert failure["code"] == "join_duplicate_key"
    assert _CANARY not in str(failure)
    line = f"combined: {failure['summary']}"
    assert _indexed(service)["r1"] == line
    assert _admin_reasons(service)["r1"] == line
    service._build_index.close()

    assert rebuild_index(tmp_path) == 1

    rebuilt = _service(tmp_path, source)
    assert _indexed(rebuilt)["r1"] == line
    assert _CANARY not in str(_admin_reasons(rebuilt))


def test_a_manifest_without_failures_keeps_its_errors_out_of_a_rebuilt_index(
    tmp_path: Path,
) -> None:
    """A manifest written before #1120 has ``errors`` and no ``failures``. A rebuild of
    the index gives such a run a fixed line, not the text of its ``errors``."""
    source = _Source()
    source.answer = [{"id": _CANARY, "v": 1}, {"id": _CANARY, "v": 2}]
    service = _service(tmp_path, source)
    spec = (
        "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
        "  - provider: datago\n    dataset: air_station\n    alias: a\n"
        "  - provider: datago\n    dataset: air_station\n    alias: b\n"
        "composition:\n  name: combined\n"
        "  join: {left: a, right: b, left_key: id, right_key: id, on_duplicate_key: fail}\n"
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
    )
    assert service.build(spec, run_id="r1").status_code == 502
    service._build_index.close()
    path = tmp_path / "r1" / "manifest.json"
    older = _manifest(tmp_path, "r1")
    del older["failures"]
    assert _CANARY in str(older["errors"])
    path.write_text(json.dumps(older), encoding="utf-8")

    assert rebuild_index(tmp_path) == 1

    rebuilt = _service(tmp_path, source)
    reason = _indexed(rebuilt)["r1"]
    assert reason is not None and "before failure reasons were recorded" in reason
    assert _CANARY not in str(_admin_reasons(rebuilt))


def test_the_owner_facing_build_list_does_not_return_the_reason(tmp_path: Path) -> None:
    source = _Source()
    source.answer = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="r1").status_code == 502
    assert _indexed(service)["r1"] is not None

    response = service.list_builds()

    assert response.status_code == 200
    (build,) = cast(list[dict[str, Any]], cast(dict[str, Any], response.body)["builds"])
    assert build["run_id"] == "r1"
    assert "error" not in build


def test_a_user_who_is_not_an_administrator_is_refused_the_reasons(tmp_path: Path) -> None:
    source = _Source()
    source.answer = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    service = _service(tmp_path, source)
    assert service.build(_spec(), run_id="r1").status_code == 502

    response = admin.route(service, "GET", "/admin/runs", None, "", _USER)

    assert response is not None and response.status_code == 403
    assert "r1" not in str(response.body)


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
    assert failure["summary"] == "the source failed at the bronze stage"


@pytest.mark.parametrize(
    ("manifest", "line"),
    [
        ({}, None),
        (
            {"errors": ["a: broke"]},
            "the run failed; its manifest was written before failure reasons were recorded",
        ),
        (
            {"warehouse_failures": {"m": {"reason": "conflict", "detail": "newer"}}},
            "m: the table was not committed (conflict)",
        ),
        (
            {"warehouse_failures": {"m": {"reason": "rows: 1, 2", "detail": "newer"}}},
            "m: the table was not committed",
        ),
        (
            {
                "failures": [{"source_key": "a", "stage": "gold", "code": "x", "summary": "s"}],
                "errors": ["a: other"],
            },
            "a: s",
        ),
    ],
)
def test_the_summary_of_an_older_manifest_copies_none_of_its_text(
    manifest: dict[str, object], line: str | None
) -> None:
    assert run_failure_summary(manifest) == line


def test_the_contract_declares_the_failures_field() -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    failures = contract["components"]["schemas"]["BuildManifest"]["properties"]["failures"]

    item = failures["items"]
    assert set(item["required"]) == {"source_key", "stage", "code", "summary"}

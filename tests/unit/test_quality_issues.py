"""GET /quality/issues lists actionable findings across tables in one call (#843).

Studio's Quality Center called ``GET /builds/{run_id}/quality`` once per table and could
not see past the first 100 tables.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"


class _Result:
    def __init__(self) -> None:
        self.items = [{"id": "1", "pm10": 30}, {"id": "2", "pm10": None}]


class _Dataset:
    def list(self, **_params: object) -> _Result:
        return _Result()


class _Client:
    def dataset(self, _key: str) -> _Dataset:
        return _Dataset()


def _spec(dataset_id: str, quality: str = "") -> str:
    return (
        f"dataset_id: {dataset_id}\ntitle: T {dataset_id}\ndescription: d\n"
        "sources:\n  - provider: datago\n    dataset: air_quality\n"
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n" + quality
    )


_WARN = "quality:\n  min_rows: 100\n"
_FAIL = "quality:\n  min_rows: 100\n  min_rows_severity: fail\n"
_PASS = "quality:\n  min_rows: 1\n"


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_: _Client())


def _get(service: BuilderService, query: str = "") -> ServiceResponse:
    response = dispatch(service, "GET", "/quality/issues", None, query)
    assert isinstance(response, ServiceResponse)
    return response


def _issues(response: ServiceResponse) -> list[dict[str, JsonValue]]:
    assert response.status_code == 200, response.body
    return cast(list[dict[str, JsonValue]], response.body["issues"])


def _build(service: BuilderService, dataset_id: str, quality: str, run_id: str) -> None:
    assert service.build(_spec(dataset_id, quality), run_id=run_id).status_code in (200, 502)


def test_warn_and_fail_from_every_table_failures_first(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _build(service, "a.warn", _WARN, "r-warn")
    _build(service, "b.fail", _FAIL, "r-fail")
    _build(service, "c.pass", _PASS, "r-pass")

    issues = _issues(_get(service))

    assert [(i["dataset_id"], i["status"]) for i in issues] == [
        ("b.fail", "fail"),
        ("a.warn", "warn"),
    ]
    first = issues[0]
    assert (first["title"], first["run_id"], first["kind"]) == ("T b.fail", "r-fail", "check")
    check = cast(dict[str, JsonValue], first["check"])
    assert (check["rule"], check["status"]) == ("min_rows", "fail")


def test_only_the_latest_run_of_a_table_counts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _build(service, "a", _FAIL, "old")
    _build(service, "a", _PASS, "new")

    assert _issues(_get(service)) == []


def test_filters_and_paging(tmp_path: Path) -> None:
    service = _service(tmp_path)
    for index in range(3):
        _build(service, f"t{index}", _WARN, f"r{index}")
    _build(service, "x", _FAIL, "rx")

    assert {i["dataset_id"] for i in _issues(_get(service, "status=warn"))} == {"t0", "t1", "t2"}
    assert [i["dataset_id"] for i in _issues(_get(service, "dataset_id=x"))] == ["x"]
    assert _issues(_get(service, "category=nothing")) == []

    first = _get(service, "limit=2")
    second = _get(service, f"limit=2&cursor={first.body['next_cursor']}")
    assert first.body["total"] == 4
    seen = [i["run_id"] for i in _issues(first) + _issues(second)]
    assert sorted(seen) == ["r0", "r1", "r2", "rx"]
    assert second.body["next_cursor"] is None


def test_unevaluated_tables_are_counted_not_passed(tmp_path: Path) -> None:
    """Negative: a table without checks is `not_evaluated`, never quietly clean."""
    service = _service(tmp_path)
    _build(service, "plain", "", "r-plain")
    _build(service, "checked", _PASS, "r-checked")

    response = _get(service)

    assert _issues(response) == []
    assert response.body["coverage"] == {
        "tables": 2,
        "evaluated": 1,
        "not_evaluated": 1,
        "partial": 0,
        "unreadable": 0,
    }


def test_schema_drift_is_listed_but_never_a_failure(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _build(service, "d", _PASS, "r-d")
    manifest_path = tmp_path / "r-d" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    (source_key,) = manifest["quality_results"]
    manifest["schema_drift"] = {
        source_key: [{"kind": "column_added", "column": "new_col", "detail": "added"}]
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    (issue,) = _issues(_get(service))
    assert (issue["kind"], issue["status"], issue["category"]) == ("drift", "drift", "schema_drift")
    assert _issues(_get(service, "status=fail,warn")) == []


def test_another_owners_tables_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    service = _service(tmp_path)
    assert service.build(
        _spec("bob.table", _FAIL), run_id="bob-1", owner_id="oidc:bob", manifest_owner_id="oidc:bob"
    ).status_code in (200, 502)
    alice = Principal("oidc", "alice", "oidc:alice")

    response = service.list_quality_issues(principal=alice)

    assert response.body["issues"] == []
    assert cast(dict[str, int], response.body["coverage"])["tables"] == 0


@pytest.mark.parametrize(
    "query",
    ["status=bad", "dataset_id=", "category=", "limit=0", "limit=501", "cursor=x", "cursor=-1"],
)
def test_bad_parameters_are_400(tmp_path: Path, query: str) -> None:
    assert _get(_service(tmp_path), query).status_code == 400


def test_the_response_matches_the_contract(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _build(service, "b.fail", _FAIL, "r-fail")
    _build(service, "plain", "", "r-plain")

    response = _get(service)

    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/quality/issues", "get", 200)
    assert schema is not None
    assert validate(response.body, schema, contract) == []

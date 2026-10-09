"""A build can ask that its tables be new, and Builder holds it to that (#1223).

Studio picks a free dataset id and checks again before submitting, but both are the
client's "check, then write": another tab can make the table in between, and the build
then replaces it. ``if_absent`` moves the check to the commit.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
_DEV = Principal("dev")
_SPEC = (
    "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_station\n    alias: m\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    def __init__(self) -> None:
        self.rows: list[dict[str, JsonValue]] = [{"id": "1", "v": 1}]
        self.during_fetch: Any = None

    def list(self, **_params: object) -> _Result:
        hook, self.during_fetch = self.during_fetch, None
        if hook is not None:
            hook()
        return _Result(list(self.rows))

    def dataset(self, _key: str) -> _Source:
        return self


def _service(tmp_path: Path, source: _Source) -> BuilderService:
    return BuilderService(
        output_root=tmp_path, client_factory=lambda **_: source, warehouse_root=tmp_path / "wh"
    )


def _post(service: BuilderService, path: str, body: dict[str, JsonValue]) -> ServiceResponse:
    response = dispatch(service, "POST", path, cast(JsonValue, body), "")
    assert isinstance(response, ServiceResponse)
    return response


def _table(service: BuilderService) -> dict[str, Any]:
    response = service.get_warehouse_table("air.m", principal=_DEV)
    assert response.status_code == 200, response.body
    return cast(dict[str, Any], response.body)


def test_a_new_table_build_is_refused_where_the_table_exists(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert _post(service, "/build", {"spec": _SPEC, "run_id": "first"}).status_code == 200
    source.rows = [{"id": "2", "v": 2}]

    response = _post(service, "/build", {"spec": _SPEC, "run_id": "second", "if_absent": True})

    assert response.status_code == 409, response.body
    failure = cast(dict[str, Any], response.body)["warehouse_failures"]["m"]
    assert failure["reason"] == "table_exists"
    table = _table(service)
    assert table["revision"] == 1
    assert [s["run_id"] for s in table["snapshots"]] == ["first"]
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, "/build", "post", 409)
    assert schema is not None
    assert validate(response.body, schema, contract) == []


def test_a_new_table_build_commits_where_there_is_none(tmp_path: Path) -> None:
    service = _service(tmp_path, _Source())

    response = _post(service, "/build", {"spec": _SPEC, "run_id": "first", "if_absent": True})

    assert response.status_code == 200, response.body
    assert _table(service)["revision"] == 1


def test_a_table_made_while_the_build_ran_is_not_committed_over(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)

    def another_tab() -> None:
        # The table does not exist when this build starts; another build makes it
        # while this one is fetching.
        assert _post(service, "/build", {"spec": _SPEC, "run_id": "other"}).status_code == 200

    source.during_fetch = another_tab

    response = _post(service, "/build", {"spec": _SPEC, "run_id": "mine", "if_absent": True})

    assert response.status_code == 409, response.body
    assert cast(dict[str, Any], response.body)["warehouse_failures"]["m"]["reason"] == (
        "table_exists"
    )
    assert [s["run_id"] for s in _table(service)["snapshots"]] == ["other"]


def test_without_if_absent_a_build_still_refreshes_the_table(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert _post(service, "/build", {"spec": _SPEC, "run_id": "first"}).status_code == 200

    assert _post(service, "/build", {"spec": _SPEC, "run_id": "second"}).status_code == 200

    assert _table(service)["revision"] == 2


def test_the_async_route_carries_if_absent_to_the_worker(tmp_path: Path) -> None:
    source = _Source()
    service = _service(tmp_path, source)
    assert _post(service, "/build", {"spec": _SPEC, "run_id": "first"}).status_code == 200

    accepted = _post(service, "/builds", {"spec": _SPEC, "run_id": "job", "if_absent": True})
    assert accepted.status_code == 202, accepted.body
    done = threading.Event()
    for _ in range(400):
        job = cast(dict[str, Any], service.build_status("job").body)
        if job["status"] in ("succeeded", "failed", "cancelled"):
            break
        done.wait(0.025)

    assert job["status"] == "failed"
    response = cast(dict[str, Any], job["response"])
    assert response["warehouse_failures"]["m"]["reason"] == "table_exists"
    assert _table(service)["revision"] == 1


def test_if_absent_must_be_a_boolean(tmp_path: Path) -> None:
    service = _service(tmp_path, _Source())

    for path in ("/build", "/builds"):
        response = _post(service, path, {"spec": _SPEC, "if_absent": "yes"})

        assert response.status_code == 400, (path, response.body)
        assert "if_absent" in str(response.body)

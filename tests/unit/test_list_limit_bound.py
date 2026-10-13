"""History listing ``limit`` is bounded before any persisted run is read (#1327).

``GET /builds``, ``GET /datasets``, ``GET /datasets/{id}/runs`` and
``GET /datasets/{id}/quality/history`` take a ``limit``. A limit above the documented
maximum, or one that is not a positive integer, is refused with 400 before the listing
method is called, so it cannot make the service enumerate or answer with an unbounded
number of runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.routes._parsing import MAX_LIST_LIMIT
from kpubdata_builder.spec import JsonValue

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

_LISTING_ROUTES = (
    ("/builds", "list_builds"),
    ("/datasets", "list_datasets"),
    ("/datasets/d.a/runs", "list_dataset_runs"),
    ("/datasets/d.a/quality/history", "get_dataset_quality_history"),
)


def _no_client(**_: object) -> object:
    raise AssertionError("a listing must not reach a provider")


def _instrumented(tmp_path: Path, method_name: str) -> tuple[BuilderService, list[int]]:
    """A service whose listing method records the limit it was asked for."""
    service = BuilderService(output_root=tmp_path, client_factory=_no_client)
    calls: list[int] = []

    def _listing(*args: Any, limit: int, **kwargs: Any) -> ServiceResponse:
        del args, kwargs
        calls.append(limit)
        return ServiceResponse(200, {"builds": [], "datasets": [], "runs": []})

    setattr(service, method_name, _listing)
    return service, calls


@pytest.mark.parametrize(("path", "method_name"), _LISTING_ROUTES)
@pytest.mark.parametrize(
    "raw",
    [str(MAX_LIST_LIMIT + 1), "1000000000000", "0", "-1", "abc", "1.5", "true"],
)
def test_out_of_range_query_limit_never_reaches_listing(
    tmp_path: Path, path: str, method_name: str, raw: str
) -> None:
    service, calls = _instrumented(tmp_path, method_name)
    resp = dispatch(service, "GET", path, None, query=f"limit={raw}")
    assert resp.status_code == 400
    assert resp.body == {"error": f"'limit' must be a positive integer up to {MAX_LIST_LIMIT}"}
    assert calls == []


@pytest.mark.parametrize("value", [MAX_LIST_LIMIT + 1, 10**12, 0, -1, True, "5", 1.5, None])
def test_out_of_range_body_limit_never_reaches_list_builds(
    tmp_path: Path, value: JsonValue
) -> None:
    service, calls = _instrumented(tmp_path, "list_builds")
    resp = dispatch(service, "GET", "/builds", {"limit": value})
    assert resp.status_code == 400
    assert calls == []


@pytest.mark.parametrize(("path", "method_name"), _LISTING_ROUTES)
def test_limit_at_maximum_is_passed_through(tmp_path: Path, path: str, method_name: str) -> None:
    service, calls = _instrumented(tmp_path, method_name)
    resp = dispatch(service, "GET", path, None, query=f"limit={MAX_LIST_LIMIT}")
    assert resp.status_code == 200
    assert calls == [MAX_LIST_LIMIT]


@pytest.mark.parametrize(
    ("path", "method_name", "default"),
    [
        ("/builds", "list_builds", 50),
        ("/datasets", "list_datasets", 50),
        ("/datasets/d.a/runs", "list_dataset_runs", 50),
        ("/datasets/d.a/quality/history", "get_dataset_quality_history", 30),
    ],
)
def test_default_limit_is_unchanged(
    tmp_path: Path, path: str, method_name: str, default: int
) -> None:
    service, calls = _instrumented(tmp_path, method_name)
    resp = dispatch(service, "GET", path, None)
    assert resp.status_code == 200
    assert calls == [default]


def test_build_index_is_asked_for_at_most_the_maximum(tmp_path: Path) -> None:
    """The persisted-run listing never receives more than the bound, and answers within it."""
    service = BuilderService(output_root=tmp_path, client_factory=_no_client)
    asked: list[int | None] = []
    original = service._build_index.list_builds

    def _spy(limit: int | None = 50) -> Any:
        asked.append(limit)
        return original(limit=limit)

    service._build_index.list_builds = _spy
    refused = dispatch(service, "GET", "/builds", None, query="limit=99999999")
    assert refused.status_code == 400
    assert asked == []
    accepted = dispatch(service, "GET", "/builds", None, query=f"limit={MAX_LIST_LIMIT}")
    assert accepted.status_code == 200
    assert asked == [MAX_LIST_LIMIT]
    builds = accepted.body["builds"]
    assert isinstance(builds, list)
    assert len(builds) <= MAX_LIST_LIMIT


@pytest.mark.parametrize(
    "path",
    [
        "/builds",
        "/datasets",
        "/datasets/{dataset_id}/runs",
        "/datasets/{dataset_id}/quality/history",
    ],
)
def test_contract_declares_the_maximum(path: str) -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    operation = contract["paths"][path]["get"]
    (limit,) = [p for p in operation["parameters"] if p.get("name") == "limit"]
    assert limit["schema"]["maximum"] == MAX_LIST_LIMIT

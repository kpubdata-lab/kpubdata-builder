"""An admin's job row never carries the job's own error message (#1221).

``GET /admin/runs`` lists async jobs from the registry as well as finished runs from
the build index; the index row wins for the same run. When the index has no entry for
a run — its best-effort write failed, or it is past the requested limit — the job row
is the one served, and it carried the job's ``error``: for a failed build, the first
failed source's or composition's message, which names a join key's value.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, cast

import kpubdata
import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.build_runs_api import SHUTDOWN_QUEUED_ERROR
from kpubdata_builder.service.jobs import BuildJobSnapshot
from kpubdata_builder.service.routes import admin
from kpubdata_builder.spec import JsonValue

_CANARY = "canary-join-key-1221"
_ADMIN = Principal(kind="oidc", identifier="admin123", owner_id="oidc:admin", is_admin=True)
_JOIN_SPEC = (
    "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_station\n    alias: a\n"
    "  - provider: datago\n    dataset: air_station\n    alias: b\n"
    "composition:\n  name: combined\n"
    "  join: {left: a, right: b, left_key: id, right_key: id, on_duplicate_key: fail}\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)
_SPEC = (
    "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_station\n    alias: m\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    def __init__(self, answer: list[dict[str, JsonValue]] | Exception) -> None:
        self.answer = answer

    def list(self, **_params: object) -> _Result:
        if isinstance(self.answer, Exception):
            raise self.answer
        return _Result(list(self.answer))

    def dataset(self, _key: str) -> _Source:
        return self


def _no_index(service: BuilderService, monkeypatch: pytest.MonkeyPatch) -> None:
    """The index write after the build fails, as it may (it is best-effort)."""

    def refuse(**_kwargs: object) -> None:
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(service._build_index, "insert_or_replace", refuse)


def _run_async(service: BuilderService, spec: str, run_id: str) -> dict[str, Any]:
    assert service.submit_build(spec, run_id=run_id, created_by="oidc:alice").status_code == 202
    done = threading.Event()
    for _ in range(400):
        body = cast(dict[str, Any], service.build_status(run_id).body)
        if body.get("status") in ("succeeded", "failed", "cancelled"):
            return body
        done.wait(0.025)
    raise AssertionError(f"{run_id} did not end")


def _admin_rows(service: BuilderService) -> dict[str, dict[str, Any]]:
    response = admin.route(service, "GET", "/admin/runs", None, "", _ADMIN)
    assert response is not None and response.status_code == 200, response
    runs = cast(list[dict[str, Any]], cast(dict[str, Any], response.body)["runs"])
    return {run["run_id"]: run for run in runs}


def test_a_join_key_value_in_the_job_error_stays_out_of_the_admin_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _Source([{"id": _CANARY, "v": 1}, {"id": _CANARY, "v": 2}])
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: source)
    _no_index(service, monkeypatch)

    owner_view = _run_async(service, _JOIN_SPEC, "r1")

    # The owner keeps the full message.
    assert owner_view["status"] == "failed"
    assert _CANARY in str(owner_view["error"])
    assert service._build_index.list_builds(limit=None) == []
    row = _admin_rows(service)["r1"]
    assert row["status"] == "failed"
    assert row["error"] == "combined: the composition failed"
    assert _CANARY not in str(_admin_rows(service))


def test_a_refused_source_gets_its_fixed_sentence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refused = kpubdata.AuthError("refused", provider="datago", provider_code="30")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source(refused))
    _no_index(service, monkeypatch)

    _run_async(service, _SPEC, "r1")

    line = _admin_rows(service)["r1"]["error"]
    assert line.startswith("m: the provider refused this key for this dataset")


def test_any_other_source_failure_is_named_by_its_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = RuntimeError(f"column {_CANARY} broke")
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: _Source(failing))
    _no_index(service, monkeypatch)

    _run_async(service, _SPEC, "r1")

    assert _admin_rows(service)["r1"]["error"] == "m: the source failed at the bronze stage"


@pytest.mark.parametrize(
    ("error", "line"),
    [
        ("credentials_required: the keys were dropped", "credentials_required"),
        (SHUTDOWN_QUEUED_ERROR, "the server shut down before this job started"),
        ("internal error: KeyError", "the build failed with an internal error"),
        (f"something about {_CANARY}", "the build failed"),
    ],
)
def test_a_job_without_a_build_response_gets_a_fixed_line(error: str, line: str) -> None:
    job = BuildJobSnapshot(
        run_id="r1", status="failed", created_at="t", updated_at="t", error=error
    )

    shown = admin._job_failure_line(job)

    assert shown is not None and line in shown
    assert _CANARY not in shown


def test_a_job_that_did_not_fail_has_no_line() -> None:
    job = BuildJobSnapshot(run_id="r1", status="running", created_at="t", updated_at="t")

    assert admin._job_failure_line(job) is None

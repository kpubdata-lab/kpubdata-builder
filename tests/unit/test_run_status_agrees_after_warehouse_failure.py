"""A run whose table was not committed reads the same wherever it is listed (#1106).

A build can produce every artifact and still fail: its table commit is refused
(``warehouse_failures``, #788). The request answered 409 and ``GET /builds/{run_id}``
said ``failed`` (#997), while ``GET /builds`` and ``GET /admin/runs`` showed the same
run as ``ok`` — they read the manifest's build status alone, from the index or from the
filesystem.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import kpubdata_builder.pipeline.orchestrator as orchestrator
from kpubdata_builder.manifest import run_status_from_manifest, status_from_manifest
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import _DEV_MODE_ENV
from kpubdata_builder.store import rebuild_index
from kpubdata_builder.warehouse import SnapshotStateError

from .test_warehouse_commit_races import _Client

_SPEC = """\
dataset_id: race.table
title: Race
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""


def _service(root: Path) -> BuilderService:
    return BuilderService(
        output_root=root / "runs",
        client_factory=lambda **_: _Client("x"),
        warehouse_root=root / "wh",
    )


def _get(service: BuilderService, path: str) -> ServiceResponse:
    response = dispatch(service, "GET", path, None)
    assert isinstance(response, ServiceResponse) and response.status_code == 200, response
    return response


def _listed(service: BuilderService) -> dict[str, str]:
    return {b["run_id"]: b["status"] for b in _get(service, "/builds").body["builds"]}


def _admin_listed(service: BuilderService) -> dict[str, str]:
    return {r["run_id"]: r["status"] for r in _get(service, "/admin/runs").body["runs"]}


def _detail(service: BuilderService, run_id: str) -> str:
    return str(_get(service, f"/builds/{run_id}").body["status"])


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three finished runs: a synchronous and an asynchronous one whose table commit was
    refused, and one built before the commit began to fail."""
    monkeypatch.setenv(_DEV_MODE_ENV, "true")
    (tmp_path / "runs").mkdir()
    service = _service(tmp_path)
    plain = dispatch(service, "POST", "/build", {"spec": _SPEC, "run_id": "plain"})
    assert isinstance(plain, ServiceResponse) and plain.status_code == 200, plain.body

    def refused(*_args: object, **_kwargs: object) -> object:
        raise SnapshotStateError("the promoted files do not match their digest")

    monkeypatch.setattr(orchestrator, "materialize", refused)
    sync = dispatch(service, "POST", "/build", {"spec": _SPEC, "run_id": "sync"})
    assert isinstance(sync, ServiceResponse) and sync.status_code == 409
    assert sync.body["warehouse_failures"]
    submitted = dispatch(service, "POST", "/builds", {"spec": _SPEC, "run_id": "async"})
    assert isinstance(submitted, ServiceResponse) and submitted.status_code == 202
    deadline = time.monotonic() + 10
    while service.build_status("async").body["status"] not in ("succeeded", "failed"):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    service._async_builds.shutdown()
    return tmp_path


_EXPECTED = {"plain": "ok", "sync": "failed", "async": "failed"}


def test_the_list_says_what_the_detail_says_while_the_service_runs(root: Path) -> None:
    service = _service(root)

    assert _listed(service) == _EXPECTED
    assert _admin_listed(service) == _EXPECTED
    assert {run: _detail(service, run) for run in _EXPECTED} == {
        "plain": "succeeded",
        "sync": "failed",
        "async": "failed",
    }


def test_it_says_the_same_from_the_filesystem_when_there_is_no_index(root: Path) -> None:
    """The fallback path: a deployment whose index file was lost."""
    for leftover in (root / "runs").glob("_builds.sqlite*"):
        leftover.unlink()
    service = _service(root)

    assert _listed(service) == _EXPECTED
    assert {run: _detail(service, run) for run in ("sync", "async")} == {
        "sync": "failed",
        "async": "failed",
    }


def test_a_rebuilt_index_says_the_same(root: Path) -> None:
    for leftover in (root / "runs").glob("_builds.sqlite*"):
        leftover.unlink()

    assert rebuild_index(root / "runs") == 3
    service = _service(root)

    assert _listed(service) == _EXPECTED
    assert _admin_listed(service) == _EXPECTED


def test_rebuilding_corrects_a_row_written_before_the_fix(root: Path) -> None:
    """An index from before #1106 holds ``ok`` for such a run; the index is derived, and
    ``rebuild-index`` reads the manifests again."""
    service = _service(root)
    entry = service._build_index.get("sync")
    assert entry is not None
    service._build_index.insert_or_replace(
        run_id="sync",
        status="ok",
        started_at=entry.started_at,
        finished_at=entry.finished_at,
        spec_digest=entry.spec_digest,
        created_by=entry.created_by,
        owner_id=entry.owner_id,
        dataset_id=entry.dataset_id,
    )
    assert _listed(service)["sync"] == "ok"
    service._build_index.close()

    rebuild_index(root / "runs")

    assert _listed(_service(root)) == _EXPECTED


def test_the_build_itself_is_still_recorded_as_whole(root: Path) -> None:
    """Negative: only what a caller is told changes. The manifest still says the build
    produced its artifacts, and records the commit failure apart — publishing, retention
    and the drift baseline read that."""
    for run_id, outcome in _EXPECTED.items():
        manifest = json.loads((root / "runs" / run_id / "manifest.json").read_text("utf-8"))
        assert status_from_manifest(manifest) == "ok", run_id
        assert run_status_from_manifest(manifest) == outcome, run_id
        assert bool(manifest.get("warehouse_failures")) is (outcome == "failed"), run_id
        assert (root / "runs" / run_id / "gold").is_dir()


@pytest.mark.parametrize(
    ("manifest", "outcome"),
    [
        ({"status": "ok"}, "ok"),
        ({"status": "ok", "warehouse_failures": {}}, "ok"),
        ({"status": "ok", "warehouse_failures": {"s": {"reason": "conflict"}}}, "failed"),
        ({"warehouse_failures": {"s": {"reason": "conflict"}}}, "failed"),  # legacy: no status
        ({"status": "failed", "warehouse_failures": {"s": {"reason": "x"}}}, "failed"),
        ({"status": "cancelled", "warehouse_failures": {"s": {"reason": "x"}}}, "cancelled"),
        ({"status": "cancelled"}, "cancelled"),
        ({"errors": ["boom"]}, "failed"),
        ({}, "ok"),
    ],
)
def test_one_reading_of_a_manifests_outcome(manifest: dict[str, object], outcome: str) -> None:
    assert run_status_from_manifest(manifest) == outcome

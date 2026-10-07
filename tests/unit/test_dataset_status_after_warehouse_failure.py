"""A refresh whose table commit was refused is not a successful refresh of the dataset
(#1123).

Such a run produced every artifact, so its manifest says ``ok`` and records the refused
commit in ``warehouse_failures`` (#788). #1106 made the run lists say ``failed``; the
dataset views still read the manifest's build status, so the dataset's ``refresh`` was
``succeeded`` and its health counted the run as the last success — for a table that had
not changed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import kpubdata_builder.pipeline.orchestrator as orchestrator
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import _DEV_MODE_ENV
from kpubdata_builder.warehouse import SnapshotStateError

from .test_run_status_agrees_after_warehouse_failure import _SPEC, _get, _service

#: A table refreshed once and then refused, and one whose only refresh was refused. Both
#: declare a cadence, so ``health`` shows which run is counted as the last success.
_REFRESHED = _SPEC + "refresh_cadence: P1D\n"
_NEVER = _REFRESHED.replace("race.table", "race.never")


def _build(service: BuilderService, spec: str, run_id: str, status: int) -> ServiceResponse:
    response = dispatch(service, "POST", "/build", {"spec": spec, "run_id": run_id})
    assert isinstance(response, ServiceResponse) and response.status_code == status, response.body
    return response


@pytest.fixture()
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(_DEV_MODE_ENV, "true")
    (tmp_path / "runs").mkdir()
    service = _service(tmp_path)
    _build(service, _REFRESHED, "committed", 200)

    def refused(*_args: object, **_kwargs: object) -> object:
        raise SnapshotStateError("the promoted files do not match their digest")

    monkeypatch.setattr(orchestrator, "materialize", refused)
    assert _build(service, _REFRESHED, "refused", 409).body["warehouse_failures"]
    assert _build(service, _NEVER, "never", 409).body["warehouse_failures"]
    return tmp_path


def _listed(service: BuilderService) -> dict[str, dict[str, object]]:
    return {item["dataset_id"]: item for item in _get(service, "/datasets").body["datasets"]}


def _axes(summary: dict[str, object]) -> dict[str, object]:
    axes = summary["status_axes"]
    assert isinstance(axes, dict)
    return {name: axes[name] for name in ("refresh", "completeness", "health")}


def _check(service: BuilderService) -> None:
    listed = _listed(service)

    refreshed = listed["race.table"]
    assert refreshed["latest_run_id"] == "refused"
    assert refreshed["status"] == "failed"
    # The table is what ``committed`` left: that run is the last success, and it is
    # inside the cadence. What this run wrote says nothing about the table's rows.
    assert _axes(refreshed) == {
        "refresh": "failed",
        "completeness": "unknown",
        "health": "healthy",
    }

    never = listed["race.never"]
    assert never["status"] == "failed"
    assert _axes(never) == {"refresh": "failed", "completeness": "unknown", "health": "unknown"}

    for dataset_id, summary in listed.items():
        detail = _get(service, f"/datasets/{dataset_id}").body
        assert detail["status"] == summary["status"], dataset_id
        assert _axes(detail) == _axes(summary), dataset_id


def test_the_dataset_says_the_refresh_failed_in_the_list_and_the_detail(root: Path) -> None:
    _check(_service(root))


def test_it_says_the_same_from_the_filesystem_when_there_is_no_index(root: Path) -> None:
    for leftover in (root / "runs").glob("_builds.sqlite*"):
        leftover.unlink()

    _check(_service(root))


def test_the_datasets_runs_agree_with_the_build_list(root: Path) -> None:
    service = _service(root)

    builds = {b["run_id"]: b["status"] for b in _get(service, "/builds").body["builds"]}
    runs = {
        r["run_id"]: r["status"] for r in _get(service, "/datasets/race.table/runs").body["runs"]
    }

    assert runs == {"committed": "ok", "refused": "failed"}
    assert all(builds[run_id] == status for run_id, status in runs.items())


def test_a_committed_refresh_still_reads_as_succeeded_and_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: only a refused commit changes the reading."""
    monkeypatch.setenv(_DEV_MODE_ENV, "true")
    (tmp_path / "runs").mkdir()
    service = _service(tmp_path)
    _build(service, _REFRESHED, "committed", 200)

    summary = _listed(service)["race.table"]

    assert summary["status"] == "ok"
    assert _axes(summary) == {
        "refresh": "succeeded",
        "completeness": "complete",
        "health": "healthy",
    }

"""Expired runs are collected safely, and a build needs room to start (#1099).

``prune-runs`` removes the workspace of a succeeded or failed run past its retention,
and never one that is running, retried, published, or kept by the warehouse — held,
leased or merely committed. Each guarantee has a test here, and so does a pass that
dies midway. ``POST /build`` and ``POST /builds`` refuse a build when the disk is low
or the owner's runs, with their reservations, would pass the owner's limit.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder import retention
from kpubdata_builder.cli import main
from kpubdata_builder.events import BuildEvent
from kpubdata_builder.events.store import BuildEventStore
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.retention import RUN_GC_DIRNAME, RUN_RETENTION_DAYS_ENV, collect_expired_runs
from kpubdata_builder.service import BuilderService, ServiceResponse, run_storage
from kpubdata_builder.service.build_limits import MAX_ACTIVE_BUILDS_PER_OWNER_ENV
from kpubdata_builder.service.jobs import RunCancellation
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.service.publish import PublishReceiptStore
from kpubdata_builder.service.readiness import DiskUsage
from kpubdata_builder.service.run_storage import (
    BUILD_MIN_FREE_MB_ENV,
    RUN_STORAGE_MAX_BYTES_ENV,
    RUN_STORAGE_RESERVE_BYTES_ENV,
)
from kpubdata_builder.store import SqliteBuildIndex, rebuild_index
from kpubdata_builder.store.state_lock import state_lock
from kpubdata_builder.warehouse import SnapshotLayout, SnapshotManifest, TableCatalog

from ._openapi import response_schema, validate

_CONTRACT = Path(__file__).parents[2] / "contract" / "builder-api.yaml"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
OLD = NOW - timedelta(days=40)
RECENT = NOW - timedelta(days=2)


def _run(
    root: Path,
    run_id: str,
    *,
    status: str = "ok",
    finished: datetime | None = OLD,
    size: int = 1000,
    owner: str | None = None,
) -> Path:
    """A run workspace with a manifest and ``size`` bytes of artifacts."""
    run_dir = root / run_id
    (run_dir / "gold").mkdir(parents=True)
    (run_dir / "gold" / "part-0.parquet").write_bytes(b"x" * size)
    manifest: dict[str, object] = {"run_id": run_id, "status": status}
    if status == "failed":
        manifest["errors"] = ["boom"]
    if finished is not None:
        manifest["finished_at"] = finished.isoformat()
    if owner is not None:
        manifest["owner_id"] = owner
        manifest["created_by"] = f"oidc:{owner}"
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


def _indexed(root: Path) -> set[str]:
    index = SqliteBuildIndex(root)
    try:
        return {entry.run_id for entry in index.list_builds(limit=None)}
    finally:
        index.close()


def _event(store: BuildEventStore, run_id: str, event: str) -> None:
    store.append(
        BuildEvent(
            seq=0,
            timestamp=NOW,
            run_id=run_id,
            event=cast(Any, event),
            status="ok",
            message=event,
        )
    )


def _collect(root: Path, **kwargs: Any) -> retention.RunGcReport:
    kwargs.setdefault("retention_days", 30)
    return collect_expired_runs(root, now=NOW, **kwargs)


# ------------------------------------------------------------------------ dry run


def test_a_dry_run_lists_what_would_go_and_the_space_it_frees(tmp_path: Path) -> None:
    _run(tmp_path, "old-ok", size=1000)
    _run(tmp_path, "old-failed", status="failed", size=500)
    _run(tmp_path, "young", finished=RECENT)
    rebuild_index(tmp_path)

    report = _collect(tmp_path)

    assert {run.run_id for run in report.expired} == {"old-ok", "old-failed"}
    assert report.freed_bytes >= 1500
    assert report.retained == 1
    assert report.deleted == ()
    assert (tmp_path / "old-ok").is_dir() and (tmp_path / "old-failed").is_dir()
    assert _indexed(tmp_path) == {"old-ok", "old-failed", "young"}


@pytest.mark.parametrize("days", [None, 0])
def test_without_a_retention_nothing_expires(tmp_path: Path, days: int | None) -> None:
    _run(tmp_path, "ancient", finished=NOW - timedelta(days=3650))

    report = _collect(tmp_path, retention_days=days, apply=True)

    assert report.expired == () and report.deleted == ()
    assert (tmp_path / "ancient").is_dir()


def test_age_is_the_manifest_finish_time_never_the_file_time(tmp_path: Path) -> None:
    young = _run(tmp_path, "young", finished=RECENT)
    no_time = _run(tmp_path, "no-time", finished=None)
    ancient = (NOW - timedelta(days=3650)).timestamp()
    for path in (young, no_time, young / "manifest.json", no_time / "manifest.json"):
        os.utime(path, (ancient, ancient))

    report = _collect(tmp_path, apply=True)

    assert report.deleted == ()
    assert [(kept.run_id, kept.reason) for kept in report.kept] == [("no-time", "no_finish_time")]
    assert young.is_dir() and no_time.is_dir()


def test_cancelled_runs_and_runs_without_a_manifest_are_not_this_passes(tmp_path: Path) -> None:
    _run(tmp_path, "cancelled", status="cancelled")
    (tmp_path / "building" / "bronze").mkdir(parents=True)

    report = _collect(tmp_path, apply=True)

    assert report.expired == ()
    assert (tmp_path / "cancelled").is_dir() and (tmp_path / "building").is_dir()


# -------------------------------------------------------------------------- apply


def test_apply_deletes_expired_runs_and_keeps_the_index_consistent(tmp_path: Path) -> None:
    _run(tmp_path, "old-ok")
    _run(tmp_path, "old-failed", status="failed")
    _run(tmp_path, "young", finished=RECENT)
    rebuild_index(tmp_path)

    report = _collect(tmp_path, apply=True)

    assert set(report.deleted) == {"old-ok", "old-failed"}
    assert report.freed_bytes >= 2000
    assert not (tmp_path / "old-ok").exists() and not (tmp_path / "old-failed").exists()
    assert not (tmp_path / RUN_GC_DIRNAME).exists()
    assert _indexed(tmp_path) == {"young"}
    # What the pass left is what a rebuild from the manifests gives.
    rebuild_index(tmp_path)
    assert _indexed(tmp_path) == {"young"}


def test_a_second_pass_changes_nothing(tmp_path: Path) -> None:
    _run(tmp_path, "old")
    _run(tmp_path, "young", finished=RECENT)
    rebuild_index(tmp_path)
    _collect(tmp_path, apply=True)

    again = _collect(tmp_path, apply=True)

    assert again.expired == () and again.deleted == () and again.interrupted == ()
    assert _indexed(tmp_path) == {"young"}


def test_an_index_row_that_would_not_go_is_rebuilt_from_the_manifests(tmp_path: Path) -> None:
    _run(tmp_path, "old")
    _run(tmp_path, "young", finished=RECENT)
    rebuild_index(tmp_path)

    class _StuckIndex(SqliteBuildIndex):
        def delete(self, run_id: str) -> None:
            # As a write failure does: swallowed (ADR 0003).
            return None

    report = _collect(tmp_path, apply=True, index_factory=_StuckIndex)

    assert report.deleted == ("old",)
    assert report.index_rebuilt
    assert _indexed(tmp_path) == {"young"}


def test_a_symlinked_run_is_neither_followed_nor_deleted(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()
    outside = _run(tmp_path, "outside")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    report = _collect(root, apply=True)

    assert report.expired == ()
    assert (root / "linked").is_symlink()
    assert (outside / "gold" / "part-0.parquet").is_file()


# ------------------------------------------------------------------ what is kept


def test_a_running_run_and_the_run_it_retries_are_kept(tmp_path: Path) -> None:
    _run(tmp_path, "still-running")
    _run(tmp_path, "retried")
    _run(tmp_path, "finished")
    store = BuildEventStore(tmp_path)
    try:
        _event(store, "still-running", "run_started")
        _event(store, "retry", "run_submitted")
        store.record_submission(
            "retry", owner_id="a", created_by="oidc:a", submitted_at=NOW, retry_of="retried"
        )
        _event(store, "finished", "run_started")
        _event(store, "finished", "run_finished")
    finally:
        store.close()

    report = _collect(tmp_path, apply=True)

    assert {(kept.run_id, kept.reason) for kept in report.kept} == {
        ("still-running", "running"),
        ("retried", "retry_source"),
    }
    assert report.deleted == ("finished",)


def test_a_published_run_is_kept(tmp_path: Path) -> None:
    _run(tmp_path, "published")
    _run(tmp_path, "unpublished")
    receipts = PublishReceiptStore(tmp_path)
    receipts.claim(owner_key="a", run_id="published", target="local", destination="a/d", options={})

    report = _collect(tmp_path, apply=True)

    assert [(kept.run_id, kept.reason) for kept in report.kept] == [("published", "published")]
    assert report.deleted == ("unpublished",)


def _commit_snapshot(catalog: TableCatalog, table_id: str, run_id: str) -> str:
    snapshot = catalog.begin_snapshot(
        table_id,
        run_id=run_id,
        schema_version=1,
        coverage_hash="cov",
        artifact_digest="sha256:deadbeef",
        row_count=1,
    )
    layout = SnapshotLayout(catalog.root, table_id)
    staging = layout.begin(snapshot.id)
    (staging / "part-0.csv").write_text("a\n1\n", encoding="utf-8")
    layout.write_manifest(
        snapshot.id,
        SnapshotManifest(
            snapshot_id=snapshot.id,
            table_id=table_id,
            logical_name="t",
            run_id=run_id,
            schema_version=1,
            coverage_hash="cov",
            artifact_digest="sha256:deadbeef",
            row_count=1,
            created_at=snapshot.created_at,
        ),
    )
    catalog.mark_validated(snapshot.id)
    layout.promote(snapshot.id)
    catalog.commit_snapshot(snapshot.id, expected_revision=catalog.get_table(table_id).revision)
    return snapshot.id


@pytest.fixture
def warehouse(tmp_path: Path) -> Iterator[TableCatalog]:
    catalog = TableCatalog(tmp_path / "warehouse")
    yield catalog
    catalog.close()


def test_runs_the_warehouse_keeps_are_kept_and_say_why(
    tmp_path: Path, warehouse: TableCatalog
) -> None:
    root = tmp_path / "out"
    root.mkdir()
    for run_id in ("held", "leased", "committed", "free"):
        _run(root, run_id)
    held_table = warehouse.create_table("ws", "held")
    held = _commit_snapshot(warehouse, held_table.id, "held")
    warehouse.place_hold(held, kind="audit", reason="evidence")
    leased_table = warehouse.create_table("ws", "leased")
    _commit_snapshot(warehouse, leased_table.id, "leased")
    lease = warehouse.resolve_current(leased_table.id)
    committed_table = warehouse.create_table("ws", "committed")
    _commit_snapshot(warehouse, committed_table.id, "committed")

    report = _collect(root, apply=True, warehouse_root=warehouse.root)

    assert {(kept.run_id, kept.reason) for kept in report.kept} == {
        ("held", "held"),
        ("leased", "leased"),
        ("committed", "snapshot"),
    }
    assert report.deleted == ("free",)
    warehouse.release(lease.lease_id)


def test_a_reference_that_arrives_during_the_pass_keeps_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "late-publish")
    receipts = PublishReceiptStore(tmp_path)
    receipts.claim(owner_key="a", run_id="other", target="local", destination="a/d", options={})
    measure = retention.tree_bytes

    def measure_then_publish(path: Path) -> int:
        # The scan has judged the run free; a publish arrives before it is deleted.
        receipts.claim(
            owner_key="a", run_id="late-publish", target="local", destination="a/e", options={}
        )
        return measure(path)

    monkeypatch.setattr(retention, "tree_bytes", measure_then_publish)

    report = _collect(tmp_path, apply=True)

    assert report.deleted == ()
    assert [(kept.run_id, kept.reason) for kept in report.kept] == [("late-publish", "published")]
    assert (tmp_path / "late-publish").is_dir()


# --------------------------------------------------------------- a pass that dies


class _Crash(Exception):
    pass


def test_a_pass_killed_while_removing_files_is_finished_by_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "old")
    _run(tmp_path, "young", finished=RECENT)
    rebuild_index(tmp_path)
    real_rmtree = shutil.rmtree

    def dies_halfway(path: str | os.PathLike[str], *args: Any, **kwargs: Any) -> None:
        (Path(path) / "manifest.json").unlink()
        raise _Crash

    monkeypatch.setattr(retention.shutil, "rmtree", dies_halfway)
    with pytest.raises(_Crash):
        _collect(tmp_path, apply=True)
    monkeypatch.setattr(retention.shutil, "rmtree", real_rmtree)

    # The run left the output directory whole, in one step: no half run is listed.
    assert not (tmp_path / "old").exists()
    assert (tmp_path / RUN_GC_DIRNAME / "old").is_dir()
    rebuild_index(tmp_path)
    assert _indexed(tmp_path) == {"young"}
    dry = _collect(tmp_path)
    assert dry.interrupted == ("old",)
    assert (tmp_path / RUN_GC_DIRNAME / "old").is_dir()

    report = _collect(tmp_path, apply=True)

    assert report.interrupted == ("old",)
    assert not (tmp_path / RUN_GC_DIRNAME).exists()
    assert _indexed(tmp_path) == {"young"}


def test_a_pass_killed_between_the_rename_and_the_index_is_finished_by_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "old")
    _run(tmp_path, "young", finished=RECENT)
    rebuild_index(tmp_path)
    real_rename = os.rename

    def rename_then_die(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        real_rename(src, dst)
        raise _Crash

    monkeypatch.setattr(retention.os, "rename", rename_then_die)
    with pytest.raises(_Crash):
        _collect(tmp_path, apply=True)
    monkeypatch.setattr(retention.os, "rename", real_rename)
    # The row of a run that is no longer there is still indexed.
    assert _indexed(tmp_path) == {"old", "young"}

    report = _collect(tmp_path, apply=True)

    assert report.interrupted == ("old",)
    assert _indexed(tmp_path) == {"young"}
    assert not (tmp_path / RUN_GC_DIRNAME).exists()
    rebuild_index(tmp_path)
    assert _indexed(tmp_path) == {"young"}


# -------------------------------------------------------------------------- CLI


def test_the_command_is_a_dry_run_unless_told_and_says_the_space(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("KPUBDATA_BUILDER_WAREHOUSE", raising=False)
    monkeypatch.setenv(RUN_RETENTION_DAYS_ENV, "1")
    _run(tmp_path, "old", finished=datetime.now(timezone.utc) - timedelta(days=5), size=2_097_152)

    assert main(["prune-runs", "--output-dir", str(tmp_path)]) == 0
    dry = capsys.readouterr().out
    assert "would delete: old" in dry
    assert "would free 2.0 MB" in dry
    assert (tmp_path / "old").is_dir()

    assert main(["prune-runs", "--output-dir", str(tmp_path), "--apply"]) == 0
    applied = capsys.readouterr().out
    assert "deleted: old" in applied and "freed 2.0 MB" in applied
    assert not (tmp_path / "old").exists()


def test_the_command_takes_the_state_lock_like_every_writer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "old")
    with state_lock([tmp_path], exclusive=True, purpose="a backup"):
        code = main(
            ["prune-runs", "--output-dir", str(tmp_path), "--retention-days", "1", "--apply"]
        )

    assert code == 1
    assert "is being backed up or restored" in capsys.readouterr().err
    assert (tmp_path / "old").is_dir()


def test_a_malformed_retention_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_RETENTION_DAYS_ENV, "a month")

    assert main(["prune-runs", "--output-dir", str(tmp_path)]) == 1
    assert RUN_RETENTION_DAYS_ENV in capsys.readouterr().err


# ------------------------------------------------------------------ admission

_SPEC = (
    "dataset_id: d\ntitle: t\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_quality\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)


class _HeldBuilds(BuilderService):
    """Every build waits for ``release``."""

    def __init__(self, tmp_path: Path, *, workers: int = 4) -> None:
        super().__init__(
            output_root=tmp_path,
            client_factory=lambda **_: None,
            async_max_workers=workers,
            async_max_queue_size=50,
        )
        self.release = threading.Event()

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        self.release.wait(timeout=10)
        return ServiceResponse(200, {"status": "ok", "run_id": run_id})


def _submit(service: BuilderService, run_id: str, owner: str) -> ServiceResponse:
    return service.submit_build(_SPEC, run_id=run_id, created_by=f"oidc:{owner}", owner_id=owner)


def _validates(response: ServiceResponse, status: int, path: str = "/builds") -> None:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    schema = response_schema(contract, path, "post", status)
    assert schema is not None
    assert validate(response.body, schema, contract) == []


@pytest.fixture
def multi_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_OWNERSHIP_ENV, "true")
    monkeypatch.setenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "0")
    for name in (RUN_STORAGE_MAX_BYTES_ENV, RUN_STORAGE_RESERVE_BYTES_ENV, BUILD_MIN_FREE_MB_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.usefixtures("multi_user")
def test_every_storage_check_is_off_until_it_is_set(tmp_path: Path) -> None:
    _run(tmp_path, "alice-big", owner="alice", size=1_000_000)
    rebuild_index(tmp_path)
    service = _HeldBuilds(tmp_path)
    try:
        assert _submit(service, "alice-0", "alice").status_code == 202
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_an_owner_over_the_limit_is_refused_and_another_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "5000")
    _run(tmp_path, "alice-big", owner="alice", size=6000)
    _run(tmp_path, "bob-small", owner="bob", size=100)
    rebuild_index(tmp_path)
    service = _HeldBuilds(tmp_path)
    try:
        refused = _submit(service, "alice-0", "alice")
        bob = _submit(service, "bob-0", "bob")

        assert refused.status_code == 409, refused.body
        body = cast(dict[str, Any], refused.body)
        assert body["code"] == "run_storage_quota_exceeded"
        assert body["limit_bytes"] == 5000 and body["used_bytes"] >= 6000
        _validates(refused, 409)
        assert bob.status_code == 202
        # Nothing was recorded for the refused run.
        assert service.build_status("alice-0").status_code == 404
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_two_submissions_at_once_cannot_both_take_the_last_room(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Room for two reservations; one is taken by a running build.
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "2000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    service = _HeldBuilds(tmp_path, workers=1)
    try:
        assert _submit(service, "a-0", "alice").status_code == 202
        barrier = threading.Barrier(8)
        codes: list[int] = []

        def submit(i: int) -> None:
            barrier.wait()
            codes.append(_submit(service, f"a-race-{i}", "alice").status_code)

        threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert sorted(codes) == [202] + [409] * 7
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_a_low_disk_refuses_a_build_without_naming_a_path_or_a_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_MIN_FREE_MB_ENV, "100")
    monkeypatch.setattr(
        run_storage,
        "measure_disk_usage",
        lambda _path: DiskUsage(free_bytes=50 * 1024 * 1024, total_bytes=10**12),
    )
    service = _HeldBuilds(tmp_path)
    try:
        refused = _submit(service, "a-0", "alice")

        assert refused.status_code == 503
        body = cast(dict[str, Any], refused.body)
        assert body["code"] == "disk_space_low"
        assert str(tmp_path) not in body["error"] and "50" not in body["error"]
        _validates(refused, 503)
        assert service.build_status("a-0").status_code == 404

        monkeypatch.setattr(
            run_storage,
            "measure_disk_usage",
            lambda _path: DiskUsage(free_bytes=200 * 1024 * 1024, total_bytes=10**12),
        )
        assert _submit(service, "a-1", "alice").status_code == 202
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_synchronous_build_is_refused_before_execution_when_disk_is_low(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(BUILD_MIN_FREE_MB_ENV, "100")
    monkeypatch.setattr(
        run_storage,
        "measure_disk_usage",
        lambda _path: DiskUsage(free_bytes=50 * 1024 * 1024, total_bytes=10**12),
    )
    service = _HeldBuilds(tmp_path)
    try:
        called = False

        def run_sync(*_args: object, **_kwargs: object) -> ServiceResponse:
            nonlocal called
            called = True
            return ServiceResponse(200, {"status": "ok"})

        monkeypatch.setattr(service._build_runs, "build", run_sync)
        response = service.build(_SPEC, run_id="sync-low", owner_id="alice")

        assert response.status_code == 503
        assert cast(dict[str, Any], response.body)["code"] == "disk_space_low"
        _validates(response, 503, "/build")
        assert not called
        assert not (tmp_path / "sync-low").exists()
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_synchronous_reservation_blocks_async_submission_and_is_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "1000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    service = _HeldBuilds(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[ServiceResponse] = []

    def run_sync(*_args: object, **_kwargs: object) -> ServiceResponse:
        entered.set()
        assert release.wait(timeout=10)
        return ServiceResponse(200, {"status": "ok"})

    monkeypatch.setattr(service._build_runs, "build", run_sync)
    thread = threading.Thread(
        target=lambda: outcomes.append(service.build(_SPEC, run_id="sync", owner_id="alice"))
    )
    thread.start()
    try:
        assert entered.wait(timeout=10)
        refused = _submit(service, "async-alice", "alice")
        assert refused.status_code == 409
        assert cast(dict[str, Any], refused.body)["code"] == "run_storage_quota_exceeded"
        _validates(refused, 409, "/build")
        assert _submit(service, "async-bob", "bob").status_code == 202
    finally:
        release.set()
        thread.join(timeout=10)
        service.release.set()
    assert not thread.is_alive()
    assert outcomes[0].status_code == 200
    assert _submit(service, "async-alice-later", "alice").status_code == 202


@pytest.mark.usefixtures("multi_user")
def test_sync_storage_reservation_does_not_consume_async_owner_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "2000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    monkeypatch.setenv(MAX_ACTIVE_BUILDS_PER_OWNER_ENV, "1")
    service = _HeldBuilds(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def run_sync(*_args: object, **_kwargs: object) -> ServiceResponse:
        entered.set()
        assert release.wait(timeout=10)
        return ServiceResponse(200, {"status": "ok"})

    monkeypatch.setattr(service._build_runs, "build", run_sync)
    thread = threading.Thread(target=service.build, args=(_SPEC,), kwargs={"owner_id": "alice"})
    thread.start()
    try:
        assert entered.wait(timeout=10)
        assert _submit(service, "async-alice", "alice").status_code == 202
        refused = _submit(service, "async-alice-2", "alice")
        assert refused.status_code == 429
        assert cast(dict[str, Any], refused.body)["code"] == "build_owner_limit"
    finally:
        release.set()
        thread.join(timeout=10)
        service.release.set()
    assert not thread.is_alive()


@pytest.mark.usefixtures("multi_user")
def test_async_reservation_blocks_synchronous_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "1000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    service = _HeldBuilds(tmp_path)
    try:
        assert _submit(service, "async-alice", "alice").status_code == 202
        response = service.build(_SPEC, run_id="sync-alice", owner_id="alice")
        assert response.status_code == 409
        assert cast(dict[str, Any], response.body)["code"] == "run_storage_quota_exceeded"
        assert not (tmp_path / "sync-alice").exists()
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_direct_build_with_cancellation_still_checks_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "1000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    service = _HeldBuilds(tmp_path)
    try:
        assert _submit(service, "async-alice", "alice").status_code == 202
        response = service.build(
            _SPEC,
            run_id="direct-alice",
            owner_id="alice",
            cancellation=RunCancellation(),
        )
        assert response.status_code == 409
        assert cast(dict[str, Any], response.body)["code"] == "run_storage_quota_exceeded"
    finally:
        service.release.set()


@pytest.mark.usefixtures("multi_user")
def test_two_synchronous_builds_cannot_take_the_same_last_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "1000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    service = _HeldBuilds(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[ServiceResponse] = []

    def run_sync(*_args: object, **_kwargs: object) -> ServiceResponse:
        entered.set()
        assert release.wait(timeout=10)
        return ServiceResponse(200, {"status": "ok"})

    monkeypatch.setattr(service._build_runs, "build", run_sync)
    thread = threading.Thread(
        target=lambda: outcomes.append(service.build(_SPEC, run_id="sync-1", owner_id="alice"))
    )
    thread.start()
    try:
        assert entered.wait(timeout=10)
        refused = service.build(_SPEC, run_id="sync-2", owner_id="alice")
        assert refused.status_code == 409
        assert cast(dict[str, Any], refused.body)["code"] == "run_storage_quota_exceeded"
    finally:
        release.set()
        thread.join(timeout=10)
        service.release.set()
    assert not thread.is_alive()
    assert outcomes[0].status_code == 200


@pytest.mark.usefixtures("multi_user")
def test_synchronous_reservation_is_released_after_execution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "1000")
    monkeypatch.setenv(RUN_STORAGE_RESERVE_BYTES_ENV, "1000")
    service = _HeldBuilds(tmp_path)
    calls = 0

    def run_sync(*_args: object, **_kwargs: object) -> ServiceResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("execution failed")
        return ServiceResponse(200, {"status": "ok"})

    monkeypatch.setattr(service._build_runs, "build", run_sync)
    try:
        with pytest.raises(RuntimeError, match="execution failed"):
            service.build(_SPEC, run_id="sync-first", owner_id="alice")
        response = service.build(_SPEC, run_id="sync-second", owner_id="alice")
        assert response.status_code == 200
        assert calls == 2
    finally:
        service.release.set()


def test_a_single_user_deployment_has_no_storage_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_OWNERSHIP_ENV, raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.setenv(RUN_STORAGE_MAX_BYTES_ENV, "10")
    _run(tmp_path, "mine", owner="alice", size=1000)
    rebuild_index(tmp_path)
    service = _HeldBuilds(tmp_path)
    try:
        assert _submit(service, "a-0", "alice").status_code == 202
    finally:
        service.release.set()


def test_only_the_owners_indexed_runs_are_counted(tmp_path: Path) -> None:
    _run(tmp_path, "alice-1", owner="alice", size=300)
    _run(tmp_path, "bob-1", owner="bob", size=5000)
    rebuild_index(tmp_path)
    (tmp_path / ".service").mkdir()
    (tmp_path / ".service" / "uploads.sqlite3").write_bytes(b"u" * 9000)
    index = SqliteBuildIndex(tmp_path)
    try:
        index.insert_or_replace(
            "../escape", "ok", None, None, owner_id="alice", created_by="oidc:alice"
        )
        used = run_storage.owner_run_bytes(tmp_path, index, "alice")
    finally:
        index.close()

    assert 300 <= used < 1000

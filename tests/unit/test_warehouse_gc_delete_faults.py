"""A failed delete keeps the snapshot's catalog row until its files are gone (#1334).

``delete_snapshot`` removed the files with ``ignore_errors=True`` and then forgot the
catalog row unconditionally. A permission error or an interrupted delete therefore
left bytes on disk that no catalog row referred to: an orphan under ``snapshots/``
that no later pass would ever reclaim, because collection only reaches committed
snapshots through the catalog.

Each test injects an I/O error at the file-delete boundary and checks the catalog and
the filesystem together: the row is kept as ``retiring`` (so nothing can read or hold
it), the error is reported rather than swallowed, protected snapshots are untouched,
and a later pass with the cause gone finishes the delete.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder.cli import main as cli_main
from kpubdata_builder.warehouse import (
    MaterializeResult,
    SnapshotLayout,
    SnapshotStateError,
    TableCatalog,
    materialize,
)
from kpubdata_builder.warehouse import gc as warehouse_gc
from kpubdata_builder.warehouse.errors import SnapshotDeleteFailed

WORKSPACE = "ws_personal"
TABLE = "apt_trade"


def _gold(root: Path, name: str, payload: bytes) -> Path:
    directory = root / name / "gold" / "datago.apt_trade"
    directory.mkdir(parents=True)
    (directory / "table.parquet").write_bytes(payload)
    splits = directory / "splits"
    splits.mkdir()
    (splits / "train.parquet").write_bytes(payload + b"-train")
    return directory


def _build(catalog: TableCatalog, tmp_path: Path, run_id: str) -> MaterializeResult:
    return materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name=TABLE,
        source_dir=_gold(tmp_path, run_id, f"PAR1{run_id}".encode()),
        run_id=run_id,
    )


@pytest.fixture
def catalog(tmp_path: Path) -> TableCatalog:
    """A catalog rooted in a temporary warehouse."""
    return TableCatalog(tmp_path / "warehouse")


@pytest.fixture
def builds(catalog: TableCatalog, tmp_path: Path) -> list[MaterializeResult]:
    """Three committed builds of one table; the last is current."""
    return [_build(catalog, tmp_path, f"run-{n}") for n in (1, 2, 3)]


def _fail_unlink_of(name: str) -> Callable[[pytest.MonkeyPatch], None]:
    """Refuse to unlink one file name, letting every other entry go: a partial delete."""

    def install(monkeypatch: pytest.MonkeyPatch) -> None:
        original = os.unlink

        def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
            if os.fsdecode(path).endswith(name):
                raise PermissionError(errno.EACCES, "Permission denied", os.fsdecode(path))
            original(path, *args, **kwargs)

        monkeypatch.setattr(os, "unlink", unlink)

    return install


def _fail_rmdir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every file goes but the snapshot directory itself stays: an empty root left over."""
    original = os.rmdir

    def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(os.fsdecode(path)).name.startswith("snap_"):
            raise OSError(errno.EBUSY, "Device or resource busy", os.fsdecode(path))
        original(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", rmdir)


FAULTS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "data-file": _fail_unlink_of("table.parquet"),
    "nested-file": _fail_unlink_of("train.parquet"),
    "snapshot-root": _fail_rmdir,
}


def _snapshot_files(layout: SnapshotLayout, snapshot_id: str) -> list[Path]:
    root = layout.snapshot_dir(snapshot_id)
    return sorted(root.rglob("*")) if root.exists() else []


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_failed_delete_keeps_the_catalog_row(
    fault: str,
    catalog: TableCatalog,
    builds: list[MaterializeResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row survives as ``retiring`` while any of its bytes remain on disk."""
    oldest = builds[0].snapshot.id
    layout = SnapshotLayout(catalog.root, builds[0].table.id)

    with monkeypatch.context() as patch:
        FAULTS[fault](patch)
        with pytest.raises(SnapshotDeleteFailed) as raised:
            warehouse_gc.delete_snapshot(catalog, oldest)
    assert raised.value.snapshot_id == oldest
    assert isinstance(raised.value.__cause__, OSError)

    # What is left on disk is still referenced by the catalog.
    assert layout.snapshot_dir(oldest).exists()
    assert oldest in catalog.known_snapshot_ids(builds[0].table.id)
    assert catalog.get_snapshot(oldest).state == "retiring"
    # And nobody can start reading or holding a half-deleted snapshot.
    with pytest.raises(SnapshotStateError):
        catalog.pin(oldest)
    with pytest.raises(SnapshotStateError):
        catalog.place_hold(oldest, kind="audit", reason="too late")


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_gc_reports_the_failure_and_a_later_pass_finishes_it(
    fault: str,
    catalog: TableCatalog,
    builds: list[MaterializeResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed pass reports, keeps the row; a pass after the cause is gone reclaims it."""
    table_id = builds[0].table.id
    oldest, middle, current = (b.snapshot.id for b in builds)
    layout = SnapshotLayout(catalog.root, table_id)
    current_files = {p: p.read_bytes() for p in _snapshot_files(layout, current) if p.is_file()}

    with monkeypatch.context() as patch:
        FAULTS[fault](patch)
        failed = warehouse_gc.collect(catalog, table_id, keep=1)
    assert sorted(failed.delete_failed) == sorted([oldest, middle])
    assert failed.snapshots_removed == []
    for snapshot_id in (oldest, middle):
        assert catalog.get_snapshot(snapshot_id).state == "retiring"
        assert layout.snapshot_dir(snapshot_id).exists()

    # The cause is resolved; the retiring rows are retried without any new snapshot.
    retried = warehouse_gc.collect(catalog, table_id, keep=1)
    assert sorted(retried.snapshots_removed) == sorted([oldest, middle])
    assert retried.delete_failed == []
    assert [s.id for s in catalog.list_snapshots(table_id)] == [current]
    assert sorted(p.name for p in layout.snapshots_root.iterdir()) == [current]
    assert not layout.staging_root.exists() or not any(layout.staging_root.iterdir())
    assert catalog.get_table(table_id).current_snapshot_id == current
    assert {p: p.read_bytes() for p in current_files} == current_files

    # A third pass has nothing to do.
    assert warehouse_gc.collect(catalog, table_id, keep=1).removed_count == 0


def test_a_failed_profile_delete_keeps_the_catalog_row(
    catalog: TableCatalog, builds: list[MaterializeResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cached profile is the snapshot's too; failing to drop it keeps the row."""
    oldest = builds[0].snapshot.id
    layout = SnapshotLayout(catalog.root, builds[0].table.id)
    layout.profiles_root.mkdir(parents=True, exist_ok=True)
    layout.profile_path(oldest).write_text("{}", encoding="utf-8")

    original = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        # Path.unlink does not go through os.unlink on Python 3.10, so patch it directly.
        if self.name == f"{oldest}.json":
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        original(self, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", unlink)
        with pytest.raises(SnapshotDeleteFailed):
            warehouse_gc.delete_snapshot(catalog, oldest)
    assert catalog.get_snapshot(oldest).state == "retiring"
    assert layout.profile_path(oldest).exists()

    warehouse_gc.delete_snapshot(catalog, oldest)
    assert oldest not in catalog.known_snapshot_ids(builds[0].table.id)
    assert not layout.profile_path(oldest).exists()
    assert not layout.snapshot_dir(oldest).exists()


def test_a_failed_staging_delete_keeps_the_catalog_row(
    catalog: TableCatalog, builds: list[MaterializeResult], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An abandoned snapshot's staged bytes are its only bytes; they keep the row too."""
    table_id = builds[0].table.id
    layout = SnapshotLayout(catalog.root, table_id)
    row = catalog.begin_snapshot(
        table_id, run_id="crashed", schema_version=1, coverage_hash="c", artifact_digest="d"
    )
    staged = layout.begin(row.id)
    (staged / "stuck.parquet").write_bytes(b"PAR1")
    catalog.abandon(row.id)

    with monkeypatch.context() as patch:
        _fail_unlink_of("stuck.parquet")(patch)
        report = warehouse_gc.collect(catalog, table_id, keep=3)
    assert report.delete_failed == [row.id]
    assert catalog.get_snapshot(row.id).state == "retiring"
    assert (staged / "stuck.parquet").exists()
    # The staging directory is still known, so it is not treated as an orphan either.
    assert row.id not in warehouse_gc.collect_orphan_staging(catalog, table_id)

    assert warehouse_gc.collect(catalog, table_id, keep=3).snapshots_removed == [row.id]
    assert not staged.exists()
    assert row.id not in catalog.known_snapshot_ids(table_id)


def test_protected_snapshots_are_never_touched_while_deletes_fail(
    catalog: TableCatalog,
    tmp_path: Path,
    builds: list[MaterializeResult],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current, leased and held snapshots keep their rows and bytes through both passes."""
    fourth = _build(catalog, tmp_path, "run-4")
    table_id = fourth.table.id
    held, leased, doomed = builds[0].snapshot.id, builds[1].snapshot.id, builds[2].snapshot.id
    catalog.place_hold(held, kind="audit", reason="investigation")
    catalog.pin(leased)
    layout = SnapshotLayout(catalog.root, table_id)
    protected = (held, leased, fourth.snapshot.id)
    before = {s: _snapshot_files(layout, s) for s in protected}

    for inject in (True, False):
        with monkeypatch.context() as patch:
            if inject:
                _fail_unlink_of("table.parquet")(patch)
            report = warehouse_gc.collect(catalog, table_id, keep=0)
        assert held in report.kept_held
        assert leased in report.kept_leased
        assert fourth.snapshot.id in report.kept_current
        for snapshot_id in protected:
            assert catalog.get_snapshot(snapshot_id).state == "committed"
            assert _snapshot_files(layout, snapshot_id) == before[snapshot_id]
        if inject:
            assert report.delete_failed == [doomed]
        else:
            assert report.snapshots_removed == [doomed]
    assert catalog.get_table(table_id).current_snapshot_id == fourth.snapshot.id


def test_warehouse_gc_command_fails_when_a_delete_fails(
    catalog: TableCatalog,
    builds: list[MaterializeResult],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operator sees the failure and a non-zero exit, then a clean retry."""
    argv = ["warehouse-gc", str(catalog.root), "--keep", "1"]
    with monkeypatch.context() as patch:
        _fail_unlink_of("table.parquet")(patch)
        assert cli_main(argv) == 1
    captured = capsys.readouterr()
    assert "failed 2" in captured.out
    assert "could not delete" in captured.err

    assert cli_main(argv) == 0
    assert "reclaimed 2 directories" in capsys.readouterr().out

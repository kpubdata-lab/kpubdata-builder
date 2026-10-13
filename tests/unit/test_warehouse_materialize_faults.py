"""A failed file operation before the commit abandons the staging snapshot (#1333).

``materialize`` registers a snapshot, creates a staging directory and copies the Gold
files before the commit. Only the commit was protected, so an ``OSError`` in any step
before it (disk full, permission loss, an interrupted copy) left a ``staging`` row and
partial files that garbage collection would never look at: the catalog knew the id, so
the directory was not an orphan, and the row was not ``abandoned``, so it was not
reclaimed either.

Each test injects one I/O error at the file-operation boundary and checks the catalog
and the filesystem together: the snapshot is ``abandoned``, its staged bytes are gone,
the error that caused it is the one the caller sees, and the committed snapshot that was
current beforehand is untouched.
"""

from __future__ import annotations

import errno
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder.warehouse import (
    MaterializeResult,
    SnapshotLayout,
    TableCatalog,
    materialize,
)
from kpubdata_builder.warehouse import gc as warehouse_gc
from kpubdata_builder.warehouse.layout import MANIFEST_FILENAME, content_digest

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


def _build(catalog: TableCatalog, source: Path, run_id: str) -> MaterializeResult:
    return materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name=TABLE,
        source_dir=source,
        run_id=run_id,
    )


def _disk_full(*_args: object, **_kwargs: object) -> None:
    raise OSError(errno.ENOSPC, "No space left on device")


def _in_staging(path: object) -> bool:
    return "_staging" in Path(str(path)).parts


@pytest.fixture
def catalog(tmp_path: Path) -> TableCatalog:
    """A catalog rooted in a temporary warehouse."""
    return TableCatalog(tmp_path / "warehouse")


@pytest.fixture
def first(catalog: TableCatalog, tmp_path: Path) -> MaterializeResult:
    """A committed snapshot that every failure below must leave current and intact."""
    return _build(catalog, _gold(tmp_path, "run-1", b"PAR1first"), "run-1")


@pytest.fixture
def second_gold(tmp_path: Path) -> Path:
    """The Gold directory of the refresh that fails."""
    return _gold(tmp_path, "run-2", b"PAR1second")


def _fail_staging_create(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.mkdir

    def mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if _in_staging(self):
            _disk_full()
        original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)


def _fail_file_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    def copy2(src: Any, dst: Any, **_kwargs: Any) -> None:
        # A copy interrupted half way leaves a truncated file behind.
        Path(dst).write_bytes(Path(src).read_bytes()[:2])
        _disk_full()

    monkeypatch.setattr(shutil, "copy2", copy2)


def _fail_tree_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    def copytree(src: Any, dst: Any, **_kwargs: Any) -> None:
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "partial.parquet").write_bytes(b"PAR")
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(shutil, "copytree", copytree)


def _fail_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.open

    def open_(self: Path, *args: Any, **kwargs: Any) -> Any:
        if _in_staging(self):
            raise OSError(errno.EIO, "Input/output error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_)


def _fail_manifest_write(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.write_text

    def write_text(self: Path, *args: Any, **kwargs: Any) -> int:
        if self.name == MANIFEST_FILENAME:
            # Truncated on a full disk, then refused.
            original(self, "{", encoding="utf-8")
            _disk_full()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)


def _fail_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.rename

    def rename(self: Path, target: Any) -> Path:
        if _in_staging(self):
            raise OSError(errno.EACCES, "Permission denied")
        return original(self, target)

    monkeypatch.setattr(Path, "rename", rename)


FAULTS: dict[str, Callable[[pytest.MonkeyPatch], None]] = {
    "staging-create": _fail_staging_create,
    "file-copy": _fail_file_copy,
    "tree-copy": _fail_tree_copy,
    "digest": _fail_digest,
    "manifest": _fail_manifest_write,
    "promote": _fail_promote,
}


def _only_snapshot_besides(catalog: TableCatalog, table_id: str, committed: str) -> str:
    others = [s.id for s in catalog.list_snapshots(table_id) if s.id != committed]
    assert len(others) == 1
    return others[0]


def _assert_first_untouched(catalog: TableCatalog, first: MaterializeResult) -> None:
    table = catalog.get_table(first.table.id)
    assert table.current_snapshot_id == first.snapshot.id
    assert table.revision == first.table.revision
    assert catalog.get_snapshot(first.snapshot.id).state == "committed"
    assert (first.snapshot_dir / "table.parquet").read_bytes() == b"PAR1first"
    assert content_digest(first.snapshot_dir) == first.snapshot.artifact_digest


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_a_failed_file_operation_abandons_the_staging_snapshot(
    fault: str,
    catalog: TableCatalog,
    first: MaterializeResult,
    second_gold: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row ``abandoned``, staged bytes gone, the original error raised, current intact."""
    with monkeypatch.context() as patch:
        FAULTS[fault](patch)
        with pytest.raises(OSError) as raised:
            _build(catalog, second_gold, "run-2")
    assert raised.value.errno in (errno.ENOSPC, errno.EIO, errno.EACCES)

    failed = _only_snapshot_besides(catalog, first.table.id, first.snapshot.id)
    assert catalog.get_snapshot(failed).state == "abandoned"
    layout = SnapshotLayout(catalog.root, first.table.id)
    assert not layout.staging_dir(failed).exists()
    assert not layout.snapshot_dir(failed).exists()
    _assert_first_untouched(catalog, first)

    # GC reclaims the row, keeps the current snapshot, and a second pass has nothing left.
    report = warehouse_gc.collect(catalog, first.table.id, keep=0)
    assert report.snapshots_removed == [failed]
    assert first.snapshot.id in report.kept_current
    assert warehouse_gc.collect(catalog, first.table.id, keep=0).removed_count == 0
    assert [s.id for s in catalog.list_snapshots(first.table.id)] == [first.snapshot.id]
    _assert_first_untouched(catalog, first)

    # Nothing left behind blocks the next refresh.
    retried = _build(catalog, second_gold, "run-3")
    assert catalog.get_table(first.table.id).current_snapshot_id == retried.snapshot.id


def test_a_failure_on_the_first_build_leaves_no_partial_table_data(
    catalog: TableCatalog, second_gold: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no previous snapshot the table stays empty rather than half-written."""
    with monkeypatch.context() as patch:
        _fail_file_copy(patch)
        with pytest.raises(OSError):
            _build(catalog, second_gold, "run-1")

    table = catalog.list_tables(WORKSPACE)[0]
    assert table.current_snapshot_id is None
    snapshots = catalog.list_snapshots(table.id)
    assert [s.state for s in snapshots] == ["abandoned"]
    layout = SnapshotLayout(catalog.root, table.id)
    assert not layout.staging_dir(snapshots[0].id).exists()


def test_a_commit_that_fails_after_promote_is_abandoned(
    catalog: TableCatalog,
    first: MaterializeResult,
    second_gold: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An I/O error while verifying the promoted files is not a compare-and-swap loss,
    but it leaves the same reclaimable state, and GC removes the promoted bytes."""

    def commit_snapshot(self: TableCatalog, snapshot_id: str, **kwargs: Any) -> Any:
        raise OSError(errno.EIO, "Input/output error")

    with monkeypatch.context() as patch:
        patch.setattr(TableCatalog, "commit_snapshot", commit_snapshot)
        with pytest.raises(OSError, match="Input/output error"):
            _build(catalog, second_gold, "run-2")

    failed = _only_snapshot_besides(catalog, first.table.id, first.snapshot.id)
    assert catalog.get_snapshot(failed).state == "abandoned"
    _assert_first_untouched(catalog, first)

    layout = SnapshotLayout(catalog.root, first.table.id)
    assert layout.snapshot_dir(failed).exists()
    assert warehouse_gc.collect(catalog, first.table.id, keep=0).snapshots_removed == [failed]
    assert not layout.snapshot_dir(failed).exists()
    _assert_first_untouched(catalog, first)


def test_a_failed_cleanup_does_not_replace_the_original_error(
    tmp_path: Path,
    catalog: TableCatalog,
    first: MaterializeResult,
    second_gold: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the catalog cannot be written either, the copy error still surfaces, and
    after a restart ``abandon_stale`` plus a collection pass reclaim what was left."""

    def locked(self: TableCatalog, snapshot_id: str) -> None:
        raise sqlite3.OperationalError("database is locked")

    with monkeypatch.context() as patch:
        _fail_file_copy(patch)
        patch.setattr(TableCatalog, "abandon", locked)
        with pytest.raises(OSError) as raised:
            _build(catalog, second_gold, "run-2")
    assert raised.value.errno == errno.ENOSPC

    failed = _only_snapshot_besides(catalog, first.table.id, first.snapshot.id)
    assert catalog.get_snapshot(failed).state == "staging"
    layout = SnapshotLayout(catalog.root, first.table.id)
    # The row could not be marked, so its files are left for the row's reclamation:
    # deleting them first would leave a row pointing at nothing.
    assert layout.staging_dir(failed).exists()
    _assert_first_untouched(catalog, first)

    # A restart: a new catalog over the same root.
    catalog.close()
    restarted = TableCatalog(tmp_path / "warehouse")
    assert warehouse_gc.abandon_stale(
        restarted, first.table.id, before="9999-12-31T00:00:00+00:00"
    ) == [failed]
    report = warehouse_gc.collect(restarted, first.table.id, keep=0)
    assert report.snapshots_removed == [failed]
    assert not layout.staging_dir(failed).exists()
    _assert_first_untouched(restarted, first)
    restarted.close()


def test_a_concurrent_successful_commit_survives_the_failure_and_gc(
    catalog: TableCatalog,
    first: MaterializeResult,
    second_gold: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another build commits while this one is copying; this one then fails. The
    cleanup reaches only this build's snapshot, and GC keeps the winner current."""
    third_gold = _gold(tmp_path, "run-3", b"PAR1third")
    original = shutil.copy2
    winner: list[MaterializeResult] = []

    fired: list[bool] = []

    def copy2(src: Any, dst: Any, **kwargs: Any) -> Any:
        if not fired:
            fired.append(True)
            # The other build copies through the real function and commits first.
            winner.append(_build(catalog, third_gold, "run-3"))
            _disk_full()
        return original(src, dst, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", copy2)
        with pytest.raises(OSError):
            _build(catalog, second_gold, "run-2")

    won = winner[0]
    table = catalog.get_table(first.table.id)
    assert table.current_snapshot_id == won.snapshot.id
    states = {s.run_id: s.state for s in catalog.list_snapshots(first.table.id)}
    assert states == {"run-1": "committed", "run-2": "abandoned", "run-3": "committed"}

    warehouse_gc.collect(catalog, first.table.id, keep=0)
    assert catalog.get_table(first.table.id).current_snapshot_id == won.snapshot.id
    assert (won.snapshot_dir / "table.parquet").read_bytes() == b"PAR1third"
    assert content_digest(won.snapshot_dir) == won.snapshot.artifact_digest
    assert {s.run_id for s in catalog.list_snapshots(first.table.id)} == {"run-3"}


def test_an_interrupted_build_is_abandoned_too(
    catalog: TableCatalog,
    first: MaterializeResult,
    second_gold: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``KeyboardInterrupt`` mid-copy is not an ``Exception``, and it still leaves a
    reclaimable state rather than a ``staging`` row nothing will look at."""

    def interrupted(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(shutil, "copy2", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _build(catalog, second_gold, "run-2")

    failed = _only_snapshot_besides(catalog, first.table.id, first.snapshot.id)
    assert catalog.get_snapshot(failed).state == "abandoned"
    _assert_first_untouched(catalog, first)

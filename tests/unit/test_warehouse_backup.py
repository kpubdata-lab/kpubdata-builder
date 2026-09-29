"""Snapshot holds, backup and restore (#705).

Garbage collection must keep what something still needs — the current snapshot, a
snapshot a query is reading, and one a saved analysis, retention period or audit
holds. A backup must carry the catalog and the files together, and a restore must
refuse, loudly and completely, anything that would leave a pointer naming nothing.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.warehouse import (
    BackupInvalid,
    SnapshotHeld,
    SnapshotLayout,
    SnapshotStateError,
    TableCatalog,
    materialize,
)
from kpubdata_builder.warehouse import gc as warehouse_gc
from kpubdata_builder.warehouse.backup import BACKUP_MANIFEST, backup, restore, verify_backup
from kpubdata_builder.warehouse.catalog import CATALOG_FILENAME, SCHEMA_VERSION
from kpubdata_builder.warehouse.layout import thaw

WORKSPACE = "ws_personal"


@pytest.fixture
def catalog(tmp_path: Path) -> TableCatalog:
    return TableCatalog(tmp_path / "warehouse")


def _commit(catalog: TableCatalog, tmp_path: Path, run_id: str, values: list[int]) -> str:
    """Materialise a Gold directory holding a real parquet table; return the snapshot id."""
    gold = tmp_path / run_id / "gold"
    gold.mkdir(parents=True)
    pl.DataFrame({"id": list(range(len(values))), "value": values}).write_parquet(
        gold / "table.parquet"
    )
    result = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="trade",
        source_dir=gold,
        run_id=run_id,
        row_count=len(values),
    )
    return result.snapshot.id


def _read_current(catalog: TableCatalog) -> pl.DataFrame:
    """Query the table the way a reader does: resolve current under a lease, then read."""
    (table,) = catalog.list_tables(WORKSPACE)
    with catalog.pinned(table.id) as pin:
        directory = SnapshotLayout(catalog.root, table.id).snapshot_dir(pin.snapshot_id)
        return pl.read_parquet(directory / "table.parquet")


def _three_snapshots(catalog: TableCatalog, tmp_path: Path) -> tuple[str, str, str]:
    first = _commit(catalog, tmp_path, "run-1", [1])
    second = _commit(catalog, tmp_path, "run-2", [2])
    third = _commit(catalog, tmp_path, "run-3", [3])
    return first, second, third


# --------------------------------------------------------------------------- holds


class TestAHoldOutlivesCollection:
    @pytest.mark.parametrize("kind", ["saved_analysis", "retention", "audit"])
    def test_collection_keeps_a_held_snapshot(
        self, catalog: TableCatalog, tmp_path: Path, kind: str
    ) -> None:
        """Negative: keep=0 would take the old snapshot, and the hold stops it."""
        first, second, third = _three_snapshots(catalog, tmp_path)
        catalog.place_hold(first, kind=kind, reason="quarterly report Q3")  # type: ignore[arg-type]
        table_id = catalog.get_snapshot(first).table_id

        report = warehouse_gc.collect(catalog, table_id, keep=0)

        assert report.kept_held == [first]
        assert second in report.snapshots_removed
        assert catalog.get_snapshot(first).state == "committed"
        assert SnapshotLayout(catalog.root, table_id).snapshot_dir(first).is_dir()
        assert catalog.get_table(table_id).current_snapshot_id == third

    def test_the_hold_check_is_inside_the_retiring_transaction(
        self, catalog: TableCatalog, tmp_path: Path
    ) -> None:
        first, _, _ = _three_snapshots(catalog, tmp_path)
        catalog.place_hold(first, kind="audit", reason="evidence for case 12")

        with pytest.raises(SnapshotHeld, match="audit"):
            catalog.begin_retiring(first)
        assert catalog.get_snapshot(first).state == "committed"

    def test_a_released_hold_no_longer_protects(
        self, catalog: TableCatalog, tmp_path: Path
    ) -> None:
        first, _, _ = _three_snapshots(catalog, tmp_path)
        hold = catalog.place_hold(first, kind="saved_analysis", reason="dashboard")
        catalog.release_hold(hold.hold_id)

        report = warehouse_gc.collect(catalog, catalog.get_snapshot(first).table_id, keep=0)

        assert first in report.snapshots_removed

    def test_an_expired_hold_no_longer_protects(
        self, catalog: TableCatalog, tmp_path: Path
    ) -> None:
        first, _, _ = _three_snapshots(catalog, tmp_path)
        catalog.place_hold(
            first, kind="retention", reason="30 days", expires_at="2000-01-01T00:00:00+00:00"
        )

        assert catalog.live_holds(first) == []
        report = warehouse_gc.collect(catalog, catalog.get_snapshot(first).table_id, keep=0)
        assert first in report.snapshots_removed

    def test_a_hold_without_expiry_lasts_until_released(
        self, catalog: TableCatalog, tmp_path: Path
    ) -> None:
        first, _, _ = _three_snapshots(catalog, tmp_path)
        catalog.place_hold(first, kind="audit", reason="no end date")

        assert len(catalog.live_holds(first, now="2999-01-01T00:00:00+00:00")) == 1

    def test_only_a_readable_snapshot_can_be_held(self, catalog: TableCatalog) -> None:
        table = catalog.create_table(WORKSPACE, "trade")
        staging = catalog.begin_snapshot(
            table.id, run_id="r", schema_version=1, coverage_hash="", artifact_digest=""
        )

        with pytest.raises(SnapshotStateError, match="only a snapshot that was committed"):
            catalog.place_hold(staging.id, kind="audit", reason="too early")

    def test_a_hold_needs_a_reason(self, catalog: TableCatalog, tmp_path: Path) -> None:
        first = _commit(catalog, tmp_path, "run-1", [1])

        with pytest.raises(SnapshotStateError, match="needs a reason"):
            catalog.place_hold(first, kind="audit", reason="  ")

    def test_a_version_3_catalog_gains_holds(self, tmp_path: Path) -> None:
        """The catalog is canonical, so v3 → v4 is a migration, not a rebuild."""
        root = tmp_path / "old"
        old = TableCatalog(root)
        first = _commit(old, tmp_path, "run-1", [1])
        old.close()
        with closing(sqlite3.connect(root / CATALOG_FILENAME)) as conn:
            conn.execute("DROP TABLE snapshot_holds")
            conn.execute("UPDATE schema_version SET version = 3")
            conn.commit()

        reopened = TableCatalog(root)

        assert reopened.get_snapshot(first).state == "committed"
        reopened.place_hold(first, kind="retention", reason="migrated")
        with closing(sqlite3.connect(root / CATALOG_FILENAME)) as conn:
            assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 4
        assert SCHEMA_VERSION == 4


# ------------------------------------------------------------------ backup/restore


def test_backup_restore_query_returns_the_same_rows(catalog: TableCatalog, tmp_path: Path) -> None:
    """Acceptance: backup → restore → query gives back exactly what was committed."""
    _commit(catalog, tmp_path, "run-1", [10, 20])
    _commit(catalog, tmp_path, "run-2", [30, 40, 50])
    before = _read_current(catalog)

    report = backup(catalog, tmp_path / "backup")
    restored = restore(tmp_path / "backup", tmp_path / "restored")

    assert len(report.snapshots) == 2
    assert _read_current(restored).equals(before)
    assert not (tmp_path / "restored" / BACKUP_MANIFEST).exists()


def test_a_restore_missing_snapshot_files_fails_loudly(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    """Acceptance (negative): no empty table — a refusal naming what is missing."""
    snapshot_id = _commit(catalog, tmp_path, "run-1", [1, 2])
    backup(catalog, tmp_path / "backup")
    table_id = catalog.get_snapshot(snapshot_id).table_id
    missing = SnapshotLayout(tmp_path / "backup", table_id).snapshot_dir(snapshot_id)
    thaw(missing)
    for path in sorted(missing.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    missing.rmdir()

    with pytest.raises(BackupInvalid) as refused:
        restore(tmp_path / "backup", tmp_path / "restored")

    assert any("missing its files" in problem for problem in refused.value.problems)
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob(".restored.*"))


def test_a_backup_with_changed_bytes_is_not_restored(catalog: TableCatalog, tmp_path: Path) -> None:
    """Negative: a damaged backup made current is worse than no backup."""
    snapshot_id = _commit(catalog, tmp_path, "run-1", [1, 2])
    backup(catalog, tmp_path / "backup")
    table_id = catalog.get_snapshot(snapshot_id).table_id
    parquet = (
        SnapshotLayout(tmp_path / "backup", table_id).snapshot_dir(snapshot_id) / "table.parquet"
    )
    thaw(parquet.parent)
    parquet.write_bytes(parquet.read_bytes() + b"x")

    with pytest.raises(BackupInvalid, match="does not match its recorded digest"):
        restore(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_an_empty_snapshot_in_a_backup_is_not_restored(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    snapshot_id = _commit(catalog, tmp_path, "run-1", [1])
    backup(catalog, tmp_path / "backup")
    table_id = catalog.get_snapshot(snapshot_id).table_id
    directory = SnapshotLayout(tmp_path / "backup", table_id).snapshot_dir(snapshot_id)
    thaw(directory)
    (directory / "table.parquet").unlink()

    problems = verify_backup(tmp_path / "backup")

    assert any("is empty" in problem for problem in problems)


def test_a_pointer_to_a_snapshot_the_backup_lacks_is_refused(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    _commit(catalog, tmp_path, "run-1", [1])
    backup(catalog, tmp_path / "backup")
    with closing(sqlite3.connect(tmp_path / "backup" / CATALOG_FILENAME)) as conn:
        conn.execute("UPDATE tables SET current_snapshot_id = 'snap_nowhere'")
        conn.commit()

    problems = verify_backup(tmp_path / "backup")

    assert any("snap_nowhere" in problem for problem in problems)


def test_every_problem_is_reported_not_just_the_first(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    first = _commit(catalog, tmp_path, "run-1", [1])
    second = _commit(catalog, tmp_path, "run-2", [2])
    backup(catalog, tmp_path / "backup")
    table_id = catalog.get_snapshot(first).table_id
    for snapshot_id in (first, second):
        directory = SnapshotLayout(tmp_path / "backup", table_id).snapshot_dir(snapshot_id)
        thaw(directory)
        (directory / "table.parquet").write_bytes(b"not the committed bytes")

    problems = verify_backup(tmp_path / "backup")

    assert sum("does not match" in problem for problem in problems) == 2


def test_a_backup_leaves_out_what_nobody_can_read_and_keeps_holds(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    first, second, _ = _three_snapshots(catalog, tmp_path)
    table_id = catalog.get_snapshot(first).table_id
    catalog.place_hold(first, kind="audit", reason="keep through restore")
    catalog.begin_retiring(second)
    staging = catalog.begin_snapshot(
        table_id, run_id="crashed", schema_version=1, coverage_hash="", artifact_digest=""
    )
    catalog.pin(first)  # a lease: runtime state, not warehouse state

    report = backup(catalog, tmp_path / "backup")
    restored = restore(tmp_path / "backup", tmp_path / "restored")

    assert set(report.left_out) == {second, staging.id}
    assert {s.id for s in restored.list_snapshots(table_id)} == set(report.snapshots)
    assert [h.reason for h in restored.live_holds(first)] == ["keep through restore"]
    assert restored.live_lease_count(first) == 0


def test_nothing_is_overwritten(catalog: TableCatalog, tmp_path: Path) -> None:
    _commit(catalog, tmp_path, "run-1", [1])
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("mine", encoding="utf-8")

    with pytest.raises(BackupInvalid, match="not empty"):
        backup(catalog, occupied)
    backup(catalog, tmp_path / "backup")
    with pytest.raises(BackupInvalid, match="not empty"):
        restore(tmp_path / "backup", occupied)
    assert (occupied / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_a_damaged_live_snapshot_is_not_backed_up_as_whole(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    snapshot_id = _commit(catalog, tmp_path, "run-1", [1])
    table_id = catalog.get_snapshot(snapshot_id).table_id
    live = SnapshotLayout(catalog.root, table_id).snapshot_dir(snapshot_id)
    thaw(live)
    (live / "table.parquet").write_bytes(b"rot")

    with pytest.raises(BackupInvalid, match="damaged"):
        backup(catalog, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    # The lease taken for the copy is released even when the backup fails.
    assert catalog.live_lease_count(snapshot_id) == 0


def test_the_cli_round_trip(
    catalog: TableCatalog, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _commit(catalog, tmp_path, "run-1", [7, 8, 9])
    before = _read_current(catalog)

    assert main(["warehouse-backup", str(catalog.root), str(tmp_path / "backup")]) == 0
    assert main(["warehouse-restore", str(tmp_path / "backup"), str(tmp_path / "restored")]) == 0
    assert _read_current(TableCatalog(tmp_path / "restored")).equals(before)

    assert main(["warehouse-restore", str(tmp_path / "backup"), str(tmp_path / "restored")]) == 1
    assert "nothing was restored" in capsys.readouterr().err

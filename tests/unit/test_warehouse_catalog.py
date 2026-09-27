"""The five acceptance criteria of #699.

A warehouse promises that a committed table is immutable and that a refresh moves
a pointer. Those promises only hold under concurrency and crashes, so that is what
these tests do — two commits racing, a crash injected at every step, and garbage
collection running against a live reader.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from kpubdata_builder.warehouse import (
    ImmutableSnapshot,
    SnapshotConflict,
    SnapshotLayout,
    SnapshotManifest,
    SnapshotNotFound,
    SnapshotStateError,
    TableCatalog,
    TableNotFound,
)
from kpubdata_builder.warehouse import gc as warehouse_gc

WORKSPACE = "ws_personal"


@pytest.fixture
def catalog(tmp_path: Path) -> TableCatalog:
    """A catalog rooted in a temporary warehouse."""
    return TableCatalog(tmp_path / "warehouse")


def _write_snapshot(
    catalog: TableCatalog,
    table_id: str,
    *,
    run_id: str,
    rows: str = "a,b\n1,2\n",
    coverage: str = "cov-1",
    digest: str = "sha256:deadbeef",
) -> str:
    """Take a snapshot all the way to validated, without committing it."""
    snapshot = catalog.begin_snapshot(
        table_id,
        run_id=run_id,
        schema_version=1,
        coverage_hash=coverage,
        artifact_digest=digest,
        row_count=1,
    )
    layout = SnapshotLayout(catalog.root, table_id)
    staging = layout.begin(snapshot.id)
    (staging / "part-0.csv").write_text(rows, encoding="utf-8")
    layout.write_manifest(
        snapshot.id,
        SnapshotManifest(
            snapshot_id=snapshot.id,
            table_id=table_id,
            logical_name="t",
            run_id=run_id,
            schema_version=1,
            coverage_hash=coverage,
            artifact_digest=digest,
            row_count=1,
            created_at=snapshot.created_at,
        ),
    )
    catalog.mark_validated(snapshot.id)
    return snapshot.id


def _commit(catalog: TableCatalog, table_id: str, snapshot_id: str) -> None:
    """Promote the files and then move the pointer."""
    SnapshotLayout(catalog.root, table_id).promote(snapshot_id)
    catalog.commit_snapshot(snapshot_id, expected_revision=catalog.get_table(table_id).revision)


# --------------------------------------------------------------------- basics


def test_a_new_table_has_no_current_snapshot(catalog: TableCatalog) -> None:
    """A table exists before anything is committed to it."""
    table = catalog.create_table(WORKSPACE, "sales")
    assert table.current_snapshot_id is None
    assert table.revision == 0
    with pytest.raises(SnapshotNotFound):
        catalog.resolve_current(table.id)


def test_creating_the_same_name_twice_returns_the_same_table(catalog: TableCatalog) -> None:
    """A logical name is unique per workspace."""
    first = catalog.create_table(WORKSPACE, "sales")
    second = catalog.create_table(WORKSPACE, "sales")
    assert first.id == second.id


def test_unknown_table_and_snapshot_are_reported(catalog: TableCatalog) -> None:
    """Missing rows raise rather than returning None."""
    with pytest.raises(TableNotFound):
        catalog.get_table("tbl_nope")
    with pytest.raises(SnapshotNotFound):
        catalog.get_snapshot("snap_nope")


def test_commit_refuses_an_unvalidated_snapshot(catalog: TableCatalog) -> None:
    """Validation is not optional — an unvalidated snapshot cannot become current."""
    table = catalog.create_table(WORKSPACE, "sales")
    snapshot = catalog.begin_snapshot(
        table.id,
        run_id="run-1",
        schema_version=1,
        coverage_hash="cov",
        artifact_digest="sha256:0",
    )
    with pytest.raises(SnapshotStateError, match="validated"):
        catalog.commit_snapshot(snapshot.id, expected_revision=0)
    assert catalog.get_table(table.id).current_snapshot_id is None


def test_the_manifest_survives_promotion(catalog: TableCatalog) -> None:
    """The manifest written into staging is readable from the committed path."""
    table = catalog.create_table(WORKSPACE, "sales")
    snapshot_id = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, snapshot_id)

    manifest = SnapshotLayout(catalog.root, table.id).read_manifest(snapshot_id)
    assert manifest.snapshot_id == snapshot_id
    assert manifest.run_id == "run-1"
    assert manifest.coverage_hash == "cov-1"


# ------------------------------------------------- criterion 1: concurrency


def test_two_concurrent_commits_leave_exactly_one_winner(catalog: TableCatalog) -> None:
    """Exactly one of two commits succeeds; the other reports a conflict.

    Both read revision 0 before either writes, which is the lost-update race the
    compare-and-swap exists to lose loudly rather than silently.
    """
    table = catalog.create_table(WORKSPACE, "sales")
    first = _write_snapshot(catalog, table.id, run_id="run-1")
    second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
    layout = SnapshotLayout(catalog.root, table.id)
    layout.promote(first)
    layout.promote(second)

    expected = catalog.get_table(table.id).revision
    results: dict[str, BaseException | None] = {}
    barrier = threading.Barrier(2)

    def attempt(snapshot_id: str) -> None:
        barrier.wait()
        try:
            catalog.commit_snapshot(snapshot_id, expected_revision=expected)
            results[snapshot_id] = None
        except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
            results[snapshot_id] = exc

    threads = [threading.Thread(target=attempt, args=(s,)) for s in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [s for s, exc in results.items() if exc is None]
    losers = [(s, exc) for s, exc in results.items() if exc is not None]
    assert len(winners) == 1, f"expected one winner, got {results}"
    assert len(losers) == 1
    assert isinstance(losers[0][1], SnapshotConflict)

    table_after = catalog.get_table(table.id)
    assert table_after.current_snapshot_id == winners[0]
    assert table_after.revision == expected + 1
    # The loser stays validated. It is not current, and it is not corrupt either.
    assert catalog.get_snapshot(losers[0][0]).state == "validated"


def test_a_stale_expected_revision_conflicts(catalog: TableCatalog) -> None:
    """A commit built on a revision that has moved on is refused."""
    table = catalog.create_table(WORKSPACE, "sales")
    _commit(catalog, table.id, _write_snapshot(catalog, table.id, run_id="run-1"))
    second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
    SnapshotLayout(catalog.root, table.id).promote(second)

    with pytest.raises(SnapshotConflict, match="revision 0"):
        catalog.commit_snapshot(second, expected_revision=0)


def test_a_conflict_does_not_move_the_pointer(catalog: TableCatalog) -> None:
    """Criterion 4: a failed refresh leaves current_snapshot_id alone."""
    table = catalog.create_table(WORKSPACE, "sales")
    first = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, first)
    before = catalog.get_table(table.id)

    second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
    SnapshotLayout(catalog.root, table.id).promote(second)
    with pytest.raises(SnapshotConflict):
        catalog.commit_snapshot(second, expected_revision=99)

    after = catalog.get_table(table.id)
    assert after.current_snapshot_id == before.current_snapshot_id == first
    assert after.revision == before.revision


# ------------------------------------------------ criterion 2: crash safety


@pytest.mark.parametrize(
    "crash_after",
    ["begin", "write_files", "write_manifest", "validate", "promote"],
)
def test_the_previous_snapshot_survives_a_crash_at_any_step(
    catalog: TableCatalog, crash_after: str
) -> None:
    """Criterion 2: whatever step a refresh dies on, the old data is still readable.

    Each step is performed and then abandoned. Nothing overwrites the previous
    snapshot's directory at any point, which is the property the old
    replace-the-directory approach could not offer.
    """
    table = catalog.create_table(WORKSPACE, "sales")
    first = _write_snapshot(catalog, table.id, run_id="run-1", rows="a,b\n1,2\n")
    _commit(catalog, table.id, first)
    layout = SnapshotLayout(catalog.root, table.id)
    original = (layout.snapshot_dir(first) / "part-0.csv").read_text(encoding="utf-8")

    steps = ["begin", "write_files", "write_manifest", "validate", "promote"]
    stop = steps.index(crash_after)

    second = catalog.begin_snapshot(
        table.id,
        run_id="run-2",
        schema_version=1,
        coverage_hash="cov-2",
        artifact_digest="sha256:beef",
    )
    if stop >= steps.index("write_files"):
        staging = layout.begin(second.id)
        (staging / "part-0.csv").write_text("a,b\n9,9\n", encoding="utf-8")
    if stop >= steps.index("write_manifest"):
        layout.write_manifest(
            second.id,
            SnapshotManifest(
                snapshot_id=second.id,
                table_id=table.id,
                logical_name="t",
                run_id="run-2",
                schema_version=1,
                coverage_hash="cov-2",
                artifact_digest="sha256:beef",
                row_count=1,
                created_at=second.created_at,
            ),
        )
    if stop >= steps.index("validate"):
        catalog.mark_validated(second.id)
    if stop >= steps.index("promote"):
        layout.promote(second.id)
    # The pointer is never moved: that is the crash.

    reopened = TableCatalog(catalog.root)
    pin = reopened.resolve_current(table.id)
    assert pin.snapshot_id == first
    assert (layout.snapshot_dir(first) / "part-0.csv").read_text(encoding="utf-8") == original
    assert reopened.get_snapshot(second.id).state != "committed"


def test_promote_refuses_to_overwrite_a_committed_snapshot(catalog: TableCatalog) -> None:
    """A committed directory is never written over, even when asked directly."""
    table = catalog.create_table(WORKSPACE, "sales")
    snapshot_id = _write_snapshot(catalog, table.id, run_id="run-1")
    layout = SnapshotLayout(catalog.root, table.id)
    layout.promote(snapshot_id)

    layout.begin(snapshot_id)
    with pytest.raises(ImmutableSnapshot):
        layout.promote(snapshot_id)


def test_promote_without_staging_is_an_error(catalog: TableCatalog) -> None:
    """Promoting something that was never written is a mistake, not a no-op."""
    table = catalog.create_table(WORKSPACE, "sales")
    with pytest.raises(FileNotFoundError):
        SnapshotLayout(catalog.root, table.id).promote("snap_missing")


# ---------------------------------------------- criterion 5: query pinning


def test_a_query_is_not_affected_by_a_commit_mid_flight(catalog: TableCatalog) -> None:
    """Criterion 5: the pointer is resolved once, at the start."""
    table = catalog.create_table(WORKSPACE, "sales")
    first = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, first)

    pin = catalog.resolve_current(table.id)
    assert pin.snapshot_id == first

    second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
    _commit(catalog, table.id, second)

    # The pin still names the snapshot the query started on.
    assert pin.snapshot_id == first
    assert catalog.get_table(table.id).current_snapshot_id == second
    # And a query starting now sees the new one.
    assert catalog.resolve_current(table.id).snapshot_id == second


# ----------------------------------------------- criterion 3: GC and leases


def test_gc_will_not_delete_a_snapshot_a_query_is_reading(catalog: TableCatalog) -> None:
    """Criterion 3: a live lease keeps garbage collection away."""
    table = catalog.create_table(WORKSPACE, "sales")
    first = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, first)

    with catalog.pinned(table.id) as pin:
        assert pin.snapshot_id == first
        second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
        _commit(catalog, table.id, second)

        report = warehouse_gc.collect(catalog, table.id, keep=0)
        assert first in report.kept_leased
        assert first not in report.snapshots_removed
        layout = SnapshotLayout(catalog.root, table.id)
        assert (layout.snapshot_dir(first) / "part-0.csv").exists()

    # Once the lease is released the same pass reclaims it.
    report = warehouse_gc.collect(catalog, table.id, keep=0)
    assert first in report.snapshots_removed
    assert not SnapshotLayout(catalog.root, table.id).snapshot_dir(first).exists()
    with pytest.raises(SnapshotNotFound):
        catalog.get_snapshot(first)


def test_gc_never_deletes_the_current_snapshot(catalog: TableCatalog) -> None:
    """Even with keep=0 the current snapshot stays."""
    table = catalog.create_table(WORKSPACE, "sales")
    only = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, only)

    report = warehouse_gc.collect(catalog, table.id, keep=0)
    assert report.kept_current == [only]
    assert report.snapshots_removed == []
    assert SnapshotLayout(catalog.root, table.id).snapshot_dir(only).exists()


def test_deleting_the_current_snapshot_directly_is_refused(catalog: TableCatalog) -> None:
    """The gate holds even when GC is bypassed."""
    table = catalog.create_table(WORKSPACE, "sales")
    only = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, only)

    with pytest.raises(ImmutableSnapshot):
        warehouse_gc.delete_snapshot(catalog, only)
    with pytest.raises(ImmutableSnapshot):
        catalog.assert_deletable(only)


def test_an_expired_lease_stops_protecting(catalog: TableCatalog) -> None:
    """A crashed reader cannot pin a snapshot forever."""
    table = catalog.create_table(WORKSPACE, "sales")
    first = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, first)
    second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
    _commit(catalog, table.id, second)

    catalog.resolve_current(table.id, lease_seconds=1)  # never released
    assert catalog.live_lease_count(second) == 1

    far_future = "2999-01-01T00:00:00+00:00"
    assert catalog.live_lease_count(second, now=far_future) == 0
    assert catalog.purge_expired_leases(now=far_future) == 1
    assert catalog.live_lease_count(second) == 0

    # With no live lease left, the superseded snapshot becomes collectable.
    assert catalog.assert_deletable(first).id == first


def test_gc_removes_orphaned_staging_directories(catalog: TableCatalog) -> None:
    """Staging the catalog never recorded is what a crash leaves behind."""
    table = catalog.create_table(WORKSPACE, "sales")
    layout = SnapshotLayout(catalog.root, table.id)
    orphan = layout.staging_root / "snap_orphan"
    orphan.mkdir(parents=True)
    (orphan / "part-0.csv").write_text("x\n", encoding="utf-8")

    in_progress = catalog.begin_snapshot(
        table.id,
        run_id="run-1",
        schema_version=1,
        coverage_hash="cov",
        artifact_digest="sha256:0",
    )
    layout.begin(in_progress.id)

    removed = warehouse_gc.collect_orphan_staging(catalog, table.id)
    assert removed == ["snap_orphan"]
    assert not orphan.exists()
    # Work the catalog knows about is still in progress and must survive.
    assert layout.staging_dir(in_progress.id).exists()


def test_quarantine_cannot_be_applied_to_the_current_snapshot(catalog: TableCatalog) -> None:
    """Quarantining the only readable snapshot would leave nothing to read."""
    table = catalog.create_table(WORKSPACE, "sales")
    only = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, only)

    with pytest.raises(SnapshotStateError, match="current"):
        catalog.quarantine(only)

    second = _write_snapshot(catalog, table.id, run_id="run-2", coverage="cov-2")
    _commit(catalog, table.id, second)
    catalog.quarantine(only)
    assert catalog.get_snapshot(only).state == "quarantined"


# -------------------------------------------------------------- durability


def test_a_reopened_catalog_sees_the_same_pointer(catalog: TableCatalog) -> None:
    """State is on disk, not in the process."""
    table = catalog.create_table(WORKSPACE, "sales")
    snapshot_id = _write_snapshot(catalog, table.id, run_id="run-1")
    _commit(catalog, table.id, snapshot_id)
    catalog.close()

    reopened = TableCatalog(catalog.root)
    assert reopened.get_table(table.id).current_snapshot_id == snapshot_id


def test_a_future_schema_version_refuses_to_open(catalog: TableCatalog, tmp_path: Path) -> None:
    """A canonical catalog is never silently recreated."""
    catalog.create_table(WORKSPACE, "sales")
    catalog.close()
    path = catalog.root / "_warehouse.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("UPDATE schema_version SET version = 999")
    conn.close()

    with pytest.raises(SnapshotStateError, match="migration"):
        TableCatalog(catalog.root)


def test_a_path_traversing_identifier_is_refused(catalog: TableCatalog) -> None:
    """Table and snapshot identifiers become path segments, so they are validated."""
    with pytest.raises(ValueError):
        SnapshotLayout(catalog.root, "../escape")
    table = catalog.create_table(WORKSPACE, "sales")
    with pytest.raises(ValueError):
        SnapshotLayout(catalog.root, table.id).snapshot_dir("../escape")

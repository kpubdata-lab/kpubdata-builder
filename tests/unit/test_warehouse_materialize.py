"""A build has to be able to end at a committed table (#703).

The promise is "collect Korean public data into my own environment and query it", and
that is kept with nothing published. Until now a build could not reach a committed
table at all, because nothing wrote to the catalog.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.warehouse import (
    SnapshotLayout,
    SnapshotStateError,
    TableCatalog,
    materialize,
)
from kpubdata_builder.warehouse.layout import MANIFEST_FILENAME, thaw

WORKSPACE = "ws_personal"
ALICE = "oidc:issuer|alice"


@pytest.fixture
def catalog(tmp_path: Path) -> TableCatalog:
    """A catalog rooted in a temporary warehouse."""
    return TableCatalog(tmp_path / "warehouse")


@pytest.fixture
def gold(tmp_path: Path) -> Path:
    """A finished Gold directory, as persist_gold_package leaves one."""
    directory = tmp_path / "run-1" / "gold" / "datago.apt_trade"
    directory.mkdir(parents=True)
    (directory / "table.parquet").write_bytes(b"PAR1fake")
    (directory / "package.json").write_text('{"rows": 2}', encoding="utf-8")
    splits = directory / "splits"
    splits.mkdir()
    (splits / "train.parquet").write_bytes(b"PAR1train")
    return directory


def test_a_build_reaches_a_committed_snapshot(catalog: TableCatalog, gold: Path) -> None:
    """The end state #703 asks for, with no publish credential in sight."""
    result = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
        owner_id=ALICE,
        row_count=2,
    )

    assert result.snapshot.state == "committed"
    assert result.table.current_snapshot_id == result.snapshot.id
    assert result.table.revision == 1
    assert result.snapshot.owner_id == ALICE
    assert result.snapshot.row_count == 2

    # The files are at their final path, and the manifest went with them.
    assert (result.snapshot_dir / "table.parquet").read_bytes() == b"PAR1fake"
    assert (result.snapshot_dir / "splits" / "train.parquet").exists()
    assert (result.snapshot_dir / MANIFEST_FILENAME).is_file()


def test_the_run_workspace_is_left_intact(catalog: TableCatalog, gold: Path) -> None:
    """Copied, not moved: the run manifest's output paths point into the workspace."""
    materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
    )

    assert (gold / "table.parquet").exists()
    assert (gold / "splits" / "train.parquet").exists()


def test_the_digest_describes_what_was_written(catalog: TableCatalog, gold: Path) -> None:
    """Recorded after the bytes land, which is the only order verification can check.

    A digest recorded before the write describes something that may not have happened,
    and `verify_before_commit` would then compare a claim against itself.
    """
    result = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
    )

    from kpubdata_builder.warehouse.layout import content_digest

    assert result.snapshot.artifact_digest == content_digest(result.snapshot_dir)
    assert result.snapshot.artifact_digest.startswith("sha256:")

    # And the manifest agrees with the row.
    manifest = SnapshotLayout(catalog.root, result.table.id).read_manifest(result.snapshot.id)
    assert manifest.artifact_digest == result.snapshot.artifact_digest


def test_an_empty_gold_directory_does_not_become_a_table(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    """A build that produced nothing must not replace a table with emptiness."""
    empty = tmp_path / "empty-gold"
    empty.mkdir()

    with pytest.raises(SnapshotStateError, match="no files"):
        materialize(
            catalog,
            workspace_id=WORKSPACE,
            logical_name="apt_trade",
            source_dir=empty,
            run_id="run-1",
        )

    table = catalog.list_tables(WORKSPACE)[0]
    assert catalog.get_table(table.id).current_snapshot_id is None


def test_a_second_materialise_moves_the_pointer_and_keeps_the_first(
    catalog: TableCatalog, gold: Path, tmp_path: Path
) -> None:
    """A refresh writes a new snapshot; the previous one is not touched."""
    first = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
    )

    second_gold = tmp_path / "run-2" / "gold" / "datago.apt_trade"
    second_gold.mkdir(parents=True)
    (second_gold / "table.parquet").write_bytes(b"PAR1newer")
    second = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=second_gold,
        run_id="run-2",
    )

    assert second.table.current_snapshot_id == second.snapshot.id
    assert second.table.revision == 2
    # The first snapshot's bytes are exactly as they were.
    assert (first.snapshot_dir / "table.parquet").read_bytes() == b"PAR1fake"
    assert catalog.get_snapshot(first.snapshot.id).state == "committed"


def test_the_committed_snapshot_is_read_only(catalog: TableCatalog, gold: Path) -> None:
    """A guard against mistakes, not a security boundary — but it should be there."""
    result = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
    )
    target = result.snapshot_dir / "table.parquet"

    with pytest.raises(PermissionError):
        target.write_bytes(b"overwritten")

    # The owner can undo it, which is why the contract rests on promote() refusing an
    # existing path rather than on this.
    thaw(result.snapshot_dir)
    target.write_bytes(b"overwritten")


def test_a_missing_gold_directory_is_an_error(catalog: TableCatalog, tmp_path: Path) -> None:
    """Materialising nothing is a mistake, not an empty table."""
    with pytest.raises(FileNotFoundError):
        materialize(
            catalog,
            workspace_id=WORKSPACE,
            logical_name="apt_trade",
            source_dir=tmp_path / "does-not-exist",
            run_id="run-1",
        )


def test_the_digest_cannot_be_rewritten_after_commit(catalog: TableCatalog, gold: Path) -> None:
    """The digest is what verification compares against; rewriting it is circular."""
    result = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
    )

    with pytest.raises(SnapshotStateError, match="cannot be rewritten"):
        catalog.set_artifact_digest(result.snapshot.id, "sha256:something-else")


def test_fingerprints_are_carried_through(catalog: TableCatalog, gold: Path) -> None:
    """Drift baseline selection reads these, and a missing one means "cannot compare"."""
    result = materialize(
        catalog,
        workspace_id=WORKSPACE,
        logical_name="apt_trade",
        source_dir=gold,
        run_id="run-1",
        owner_id=ALICE,
        coverage_fingerprint="region=seoul;period=2025",
        source_params_fingerprint="LAWD_CD=11110",
        schema_contract_version="v1",
    )

    assert result.snapshot.coverage_fingerprint == "region=seoul;period=2025"
    assert result.snapshot.source_params_fingerprint == "LAWD_CD=11110"
    assert result.snapshot.schema_contract_version == "v1"


class TestARefusedCommitDoesNotLeaveItsBytes:
    """A snapshot that will never be committed must become reclaimable (#738).

    #733 added the `abandoned` state and taught GC to collect it. Nothing set it, so a
    compare-and-swap loser stayed `validated` for ever with its files promoted, and
    garbage collection walked past because the catalog knew about it.
    """

    def test_a_losing_commit_is_abandoned_and_collected(
        self, catalog: TableCatalog, gold: Path, tmp_path: Path
    ) -> None:
        """Two refreshes race; the loser's bytes do not stay on disk."""
        from kpubdata_builder.warehouse import SnapshotConflict
        from kpubdata_builder.warehouse import gc as warehouse_gc

        first = materialize(
            catalog,
            workspace_id=WORKSPACE,
            logical_name="apt_trade",
            source_dir=gold,
            run_id="run-1",
        )

        # A second refresh that reads the revision before the third commits. Simulated
        # by committing something else in between, which is what a race produces.
        second_gold = tmp_path / "run-2" / "gold" / "datago.apt_trade"
        second_gold.mkdir(parents=True)
        (second_gold / "table.parquet").write_bytes(b"PAR1second")
        third_gold = tmp_path / "run-3" / "gold" / "datago.apt_trade"
        third_gold.mkdir(parents=True)
        (third_gold / "table.parquet").write_bytes(b"PAR1third")

        materialize(
            catalog,
            workspace_id=WORKSPACE,
            logical_name="apt_trade",
            source_dir=second_gold,
            run_id="run-2",
        )

        # Force the loss: commit against a revision that has moved on.
        snapshot = catalog.begin_snapshot(
            first.table.id,
            run_id="run-3",
            schema_version=1,
            coverage_hash="",
            artifact_digest="",
        )
        layout = SnapshotLayout(catalog.root, first.table.id)
        staging = layout.begin(snapshot.id)
        (staging / "table.parquet").write_bytes(b"PAR1third")
        catalog.mark_validated(snapshot.id)
        layout.promote(snapshot.id)
        with pytest.raises(SnapshotConflict):
            catalog.commit_snapshot(snapshot.id, expected_revision=0)

        # Left alone, this is the leak: validated for ever, files on disk, GC skips it.
        assert catalog.get_snapshot(snapshot.id).state == "validated"
        assert (
            snapshot.id
            not in warehouse_gc.collect(catalog, first.table.id, keep=0).snapshots_removed
        )

        # materialize() marks it instead, so the same pass reclaims it.
        catalog.abandon(snapshot.id)
        assert (
            snapshot.id in warehouse_gc.collect(catalog, first.table.id, keep=0).snapshots_removed
        )

    def test_an_empty_source_leaves_nothing_behind(
        self, catalog: TableCatalog, tmp_path: Path
    ) -> None:
        """The refusal path abandons too, so a rejected build does not accumulate."""
        from kpubdata_builder.warehouse import SnapshotStateError

        empty = tmp_path / "empty-gold"
        empty.mkdir()

        with pytest.raises(SnapshotStateError):
            materialize(
                catalog,
                workspace_id=WORKSPACE,
                logical_name="apt_trade",
                source_dir=empty,
                run_id="run-1",
            )

        table = catalog.list_tables(WORKSPACE)[0]
        snapshots = catalog.list_snapshots(table.id)
        assert [s.state for s in snapshots] == ["abandoned"]

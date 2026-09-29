"""The four acceptance criteria of #700.

Three of them are negative: what must *not* be chosen as a baseline, and what must
*not* be reported as healthy. A drift check that picks the wrong baseline still
produces numbers, so only a test that asserts the absence catches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.warehouse import SnapshotLayout, SnapshotManifest, TableCatalog
from kpubdata_builder.warehouse.baseline import (
    BaselineFound,
    DriftAxis,
    NotEvaluated,
    NotEvaluatedReason,
    select_baseline,
)

WORKSPACE = "ws_personal"
ALICE = "oidc:issuer|alice"
BOB = "oidc:issuer|bob"
SEOUL_2025 = "region=seoul;period=2025"
BUSAN_2026 = "region=busan;period=2026"
CONTRACT_1 = "v1"
CONTRACT_2 = "v2"


@pytest.fixture
def catalog(tmp_path: Path) -> TableCatalog:
    """A catalog rooted in a temporary warehouse."""
    return TableCatalog(tmp_path / "warehouse")


def _commit(
    catalog: TableCatalog,
    table_id: str,
    *,
    run_id: str,
    owner_id: str | None,
    coverage: str | None,
    contract: str | None,
    row_count: int = 100,
) -> str:
    """Take a snapshot all the way to committed."""
    snapshot = catalog.begin_snapshot(
        table_id,
        run_id=run_id,
        schema_version=1,
        coverage_hash=coverage or "unknown",
        artifact_digest=f"sha256:{run_id}",
        row_count=row_count,
        owner_id=owner_id,
        coverage_fingerprint=coverage,
        source_params_fingerprint=coverage,
        schema_contract_version=contract,
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
            coverage_hash=coverage or "unknown",
            artifact_digest=f"sha256:{run_id}",
            row_count=row_count,
            created_at=snapshot.created_at,
        ),
    )
    catalog.mark_validated(snapshot.id)
    layout.promote(snapshot.id)
    catalog.commit_snapshot(snapshot.id, expected_revision=catalog.get_table(table_id).revision)
    return snapshot.id


# ------------------------------------------- criterion 1: owner isolation


def test_another_users_run_is_never_the_baseline(catalog: TableCatalog) -> None:
    """Criterion 1 (negative): Alice's snapshot is not Bob's baseline.

    Row count, schema and distribution are metadata about Alice's data, and drift is
    exactly what reports them.
    """
    table = catalog.create_table(WORKSPACE, "trade")
    alice = _commit(
        catalog,
        table.id,
        run_id="alice-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=BOB,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(outcome, NotEvaluated)
    assert not outcome.evaluated
    # Alice's snapshot was not returned under any guise — and the reason does not
    # even say that someone else's snapshot exists (N-04).
    assert alice not in outcome.detail
    assert ALICE not in outcome.detail

    # Alice still gets her own.
    mine = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(mine, BaselineFound)
    assert mine.snapshot.id == alice


def test_an_unowned_snapshot_is_rejected_not_assumed(catalog: TableCatalog) -> None:
    """A snapshot with no recorded owner is not treated as ours."""
    table = catalog.create_table(WORKSPACE, "trade")
    _commit(
        catalog,
        table.id,
        run_id="legacy-1",
        owner_id=None,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.OWNER_UNKNOWN


# ----------------------------------------- criterion 2: coverage isolation


def test_different_coverage_does_not_share_a_volume_baseline(catalog: TableCatalog) -> None:
    """Criterion 2 (negative): Seoul 2025 is not the baseline for Busan 2026.

    The row-count difference between two populations is not drift; it is a
    different question being answered by accident.
    """
    table = catalog.create_table(WORKSPACE, "trade")
    _commit(
        catalog,
        table.id,
        run_id="seoul-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
        row_count=1000,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=BUSAN_2026,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.COVERAGE_MISMATCH
    assert "meaningless" in outcome.detail


def test_schema_drift_tolerates_a_coverage_change(catalog: TableCatalog) -> None:
    """Schema and volume do not share a baseline, and that is the point.

    Which region was collected should not change the column set. If it does, that is
    drift worth reporting rather than a reason to skip the check.
    """
    table = catalog.create_table(WORKSPACE, "trade")
    seoul = _commit(
        catalog,
        table.id,
        run_id="seoul-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )

    schema = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.SCHEMA,
        owner_id=ALICE,
        coverage_fingerprint=BUSAN_2026,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(schema, BaselineFound)
    assert schema.snapshot.id == seoul
    assert schema.axis is DriftAxis.SCHEMA

    volume = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=BUSAN_2026,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(volume, NotEvaluated)


# ------------------------------- criterion 3: absence is not health


def test_no_committed_snapshot_is_not_evaluated_not_healthy(catalog: TableCatalog) -> None:
    """Criterion 3 (negative): an empty table reports not_evaluated.

    ``list_snapshots`` returns an empty list and the catalog does not know whether
    that means healthy. The outcome type makes the distinction impossible to lose:
    there is no ``None`` to mistake for "fine", and no empty finding list.
    """
    table = catalog.create_table(WORKSPACE, "trade")

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.NO_COMMITTED_SNAPSHOT
    assert outcome.evaluated is False
    # The thing that must not happen: a truthy "found" result, or a bare None that a
    # caller can write `if baseline is None: healthy` against.
    assert not isinstance(outcome, BaselineFound)
    assert outcome is not None


def test_an_uncommitted_snapshot_is_not_a_baseline(catalog: TableCatalog) -> None:
    """Staging and validated snapshots are not a previous state anyone read."""
    table = catalog.create_table(WORKSPACE, "trade")
    snapshot = catalog.begin_snapshot(
        table.id,
        run_id="run-1",
        schema_version=1,
        coverage_hash="cov",
        artifact_digest="sha256:0",
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
    )
    catalog.mark_validated(snapshot.id)

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.SCHEMA,
        owner_id=ALICE,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.NO_COMMITTED_SNAPSHOT


def test_a_quarantined_snapshot_is_not_a_baseline(catalog: TableCatalog) -> None:
    """A snapshot pulled out of service does not come back as a comparison point."""
    table = catalog.create_table(WORKSPACE, "trade")
    first = _commit(
        catalog,
        table.id,
        run_id="run-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )
    second = _commit(
        catalog,
        table.id,
        run_id="run-2",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )
    catalog.quarantine(first)

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
        exclude_snapshot_id=second,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.NO_COMMITTED_SNAPSHOT


def test_a_run_is_not_its_own_baseline(catalog: TableCatalog) -> None:
    """Excluding the current snapshot keeps a refresh from comparing to itself."""
    table = catalog.create_table(WORKSPACE, "trade")
    only = _commit(
        catalog,
        table.id,
        run_id="run-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.SCHEMA,
        owner_id=ALICE,
        exclude_snapshot_id=only,
    )
    assert isinstance(outcome, NotEvaluated)


# ------------------------- criterion 4: a contract change invalidates volume


def test_a_schema_contract_change_invalidates_the_volume_baseline(
    catalog: TableCatalog,
) -> None:
    """Criterion 4: the comparison is refused, not made quietly."""
    table = catalog.create_table(WORKSPACE, "trade")
    _commit(
        catalog,
        table.id,
        run_id="run-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_2,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.SCHEMA_CONTRACT_CHANGED

    # Schema drift is still evaluated — that is how the contract change shows up.
    schema = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.SCHEMA,
        owner_id=ALICE,
        schema_contract_version=CONTRACT_2,
    )
    assert isinstance(schema, BaselineFound)


def test_missing_current_coverage_blocks_a_volume_comparison(catalog: TableCatalog) -> None:
    """An unlabelled current run cannot be compared by volume either."""
    table = catalog.create_table(WORKSPACE, "trade")
    _commit(
        catalog,
        table.id,
        run_id="run-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=None,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.COVERAGE_UNKNOWN


def test_the_newest_comparable_snapshot_wins(catalog: TableCatalog) -> None:
    """Among comparable candidates the most recent is chosen."""
    table = catalog.create_table(WORKSPACE, "trade")
    _commit(
        catalog,
        table.id,
        run_id="run-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )
    newer = _commit(
        catalog,
        table.id,
        run_id="run-2",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )
    # A newer but incomparable snapshot must not displace it.
    _commit(
        catalog,
        table.id,
        run_id="run-3",
        owner_id=ALICE,
        coverage=BUSAN_2026,
        contract=CONTRACT_1,
    )

    outcome = select_baseline(
        catalog,
        table.id,
        axis=DriftAxis.VOLUME,
        owner_id=ALICE,
        coverage_fingerprint=SEOUL_2025,
        schema_contract_version=CONTRACT_1,
    )
    assert isinstance(outcome, BaselineFound)
    assert outcome.snapshot.id == newer


# ------------------------------------------------------------- migration


def test_a_version_1_catalog_migrates_in_place(tmp_path: Path) -> None:
    """The columns #700 needs are added to an existing catalog, not rebuilt.

    A canonical catalog cannot be dropped and recreated, so the upgrade has to
    preserve the rows that are already there.
    """
    import sqlite3

    root = tmp_path / "warehouse"
    catalog = TableCatalog(root)
    table = catalog.create_table(WORKSPACE, "trade")
    snapshot_id = _commit(
        catalog,
        table.id,
        run_id="run-1",
        owner_id=ALICE,
        coverage=SEOUL_2025,
        contract=CONTRACT_1,
    )
    catalog.close()

    # Rewind to the v1 shape: drop the four columns and set the version back.
    path = root / "_warehouse.sqlite"
    conn = sqlite3.connect(str(path), isolation_level=None)
    for column in (
        "owner_id",
        "coverage_fingerprint",
        "source_params_fingerprint",
        "schema_contract_version",
    ):
        conn.execute(f"ALTER TABLE table_snapshots DROP COLUMN {column}")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (1)")
    conn.close()

    reopened = TableCatalog(root)
    # The row survived, and the new columns read as unknown rather than as a match.
    row = reopened.get_snapshot(snapshot_id)
    assert row.run_id == "run-1"
    assert row.owner_id is None
    assert reopened.get_table(table.id).current_snapshot_id == snapshot_id

    # And an unknown owner is rejected, not assumed.
    outcome = select_baseline(
        reopened,
        table.id,
        axis=DriftAxis.SCHEMA,
        owner_id=ALICE,
    )
    assert isinstance(outcome, NotEvaluated)
    assert outcome.reason is NotEvaluatedReason.OWNER_UNKNOWN


def test_the_reason_does_not_reveal_that_other_owners_have_snapshots(
    catalog: TableCatalog, tmp_path: Path
) -> None:
    """N-04 (negative): "only other people's snapshots" reads exactly like "none".

    The count of another owner's snapshots, or the fact that there are any, is the
    same metadata side channel the owner filter closes. The reason and the text must
    be identical to an empty table's, and carry no number.
    """
    crowded = catalog.create_table(WORKSPACE, "trade")
    for index in range(3):
        _commit(
            catalog,
            crowded.id,
            run_id=f"alice-{index}",
            owner_id=ALICE,
            coverage=SEOUL_2025,
            contract=CONTRACT_1,
        )
    other = TableCatalog(tmp_path / "other")
    empty = other.create_table(WORKSPACE, "trade", table_id=crowded.id)

    for axis in DriftAxis:
        seen = select_baseline(
            catalog,
            crowded.id,
            axis=axis,
            owner_id=BOB,
            coverage_fingerprint=SEOUL_2025,
            schema_contract_version=CONTRACT_1,
        )
        nothing = select_baseline(
            other,
            empty.id,
            axis=axis,
            owner_id=BOB,
            coverage_fingerprint=SEOUL_2025,
            schema_contract_version=CONTRACT_1,
        )
        assert isinstance(seen, NotEvaluated) and isinstance(nothing, NotEvaluated)
        assert (seen.reason, seen.detail) == (nothing.reason, nothing.detail)
        assert not any(ch.isdigit() for ch in seen.detail.replace(crowded.id, ""))


def test_the_file_path_and_the_catalog_share_one_reason_vocabulary() -> None:
    """`stages.silver.drift` repeats these as literals to avoid importing the warehouse.

    Its comment promises a test keeps the two in step; this is that test. A reason the
    execution path writes to a manifest must be one a catalog reader understands.
    """
    from kpubdata_builder.stages.silver import drift

    literals = {
        drift.NO_COMMITTED_SNAPSHOT,
        drift.OWNER_UNKNOWN,
        drift.COVERAGE_MISMATCH,
        drift.COVERAGE_UNKNOWN,
        drift.SCHEMA_CONTRACT_CHANGED,
    }
    assert literals == {reason.value for reason in NotEvaluatedReason}

"""Drift detection unit test (#445, DRIFT-1; dataset/source scoping #486)."""

from __future__ import annotations

import json
from pathlib import Path

from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef
from kpubdata_builder.spec.serializer import write_buildspec_snapshot
from kpubdata_builder.stages.silver.drift import (
    NoSilverBaseline,
    SilverBaseline,
    detect_drift,
    find_previous_silver,
)
from kpubdata_builder.tabular import SchemaInfo, TableStatistics
from kpubdata_builder.tabular.types import ColumnInfo

_SCHEMA_A = SchemaInfo(
    columns=(
        ColumnInfo(name="a", dtype="Int64", nullable=False, unique_count=3),
        ColumnInfo(name="b", dtype="Utf8", nullable=True, unique_count=2),
    )
)
_STATS_A = TableStatistics(row_count=100, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)


class TestDetectDriftSchema:
    def test_column_added(self) -> None:
        schema_b = SchemaInfo(
            columns=_SCHEMA_A.columns
            + (ColumnInfo(name="c", dtype="Float64", nullable=True, unique_count=1),)
        )
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "column_added" and f.column == "c" for f in findings)

    def test_column_removed(self) -> None:
        schema_b = SchemaInfo(columns=(_SCHEMA_A.columns[0],))
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "column_removed" and f.column == "b" for f in findings)

    def test_dtype_changed(self) -> None:
        schema_b = SchemaInfo(
            columns=(
                ColumnInfo(name="a", dtype="Float64", nullable=False, unique_count=3),
                ColumnInfo(name="b", dtype="Utf8", nullable=True, unique_count=2),
            )
        )
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "dtype_changed" and f.column == "a" for f in findings)

    def test_no_schema_drift(self) -> None:
        findings = detect_drift(_SCHEMA_A, _STATS_A, _SCHEMA_A, _STATS_A)
        assert findings == []


class TestDetectDriftStats:
    def test_row_count_jump(self) -> None:
        stats_b = TableStatistics(row_count=500, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)
        findings = detect_drift(_SCHEMA_A, stats_b, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "row_count_jump" for f in findings)

    def test_small_row_count_change_no_drift(self) -> None:
        stats_b = TableStatistics(row_count=110, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)
        findings = detect_drift(_SCHEMA_A, stats_b, _SCHEMA_A, _STATS_A)
        assert findings == []

    def test_previous_zero_rows_no_jump(self) -> None:
        stats_prev = TableStatistics(row_count=0, null_counts={}, duplicate_rate=0.0)
        stats_curr = TableStatistics(row_count=100, null_counts={"a": 0}, duplicate_rate=0.0)
        findings = detect_drift(_SCHEMA_A, stats_curr, _SCHEMA_A, stats_prev)
        assert not any(f.kind == "row_count_jump" for f in findings)


def _write_run(
    output_root: Path,
    run_id: str,
    *,
    dataset_id: str,
    source_key: str = "datago.apt_trade",
    row_count: int = 10,
    finished_at: str = "2025-01-01T00:05:00+00:00",
    errors: tuple[str, ...] = (),
    owner_id: str | None = None,
) -> None:
    """Records buildspec.yaml snapshot + manifest.json + silver/{source_key}/schema+stats.json.

    Minimally reproduces the file layout that find_previous_silver reads.
    Fixture verifies scoping logic deterministically without running the actual pipeline.
    """
    spec = BuildSpec(
        dataset_id=dataset_id,
        title="Fixture",
        description="fixture",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )
    write_buildspec_snapshot(spec, output_root=output_root, run_id=run_id)
    run_dir = output_root / run_id
    manifest: dict[str, object] = {
        "started_at": "2025-01-01T00:00:00+00:00",
        "finished_at": finished_at,
        "errors": list(errors),
    }
    if owner_id is not None:
        manifest["owner_id"] = owner_id
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    silver_dir = run_dir / "silver" / source_key.replace("/", "_")
    silver_dir.mkdir(parents=True, exist_ok=True)
    schema_payload = {
        "columns": [{"name": "id", "dtype": "Int64", "nullable": False, "unique_count": row_count}]
    }
    stats_payload = {"row_count": row_count, "null_counts": {"id": 0}, "duplicate_rate": 0.0}
    (silver_dir / "schema.json").write_text(json.dumps(schema_payload), encoding="utf-8")
    (silver_dir / "stats.json").write_text(json.dumps(stats_payload), encoding="utf-8")


_APT_TRADE = "datago.apt_trade"


class TestFindPreviousSilverScoping:
    """Finds only the immediately preceding "success" run for the same dataset_id·source_key,
    not just any previous run (#486).
    """

    def test_returns_none_when_no_candidates(self, tmp_path: Path) -> None:
        outcome = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key="s")
        assert isinstance(outcome, NoSilverBaseline)
        assert outcome.reason

    def test_finds_matching_previous_run(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run0", dataset_id="d.a", row_count=5)

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert isinstance(found, SilverBaseline)
        stats = found.stats
        assert stats.row_count == 5

    def test_does_not_compare_across_datasets(self, tmp_path: Path) -> None:
        """In order dataset A run, dataset B run, dataset A new run, A new is not compared with
        B.
        """
        _write_run(tmp_path, "a-run1", dataset_id="dataset.a", row_count=10)
        _write_run(tmp_path, "b-run1", dataset_id="dataset.b", row_count=999)

        found = find_previous_silver(
            tmp_path, "a-run2", dataset_id="dataset.a", source_key=_APT_TRADE
        )

        assert isinstance(found, SilverBaseline)
        stats = found.stats
        assert stats.row_count == 10  # Previous run of dataset.a, not dataset.b(999).

    def test_does_not_compare_across_sources(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run0", dataset_id="d.a", source_key="datago.other", row_count=10)

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert isinstance(found, NoSilverBaseline)

    def test_excludes_failed_runs(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run0", dataset_id="d.a", row_count=10, errors=("boom",))

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert isinstance(found, NoSilverBaseline)

    def test_excludes_current_run(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run1", dataset_id="d.a", row_count=10)

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert isinstance(found, NoSilverBaseline)

    def test_picks_most_recent_by_finished_at(self, tmp_path: Path) -> None:
        _write_run(
            tmp_path,
            "run-old",
            dataset_id="d.a",
            row_count=1,
            finished_at="2025-01-01T00:00:00+00:00",
        )
        _write_run(
            tmp_path,
            "run-new",
            dataset_id="d.a",
            row_count=2,
            finished_at="2025-06-01T00:00:00+00:00",
        )

        found = find_previous_silver(
            tmp_path, "run-latest", dataset_id="d.a", source_key=_APT_TRADE
        )

        assert isinstance(found, SilverBaseline)
        stats = found.stats
        assert stats.row_count == 2

    def test_missing_snapshot_or_stats_are_skipped(self, tmp_path: Path) -> None:
        # Legacy run without snapshot.
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        (legacy_dir / "manifest.json").write_text(
            json.dumps({"finished_at": "2025-01-01T00:00:00+00:00", "errors": []}),
            encoding="utf-8",
        )

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert isinstance(found, NoSilverBaseline)


_ALICE = "oidc:issuer|alice"
_BOB = "oidc:issuer|bob"


class TestBaselineOwnerScoping:
    """A baseline never crosses owners (#700).

    Row count, schema and distribution changes are metadata about someone else's
    data, and drift is exactly what reports them. These are negative tests: a drift
    check reading the wrong baseline still produces numbers, so only asserting the
    absence catches it.
    """

    def test_another_owners_run_is_not_the_baseline(self, tmp_path: Path) -> None:
        """Alice's run must not become Bob's baseline."""
        _write_run(tmp_path, "alice-1", dataset_id="d.a", row_count=1000, owner_id=_ALICE)

        found = find_previous_silver(
            tmp_path, "bob-1", dataset_id="d.a", source_key=_APT_TRADE, owner_id=_BOB
        )

        assert isinstance(found, NoSilverBaseline)
        assert _ALICE not in found.detail

    def test_the_reason_does_not_reveal_that_other_owners_have_runs(self, tmp_path: Path) -> None:
        """N-04 (negative): "only other people's runs" reads exactly like "no runs".

        How many runs another owner has, or that they have any, is the metadata side
        channel the owner filter closes. Reason and text must match an empty
        workspace's, and carry no number.
        """
        crowded = tmp_path / "crowded"
        for index in range(3):
            _write_run(crowded, f"alice-{index}", dataset_id="d.a", row_count=1000, owner_id=_ALICE)
        empty = tmp_path / "empty"
        empty.mkdir()

        seen = find_previous_silver(
            crowded, "bob-1", dataset_id="d.a", source_key=_APT_TRADE, owner_id=_BOB
        )
        nothing = find_previous_silver(
            empty, "bob-1", dataset_id="d.a", source_key=_APT_TRADE, owner_id=_BOB
        )

        assert isinstance(seen, NoSilverBaseline) and isinstance(nothing, NoSilverBaseline)
        assert (seen.reason, seen.detail) == (nothing.reason, nothing.detail)
        assert "3" not in seen.detail and _ALICE not in seen.detail

    def test_the_same_owner_still_gets_a_baseline(self, tmp_path: Path) -> None:
        """Scoping must not break the case it is meant to preserve."""
        _write_run(tmp_path, "alice-1", dataset_id="d.a", row_count=7, owner_id=_ALICE)

        found = find_previous_silver(
            tmp_path, "alice-2", dataset_id="d.a", source_key=_APT_TRADE, owner_id=_ALICE
        )

        assert isinstance(found, SilverBaseline)
        assert found.stats.row_count == 7
        assert found.run_id == "alice-1"

    def test_a_run_without_an_owner_is_not_assumed_to_be_ours(self, tmp_path: Path) -> None:
        """A run that recorded no owner drops out rather than counting as a match."""
        _write_run(tmp_path, "legacy-1", dataset_id="d.a", row_count=5, owner_id=None)

        found = find_previous_silver(
            tmp_path, "alice-1", dataset_id="d.a", source_key=_APT_TRADE, owner_id=_ALICE
        )

        assert isinstance(found, NoSilverBaseline)
        assert found.reason == "owner_unknown"
        assert "recorded no owner" in found.detail

    def test_no_owner_argument_keeps_the_previous_behaviour(self, tmp_path: Path) -> None:
        """Single-user deployments are unaffected: without an owner, nothing filters."""
        _write_run(tmp_path, "run-0", dataset_id="d.a", row_count=5, owner_id=None)

        found = find_previous_silver(tmp_path, "run-1", dataset_id="d.a", source_key=_APT_TRADE)

        assert isinstance(found, SilverBaseline)
        assert found.stats.row_count == 5

    def test_somebody_elses_run_does_not_hide_our_own(self, tmp_path: Path) -> None:
        """A newer run by another owner must not displace ours."""
        _write_run(
            tmp_path,
            "alice-1",
            dataset_id="d.a",
            row_count=7,
            owner_id=_ALICE,
            finished_at="2025-01-01T00:00:00+00:00",
        )
        _write_run(
            tmp_path,
            "bob-1",
            dataset_id="d.a",
            row_count=9999,
            owner_id=_BOB,
            finished_at="2025-06-01T00:00:00+00:00",
        )

        found = find_previous_silver(
            tmp_path, "alice-2", dataset_id="d.a", source_key=_APT_TRADE, owner_id=_ALICE
        )

        assert isinstance(found, SilverBaseline)
        assert found.stats.row_count == 7


class TestAbsenceIsNotHealth:
    """ "No baseline" must not be reportable as "no drift" (#700)."""

    def test_the_outcome_is_never_none(self, tmp_path: Path) -> None:
        """There is no ``None`` for a caller to write ``if x is None: healthy`` against.

        That optional return is what let the orchestrator leave schema_drift empty,
        which manifest writing then dropped, making "no baseline" and "compared,
        nothing changed" the same absent key on the wire.
        """
        found = find_previous_silver(
            tmp_path / "missing", "run-1", dataset_id="d.a", source_key=_APT_TRADE
        )

        assert found is not None
        assert isinstance(found, NoSilverBaseline)
        assert not isinstance(found, SilverBaseline)

    def test_every_rejection_carries_a_reason(self, tmp_path: Path) -> None:
        """A report needs to say why, not just show nothing."""
        _write_run(tmp_path, "other", dataset_id="dataset.b", row_count=1)

        found = find_previous_silver(
            tmp_path, "run-1", dataset_id="dataset.a", source_key=_APT_TRADE
        )

        assert isinstance(found, NoSilverBaseline)
        assert found.reason
        assert found.detail
        assert "dataset.a" in found.detail

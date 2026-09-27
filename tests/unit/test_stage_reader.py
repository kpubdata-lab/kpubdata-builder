"""stages._stage_reader   (#488).

Bronze/Silver/Gold stage    summary ,
    .   "Bronze  → Silver  →
Gold "      partial
   .
"""

from __future__ import annotations

import json
from pathlib import Path

from kpubdata_builder.stages._stage_reader import (
    bronze_source_dir,
    compute_run_stage_summary,
    gold_source_dir,
    read_bronze_summary,
    read_gold_summary,
    read_silver_summary,
    sanitize_source_segment,
    silver_source_dir,
)

_SILVER_FILES = ("schema.json", "stats.json", "preview.json", "validation.json")


def _write_bronze_artifact(
    tmp_path: Path,
    run_id: str,
    source_key: str,
    artifact_id: str,
    *,
    fetched_at: str = "2025-01-01T00:00:00+00:00",
    record_count: int = 2,
    fetch_params: dict[str, object] | None = None,
) -> Path:
    d = bronze_source_dir(tmp_path, run_id, source_key) / artifact_id
    d.mkdir(parents=True)
    (d / "raw_records.jsonl").write_text('{"id": "1"}\n{"id": "2"}\n', encoding="utf-8")
    metadata = {
        "source_key": source_key,
        "fetch_params": fetch_params or {"page": 1},
        "fetched_at": fetched_at,
        "provenance": {
            "operation": "fetch",
            "source_key": source_key,
            "fetch_params": fetch_params or {"page": 1},
            "fetched_at": fetched_at,
        },
        "record_count": record_count,
        "artifact_paths": {"records": "raw_records.jsonl", "metadata": "metadata.json"},
    }
    (d / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    return d


def _write_silver(
    tmp_path: Path,
    run_id: str,
    source_key: str,
    *,
    row_count: int = 2,
    sample_rows: list[dict[str, object]] | None = None,
    omit: tuple[str, ...] = (),
    write_table: bool = True,
) -> Path:
    d = silver_source_dir(tmp_path, run_id, source_key)
    d.mkdir(parents=True)
    if write_table:
        (d / "table.parquet").write_bytes(b"not-a-real-parquet-file")
    files = {
        "schema.json": {
            "columns": [{"name": "id", "dtype": "String", "nullable": False, "unique_count": 2}]
        },
        "stats.json": {"row_count": row_count, "null_counts": {"id": 0}, "duplicate_rate": 0.0},
        "preview.json": {
            "rows": sample_rows if sample_rows is not None else [{"id": "1"}, {"id": "2"}],
            "total_rows": row_count,
        },
        "validation.json": {"ok": True, "problems": []},
    }
    for name, payload in files.items():
        if name in omit:
            continue
        (d / name).write_text(json.dumps(payload), encoding="utf-8")
    return d


def _write_gold(
    tmp_path: Path,
    run_id: str,
    source_key: str,
    *,
    row_count: int = 2,
    options: dict[str, object] | None = None,
    output_path: str = "out/data.jsonl",
    omit_package: bool = False,
    write_table: bool = True,
) -> Path:
    d = gold_source_dir(tmp_path, run_id, source_key)
    d.mkdir(parents=True)
    if write_table:
        (d / "table.parquet").write_bytes(b"not-a-real-parquet-file")
    if not omit_package:
        package = {
            "dataset_name": source_key,
            "source_silver": source_key,
            "row_count": row_count,
            "columns": ["id"],
            "metadata": {},
            "export_plan": {
                "targets": [{"kind": "jsonl", "output_path": output_path, "options": options or {}}]
            },
            "splits": {"train": 1, "test": 1},
        }
        (d / "package.json").write_text(json.dumps(package), encoding="utf-8")
    return d


class TestPathHelpers:
    def test_sanitize_source_segment_replaces_slash(self) -> None:
        assert sanitize_source_segment("provider/dataset") == "provider_dataset"

    def test_bronze_silver_dir_use_sanitized_segment(self, tmp_path: Path) -> None:
        b = bronze_source_dir(tmp_path, "run1", "a/b")
        s = silver_source_dir(tmp_path, "run1", "a/b")
        assert b.name == "a_b"
        assert s.name == "a_b"

    def test_gold_dir_uses_raw_source_key(self, tmp_path: Path) -> None:
        g = gold_source_dir(tmp_path, "run1", "air")
        assert g.name == "air"


class TestStageStatus:
    def test_full_pipeline_completed(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(tmp_path, run_id, source, "art1")
        _write_silver(tmp_path, run_id, source)
        _write_gold(tmp_path, run_id, source)

        results = compute_run_stage_summary(tmp_path, run_id, (source,), frozenset())
        assert len(results) == 1
        assert results[0].bronze == "completed"
        assert results[0].silver == "completed"
        assert results[0].gold == "completed"

    def test_bronze_only_then_silver_failed_gold_not_run(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(tmp_path, run_id, source, "art1")
        # silver   (  persist  raise  ).

        results = compute_run_stage_summary(tmp_path, run_id, (source,), frozenset({source}))
        (result,) = results
        assert result.bronze == "completed"
        assert result.silver == "failed"
        assert result.gold == "not_run"

    def test_silver_success_then_gold_failed(self, tmp_path: Path) -> None:
        """Bronze  → Silver  → Gold   (#488  )."""
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(tmp_path, run_id, source, "art1")
        _write_silver(tmp_path, run_id, source)
        # gold   — gold persist   .

        results = compute_run_stage_summary(tmp_path, run_id, (source,), frozenset({source}))
        (result,) = results
        assert result.bronze == "completed"
        assert result.silver == "completed"
        assert result.gold == "failed"

    def test_source_fetch_failure_all_not_run_or_failed(self, tmp_path: Path) -> None:
        """fetch   bronze ,  not_run."""
        run_id = "run1"
        source = "missing"
        #    .

        results = compute_run_stage_summary(tmp_path, run_id, (source,), frozenset({source}))
        (result,) = results
        assert result.bronze == "failed"
        assert result.silver == "not_run"
        assert result.gold == "not_run"

    def test_unknown_source_without_error_entry_is_not_run(self, tmp_path: Path) -> None:
        """failed_source_keys     not_run( )."""
        run_id = "run1"
        results = compute_run_stage_summary(tmp_path, run_id, ("ghost",), frozenset())
        (result,) = results
        assert result.bronze == "not_run"
        assert result.silver == "not_run"
        assert result.gold == "not_run"

    def test_partial_silver_sidecar_is_unavailable(self, tmp_path: Path) -> None:
        """sidecar  completed/failed  unavailable."""
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(tmp_path, run_id, source, "art1")
        _write_silver(tmp_path, run_id, source, omit=("stats.json",))

        results = compute_run_stage_summary(tmp_path, run_id, (source,), frozenset())
        (result,) = results
        assert result.silver == "unavailable"

    def test_invalid_utf8_silver_sidecar_is_unavailable(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(tmp_path, run_id, source, "art1")
        _write_silver(tmp_path, run_id, source)
        (silver_source_dir(tmp_path, run_id, source) / "stats.json").write_bytes(b"\xff\xfe")

        (result,) = compute_run_stage_summary(tmp_path, run_id, (source,), frozenset())
        assert result.silver == "unavailable"
        assert read_silver_summary(tmp_path, run_id, source, sample_limit=5) is None

    def test_multiple_bronze_artifacts_selects_latest_deterministically(
        self, tmp_path: Path
    ) -> None:
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(
            tmp_path, run_id, source, "art-old", fetched_at="2025-01-01T00:00:00+00:00"
        )
        _write_bronze_artifact(
            tmp_path, run_id, source, "art-new", fetched_at="2025-01-02T00:00:00+00:00"
        )

        summary = read_bronze_summary(tmp_path, run_id, source)
        assert summary is not None
        assert summary.fetched_at == "2025-01-02T00:00:00+00:00"

    def test_multiple_bronze_artifacts_tie_break_by_artifact_id(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        same_time = "2025-01-01T00:00:00+00:00"
        _write_bronze_artifact(
            tmp_path, run_id, source, "art-a", fetched_at=same_time, record_count=1
        )
        _write_bronze_artifact(
            tmp_path, run_id, source, "art-z", fetched_at=same_time, record_count=99
        )

        summary = read_bronze_summary(tmp_path, run_id, source)
        assert summary is not None
        #  fetched_at artifact_id : "art-z" > "art-a"
        assert summary.record_count == 99

    def test_unreadable_bronze_artifact_candidate_is_excluded(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        good = _write_bronze_artifact(tmp_path, run_id, source, "art-good")
        bad = bronze_source_dir(tmp_path, run_id, source) / "art-bad"
        bad.mkdir(parents=True)
        (bad / "metadata.json").write_text("not json", encoding="utf-8")
        (bad / "raw_records.jsonl").write_text("", encoding="utf-8")

        summary = read_bronze_summary(tmp_path, run_id, source)
        assert summary is not None
        assert summary.record_count == 2  # good artifact's value, not corrupted one
        del good  # keep reference for readability


class TestSecureReading:
    def test_bronze_summary_never_exposes_fetch_params_or_secrets(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        _write_bronze_artifact(
            tmp_path,
            run_id,
            source,
            "art1",
            fetch_params={"api_key": "SUPER-SECRET-VALUE"},
        )
        summary = read_bronze_summary(tmp_path, run_id, source)
        assert summary is not None
        assert not hasattr(summary, "fetch_params")
        assert not hasattr(summary, "provenance")
        assert "SUPER-SECRET-VALUE" not in repr(summary)

    def test_gold_summary_never_exposes_export_options_or_output_path(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        _write_gold(
            tmp_path,
            run_id,
            source,
            options={"kaggle_key": "SUPER-SECRET-VALUE"},
            output_path="/absolute/secret/path/out.jsonl",
        )
        summary = read_gold_summary(tmp_path, run_id, source)
        assert summary is not None
        assert not hasattr(summary, "options")
        assert not hasattr(summary, "output_path")
        assert "SUPER-SECRET-VALUE" not in repr(summary)
        assert "/absolute/secret/path" not in repr(summary)
        assert summary.export_kinds == ["jsonl"]

    def test_silver_sample_respects_limit_and_reports_total_available(self, tmp_path: Path) -> None:
        run_id = "run1"
        source = "air"
        rows = [{"id": str(i)} for i in range(5)]
        _write_silver(tmp_path, run_id, source, row_count=5, sample_rows=rows)

        capped = read_silver_summary(tmp_path, run_id, source, sample_limit=2)
        assert capped is not None
        assert capped.sample == rows[:2]
        assert capped.sample_total_available == 5

        generous = read_silver_summary(tmp_path, run_id, source, sample_limit=100)
        assert generous is not None
        # persist  5  limit=100 5  .
        assert generous.sample == rows
        assert len(generous.sample) == 5

    def test_missing_stage_returns_none_not_error(self, tmp_path: Path) -> None:
        assert read_bronze_summary(tmp_path, "nope", "air") is None
        assert read_silver_summary(tmp_path, "nope", "air", sample_limit=5) is None
        assert read_gold_summary(tmp_path, "nope", "air") is None

"""Cancelled run partial artifact preservation/cleanup hook unit tests (#549)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kpubdata_builder.retention import (
    find_cancelled_partial_runs,
    prune_cancelled_runs,
)


def _write_manifest(
    run_dir: Path,
    *,
    status: str,
    partial: bool,
    finished_at: str | None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {"build_id": run_dir.name}
    if finished_at is not None:
        body["finished_at"] = finished_at
    if status == "failed":
        body["errors"] = ["boom"]
    else:
        body["status"] = status
    body["partial"] = partial
    (run_dir / "manifest.json").write_text(json.dumps(body), encoding="utf-8")


def _iso(moment: datetime) -> str:
    return moment.isoformat()


class TestFindCancelledPartialRuns:
    def test_lists_only_cancelled_partial_runs(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        _write_manifest(
            tmp_path / "run-cancelled", status="cancelled", partial=True, finished_at=_iso(now)
        )
        _write_manifest(tmp_path / "run-ok", status="ok", partial=False, finished_at=_iso(now))
        _write_manifest(
            tmp_path / "run-failed", status="failed", partial=False, finished_at=_iso(now)
        )
        (tmp_path / "run-orphan").mkdir()

        candidates = find_cancelled_partial_runs(tmp_path)

        assert [c.run_id for c in candidates] == ["run-cancelled"]

    def test_missing_output_root_returns_empty(self, tmp_path: Path) -> None:
        assert find_cancelled_partial_runs(tmp_path / "nope") == []

    def test_unreadable_manifest_is_skipped(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run-broken"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{not json", encoding="utf-8")

        assert find_cancelled_partial_runs(tmp_path) == []


class TestPruneCancelledRuns:
    def test_ttl_none_deletes_nothing_even_with_apply(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        _write_manifest(
            tmp_path / "run-old",
            status="cancelled",
            partial=True,
            finished_at=_iso(now - timedelta(days=365)),
        )

        report = prune_cancelled_runs(tmp_path, ttl_hours=None, apply=True, now=now)

        assert report.deleted == ()
        assert report.scanned == 1
        assert (tmp_path / "run-old").exists()

    def test_dry_run_keeps_everything(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        _write_manifest(
            tmp_path / "run-old",
            status="cancelled",
            partial=True,
            finished_at=_iso(now - timedelta(days=30)),
        )

        report = prune_cancelled_runs(tmp_path, ttl_hours=24, apply=False, now=now)

        assert report.deleted == ()
        assert [c.run_id for c in report.kept] == ["run-old"]
        assert (tmp_path / "run-old").exists()

    def test_apply_deletes_only_expired_runs(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        _write_manifest(
            tmp_path / "run-expired",
            status="cancelled",
            partial=True,
            finished_at=_iso(now - timedelta(days=30)),
        )
        _write_manifest(
            tmp_path / "run-fresh",
            status="cancelled",
            partial=True,
            finished_at=_iso(now - timedelta(hours=1)),
        )

        report = prune_cancelled_runs(tmp_path, ttl_hours=24, apply=True, now=now)

        assert report.deleted == ("run-expired",)
        assert not (tmp_path / "run-expired").exists()
        assert (tmp_path / "run-fresh").exists()
        assert {c.run_id for c in report.kept} == {"run-fresh"}

    def test_run_without_finished_at_is_never_deleted(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        _write_manifest(
            tmp_path / "run-unknown-age",
            status="cancelled",
            partial=True,
            finished_at=None,
        )

        report = prune_cancelled_runs(tmp_path, ttl_hours=1, apply=True, now=now)

        assert report.deleted == ()
        assert (tmp_path / "run-unknown-age").exists()

    def test_invalid_run_dir_name_is_never_deleted(self, tmp_path: Path) -> None:
        now = datetime.now(timezone.utc)
        run_dir = tmp_path / "..%2fevil"
        _write_manifest(
            run_dir,
            status="cancelled",
            partial=True,
            finished_at=_iso(now - timedelta(days=30)),
        )

        report = prune_cancelled_runs(tmp_path, ttl_hours=24, apply=True, now=now)

        assert report.deleted == ()
        assert run_dir.exists()

"""SQLite build index test (#309, ADR 0003)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kpubdata_builder.store import SCHEMA_VERSION, SqliteBuildIndex, rebuild_index

from .conftest import requires_symlinks


class TestBuildIndex:
    """BuildIndex unit test."""

    def test_init_creates_database(self, tmp_path: Path) -> None:
        """Database and schema created on initialization."""
        index = SqliteBuildIndex(tmp_path)
        assert (tmp_path / "_builds.sqlite").exists()

        # Check schema version
        cur = index._conn.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == SCHEMA_VERSION

        # Check builds table
        cur = index._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='builds'"
        )
        assert cur.fetchone() is not None

    def test_insert_and_retrieve(self, tmp_path: Path) -> None:
        """Can retrieve entry after insertion."""
        index = SqliteBuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="test1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        entry = index.get("test1")
        assert entry is not None
        assert entry.run_id == "test1"
        assert entry.status == "ok"
        assert entry.started_at == "2025-01-01T10:00:00Z"
        assert entry.finished_at == "2025-01-01T10:05:00Z"

    def test_insert_or_replace_updates_existing(self, tmp_path: Path) -> None:
        """insert_or_replace updates existing entry."""
        index = SqliteBuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="test1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        # Reinsert after state change
        index.insert_or_replace(
            run_id="test1",
            status="failed",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            error="test error",
        )

        entry = index.get("test1")
        assert entry is not None
        assert entry.status == "failed"
        assert entry.error == "test error"

    def test_count_builds_counts_indexed_and_extra_runs_once(self, tmp_path: Path) -> None:
        """#948: the admin total — indexed runs plus ids only the job registry knows."""
        index = SqliteBuildIndex(tmp_path)
        for i in range(3):
            index.insert_or_replace(f"run-{i}", "ok", None, f"2025-01-01T0{i}:00:00Z")

        assert index.count_builds() == 3
        assert index.count_builds(also=["run-0", "run-2"]) == 3
        assert index.count_builds(also=["run-0", "live", "live"]) == 4
        # More ids than one IN (...) lookup takes.
        assert index.count_builds(also=[f"live-{i}" for i in range(1200)] + ["run-1"]) == 1203

    def test_list_builds_orders_by_finished_at_desc(self, tmp_path: Path) -> None:
        """list_builds returns in descending order by finished_at."""
        index = SqliteBuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
        )
        index.insert_or_replace(
            run_id="new",
            status="ok",
            started_at="2025-01-01T11:00:00Z",
            finished_at="2025-01-01T11:05:00Z",
        )
        index.insert_or_replace(
            run_id="mid",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        builds = index.list_builds()
        assert len(builds) == 3
        assert builds[0].run_id == "new"
        assert builds[1].run_id == "mid"
        assert builds[2].run_id == "old"

    def test_list_builds_respects_limit(self, tmp_path: Path) -> None:
        """list_builds respects limit parameter."""
        index = SqliteBuildIndex(tmp_path)

        for i in range(5):
            index.insert_or_replace(
                run_id=f"run{i}",
                status="ok",
                started_at="2025-01-01T10:00:00Z",
                finished_at=f"2025-01-01T1{i}:00:00Z",
            )

        builds = index.list_builds(limit=3)
        assert len(builds) == 3

    def test_delete_removes_entry(self, tmp_path: Path) -> None:
        """delete removes entry."""
        index = SqliteBuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="test1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        index.delete("test1")
        assert index.get("test1") is None

    def test_get_returns_none_for_missing(self, tmp_path: Path) -> None:
        """get returns None for non-existent entry."""
        index = SqliteBuildIndex(tmp_path)
        assert index.get("nonexistent") is None

    def test_schema_version_upgrade_recreates_table(self, tmp_path: Path) -> None:
        """Table is recreated if schema version changes."""
        # First index creation
        index1 = SqliteBuildIndex(tmp_path)
        index1.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )
        index1.close()

        # Simulate upgrade by manipulating schema version
        import sqlite3

        conn = sqlite3.connect(tmp_path / "_builds.sqlite")
        conn.execute("UPDATE schema_version SET version = 0")
        conn.commit()
        conn.close()

        # New index (schema recreated)
        index2 = SqliteBuildIndex(tmp_path)

        # Old data must be deleted
        assert index2.get("old") is None

        # Confirm new version
        cur = index2._conn.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == SCHEMA_VERSION


class TestBuildIndexDatasetId:
    """dataset_id derived column and query (#488)."""

    def test_insert_and_retrieve_dataset_id(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.sample",
        )
        entry = index.get("run1")
        assert entry is not None
        assert entry.dataset_id == "dataset.sample"

    def test_dataset_id_defaults_to_none(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="legacy",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )
        entry = index.get("legacy")
        assert entry is not None
        assert entry.dataset_id is None

    def test_list_by_dataset_filters_and_orders(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="a-old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
            dataset_id="dataset.a",
        )
        index.insert_or_replace(
            run_id="a-new",
            status="ok",
            started_at="2025-01-01T11:00:00Z",
            finished_at="2025-01-01T11:05:00Z",
            dataset_id="dataset.a",
        )
        index.insert_or_replace(
            run_id="b-only",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.b",
        )

        results = index.list_by_dataset("dataset.a")
        assert [r.run_id for r in results] == ["a-new", "a-old"]

        assert index.list_by_dataset("dataset.unknown") == []

    def test_unbounded_dataset_query_returns_more_than_500_rows(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        for number in range(505):
            index.insert_or_replace(
                run_id=f"run-{number:03d}",
                status="ok",
                started_at="2025-01-01T00:00:00Z",
                finished_at=f"2025-01-01T00:{number // 60:02d}:{number % 60:02d}Z",
                dataset_id="dataset.bulk",
            )

        assert len(index.list_by_dataset("dataset.bulk", limit=None)) == 505
        assert len(index.list_builds(limit=None)) == 505

    def test_list_by_dataset_respects_limit(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        for i in range(5):
            index.insert_or_replace(
                run_id=f"run{i}",
                status="ok",
                started_at="2025-01-01T10:00:00Z",
                finished_at=f"2025-01-01T1{i}:00:00Z",
                dataset_id="dataset.many",
            )
        assert len(index.list_by_dataset("dataset.many", limit=2)) == 2

    def test_list_builds_includes_dataset_id(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.sample",
        )
        builds = index.list_builds()
        assert builds[0].dataset_id == "dataset.sample"


class TestBuildIndexOwnerId:
    """owner_id derived column (#505). Source of truth is manifest.json —
    this column is derived search value."""

    def test_insert_and_retrieve_owner_id(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            created_by="oidc:userA",
            owner_id="oidc:deadbeef",
        )
        entry = index.get("run1")
        assert entry is not None
        assert entry.owner_id == "oidc:deadbeef"
        assert entry.created_by == "oidc:userA"

    def test_owner_id_defaults_to_none(self, tmp_path: Path) -> None:
        """Unspecified owner_id insertion (same form as earlier callers
        before #505) remains as None."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="legacy",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            created_by="oidc:legacyUser",
        )
        entry = index.get("legacy")
        assert entry is not None
        assert entry.owner_id is None
        assert entry.created_by == "oidc:legacyUser"

    def test_list_builds_and_list_by_dataset_include_owner_id(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.sample",
            owner_id="oidc:deadbeef",
        )
        assert index.list_builds()[0].owner_id == "oidc:deadbeef"
        assert index.list_by_dataset("dataset.sample")[0].owner_id == "oidc:deadbeef"


class TestListRecentOwned:
    """``list_recent_owned`` (#527) — ownership filter in SQL before LIMIT

    applies, so other principals' recent runs don't fill the LIMIT and cut off
    the requester's own recent run. Policy must match exactly with
    ``service.auth.principal_owns()`` (#505).
    """

    def test_owner_id_match_takes_priority(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="mine",
            status="ok",
            started_at=None,
            finished_at="2025-01-01T10:00:00Z",
            created_by="oidc:otherLabel",  # label differs but owner_id matches.
            owner_id="oidc:deadbeef",
        )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id="oidc:deadbeef", principal_label="oidc:userA"
        )
        assert [e.run_id for e in entries] == ["mine"]

    def test_legacy_created_by_fallback_when_record_owner_id_missing(self, tmp_path: Path) -> None:
        """Legacy run without owner_id in record falls back to created_by for matching."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="legacy-mine",
            status="ok",
            started_at=None,
            finished_at="2025-01-01T10:00:00Z",
            created_by="oidc:userA",
            owner_id=None,
        )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id="oidc:deadbeef", principal_label="oidc:userA"
        )
        assert [e.run_id for e in entries] == ["legacy-mine"]

    def test_principal_without_owner_id_always_falls_back_to_created_by(
        self, tmp_path: Path
    ) -> None:
        """If principal has no owner_id, compare by created_by regardless of record owner_id."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="has-owner-id",
            status="ok",
            started_at=None,
            finished_at="2025-01-01T10:00:00Z",
            created_by="oidc:userA",
            owner_id="oidc:deadbeef",  # record has owner_id but principal doesn't.
        )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id=None, principal_label="oidc:userA"
        )
        assert [e.run_id for e in entries] == ["has-owner-id"]

    def test_no_match_is_excluded_fail_closed(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="theirs",
            status="ok",
            started_at=None,
            finished_at="2025-01-01T10:00:00Z",
            created_by="oidc:userB",
            owner_id="oidc:otherhash",
        )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id="oidc:deadbeef", principal_label="oidc:userA"
        )
        assert entries == []

    def test_no_created_by_no_owner_id_is_excluded(self, tmp_path: Path) -> None:
        """Record without both created_by/owner_id matches no principal."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="anonymous",
            status="ok",
            started_at=None,
            finished_at="2025-01-01T10:00:00Z",
            created_by=None,
            owner_id=None,
        )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id="oidc:deadbeef", principal_label="oidc:userA"
        )
        assert entries == []

    def test_filter_applied_before_limit_so_own_older_run_is_not_crowded_out(
        self, tmp_path: Path
    ) -> None:
        """My run won't be cut off by LIMIT even if older than another user's top 10 runs (#527)."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="mine-old",
            status="ok",
            started_at=None,
            finished_at="2025-01-01T00:00:00Z",  # all older than another user's top 10 runs.
            created_by="oidc:userA",
            owner_id="oidc:deadbeef",
        )
        for i in range(10):
            index.insert_or_replace(
                run_id=f"theirs-{i:02d}",
                status="ok",
                started_at=None,
                finished_at=f"2025-01-02T{i:02d}:00:00Z",  # all newer than my run.
                created_by="oidc:userB",
                owner_id="oidc:otherhash",
            )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id="oidc:deadbeef", principal_label="oidc:userA"
        )
        assert [e.run_id for e in entries] == ["mine-old"]

    def test_limit_is_respected_after_filtering(self, tmp_path: Path) -> None:
        index = SqliteBuildIndex(tmp_path)
        for i in range(15):
            index.insert_or_replace(
                run_id=f"mine-{i:02d}",
                status="ok",
                started_at=None,
                finished_at=f"2025-01-01T{i:02d}:00:00Z",
                created_by="oidc:userA",
                owner_id="oidc:deadbeef",
            )
        entries = index.list_recent_owned(
            limit=10, principal_owner_id="oidc:deadbeef", principal_label="oidc:userA"
        )
        assert len(entries) == 10
        # descending by finished_at — most recent 10 (05~14 not 09~14).
        assert entries[0].run_id == "mine-14"
        assert entries[-1].run_id == "mine-05"


class TestRebuildIndex:
    """rebuild_index function test."""

    def test_rebuild_from_empty_directory(self, tmp_path: Path) -> None:
        """Empty index created from empty directory."""
        count = rebuild_index(tmp_path)
        assert count == 0

        index = SqliteBuildIndex(tmp_path)
        assert index.list_builds() == []

    def test_rebuild_scans_manifest_files(self, tmp_path: Path) -> None:
        """Rebuild index by scanning manifest.json in filesystem."""
        # Create fake manifest files
        (tmp_path / "run1").mkdir()
        (tmp_path / "run1" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run1",
                    "status": "ok",
                    "started_at": "2025-01-01T10:00:00Z",
                    "finished_at": "2025-01-01T10:05:00Z",
                }
            )
        )

        (tmp_path / "run2").mkdir()
        (tmp_path / "run2" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run2",
                    "status": "failed",
                    "errors": ["test error"],
                    "started_at": "2025-01-01T11:00:00Z",
                    "finished_at": "2025-01-01T11:05:00Z",
                }
            )
        )

        # Directory without manifest
        (tmp_path / "no-manifest").mkdir()

        count = rebuild_index(tmp_path)
        assert count == 2

        index = SqliteBuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 2

        # Confirm sorting by run_id (finished_at DESC)
        assert builds[0].run_id == "run2"
        assert builds[1].run_id == "run1"

    def test_rebuild_reads_owner_id_from_manifest(self, tmp_path: Path) -> None:
        """rebuild_index reindexes owner_id from manifest.json as-is (#505).

        Legacy manifests without the owner_id field must be rebuilt as None.
        """
        (tmp_path / "run1").mkdir()
        (tmp_path / "run1" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run1",
                    "status": "ok",
                    "started_at": "2025-01-01T10:00:00Z",
                    "finished_at": "2025-01-01T10:05:00Z",
                    "created_by": "oidc:userA",
                    "owner_id": "oidc:deadbeef",
                }
            )
        )
        (tmp_path / "run2-legacy").mkdir()
        (tmp_path / "run2-legacy" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run2-legacy",
                    "status": "ok",
                    "started_at": "2025-01-01T09:00:00Z",
                    "finished_at": "2025-01-01T09:05:00Z",
                    "created_by": "oidc:legacyUser",
                }
            )
        )

        count = rebuild_index(tmp_path)
        assert count == 2

        index = SqliteBuildIndex(tmp_path)
        assert index.get("run1").owner_id == "oidc:deadbeef"  # type: ignore[union-attr]
        assert index.get("run2-legacy").owner_id is None  # type: ignore[union-attr]

    def test_rebuild_indexes_digest_from_snapshot_bytes(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps({"started_at": "a", "finished_at": "b"}), encoding="utf-8"
        )
        payload = b"dataset_id: d\n"
        (run_dir / "buildspec.yaml").write_bytes(payload)

        assert rebuild_index(tmp_path) == 1

        entry = SqliteBuildIndex(tmp_path).get("run1")
        assert entry is not None
        assert entry.spec_digest == f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def test_rebuild_keeps_legacy_snapshot_digest_null(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        assert rebuild_index(tmp_path) == 1
        entry = SqliteBuildIndex(tmp_path).get("legacy")
        assert entry is not None
        assert entry.spec_digest is None

    def test_rebuild_restores_dataset_id_from_snapshot(self, tmp_path: Path) -> None:
        """rebuild_index safely restores dataset_id from buildspec.yaml (#488)."""
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps({"started_at": "a", "finished_at": "b"}), encoding="utf-8"
        )
        (run_dir / "buildspec.yaml").write_bytes(b"dataset_id: dataset.restored\ntitle: t\n")

        assert rebuild_index(tmp_path) == 1
        entry = SqliteBuildIndex(tmp_path).get("run1")
        assert entry is not None
        assert entry.dataset_id == "dataset.restored"

    def test_rebuild_leaves_dataset_id_null_for_legacy_run(self, tmp_path: Path) -> None:
        """dataset_id of legacy run without snapshot is not guessed (#488)."""
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        assert rebuild_index(tmp_path) == 1
        entry = SqliteBuildIndex(tmp_path).get("legacy")
        assert entry is not None
        assert entry.dataset_id is None

    def test_rebuild_leaves_dataset_id_null_for_corrupt_snapshot(self, tmp_path: Path) -> None:
        """Even with snapshot, if dataset_id cannot be read or parsed, remains as None (#488)."""
        run_dir = tmp_path / "corrupt"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        (run_dir / "buildspec.yaml").write_bytes(b"\xff\xfe\x00")

        assert rebuild_index(tmp_path) == 1
        entry = SqliteBuildIndex(tmp_path).get("corrupt")
        assert entry is not None
        assert entry.dataset_id is None

    @requires_symlinks
    def test_rebuild_skips_symlinked_snapshot(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps({"started_at": "a", "finished_at": "b"}), encoding="utf-8"
        )
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(b"dataset_id: evil\n")
        (run_dir / "buildspec.yaml").symlink_to(outside)

        assert rebuild_index(tmp_path) == 1

        entry = SqliteBuildIndex(tmp_path).get("run1")
        assert entry is not None
        assert entry.spec_digest is None
        assert entry.dataset_id is None

    def test_rebuild_isolates_empty_corrupt_and_unreadable_snapshots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payloads: dict[str, bytes | None] = {
            "normal": b"dataset_id: normal\n",
            "empty": b"",
            "invalid-utf8": b"\xff\xfe\x00",
            "unreadable": b"dataset_id: unreadable\n",
        }
        for run_id, payload in payloads.items():
            run_dir = tmp_path / run_id
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
            if payload is not None:
                (run_dir / "buildspec.yaml").write_bytes(payload)

        original_read_bytes = Path.read_bytes

        def read_bytes_or_fail(path: Path) -> bytes:
            if path == tmp_path / "unreadable" / "buildspec.yaml":
                raise PermissionError("snapshot unreadable")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes_or_fail)

        assert rebuild_index(tmp_path) == len(payloads)

        index = SqliteBuildIndex(tmp_path)
        normal = index.get("normal")
        empty = index.get("empty")
        invalid = index.get("invalid-utf8")
        unreadable = index.get("unreadable")
        assert normal is not None
        assert empty is not None
        assert invalid is not None
        assert unreadable is not None
        assert normal.spec_digest == f"sha256:{hashlib.sha256(payloads['normal']).hexdigest()}"
        assert empty.spec_digest == f"sha256:{hashlib.sha256(b'').hexdigest()}"
        assert invalid.spec_digest == (
            f"sha256:{hashlib.sha256(payloads['invalid-utf8']).hexdigest()}"
        )
        assert unreadable.spec_digest is None

    def test_rebuild_replaces_existing_index(self, tmp_path: Path) -> None:
        """rebuild deletes existing index and recreates it."""
        # First create index
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
        )
        index.close()

        # Create manifest files
        (tmp_path / "new").mkdir()
        (tmp_path / "new" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "new",
                    "status": "ok",
                    "started_at": "2025-01-01T11:00:00Z",
                    "finished_at": "2025-01-01T11:05:00Z",
                }
            )
        )

        count = rebuild_index(tmp_path)
        assert count == 1

        index = SqliteBuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 1
        assert builds[0].run_id == "new"
        assert index.get("old") is None

    def test_rebuild_skips_malformed_manifest(self, tmp_path: Path) -> None:
        """Corrupt manifest is skipped."""
        (tmp_path / "good").mkdir()
        (tmp_path / "good" / "manifest.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "started_at": "2025-01-01T10:00:00Z",
                    "finished_at": "2025-01-01T10:05:00Z",
                }
            )
        )

        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "manifest.json").write_text("invalid json")
        (tmp_path / "binary").mkdir()
        (tmp_path / "binary" / "manifest.json").write_bytes(b"\xff\xfe\x00binary")

        count = rebuild_index(tmp_path)
        assert count == 1

        index = SqliteBuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 1
        assert builds[0].run_id == "good"

    def test_rebuild_leaves_no_tmp_or_bak_after_success(self, tmp_path: Path) -> None:
        """After normal rebuild, no .tmp/.bak residual files remain (#366)."""
        index = SqliteBuildIndex(tmp_path)
        index.close()

        rebuild_index(tmp_path)

        assert (tmp_path / "_builds.sqlite").exists()
        assert not (tmp_path / "_builds.sqlite.tmp").exists()
        assert not (tmp_path / "_builds.sqlite.bak").exists()

    def test_rebuild_cleans_up_stale_tmp_file(self, tmp_path: Path) -> None:
        """Rebuild works normally even if .tmp files remain from interrupted prior run (#366)."""
        stale_tmp = tmp_path / "_builds.sqlite.tmp"
        stale_tmp.write_text("stale garbage")

        count = rebuild_index(tmp_path)

        assert count == 0
        assert not stale_tmp.exists()

    def test_rebuild_restores_backup_when_swap_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If .tmp -> original replace fails, restore existing index from backup (#366)."""
        index = SqliteBuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
        )
        index.close()

        index_path = tmp_path / "_builds.sqlite"
        tmp_index_path = tmp_path / "_builds.sqlite.tmp"
        original_rename = Path.rename

        def flaky_rename(self: Path, target: Path) -> Path:
            if self == tmp_index_path:
                raise OSError("simulated rename failure")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky_rename)

        with pytest.raises(OSError):
            rebuild_index(tmp_path)

        monkeypatch.undo()

        assert index_path.exists()
        assert not (tmp_path / "_builds.sqlite.bak").exists()

        restored = SqliteBuildIndex(tmp_path)
        assert restored.get("old") is not None

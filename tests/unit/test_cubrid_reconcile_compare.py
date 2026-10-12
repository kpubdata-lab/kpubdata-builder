"""The comparison behind the CUBRID reconciliation dry run (ADR 0022), without a database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpubdata_builder.store import cubrid_reconcile
from kpubdata_builder.store.artifacts.local import LocalArtifactStore
from kpubdata_builder.store.cubrid_reconcile import compare_manifests


def test_documents_are_compared_parsed_not_as_text(tmp_path: Path) -> None:
    manifest = {"build_id": "r1", "status": "ok", "nested": {"b": 1, "a": 2}}
    LocalArtifactStore(tmp_path).put_manifest("r1", manifest)
    # The row is compact and unsorted; the file is indented and sorted.
    diff = compare_manifests([("r1", json.dumps(manifest))], tmp_path)
    assert diff.matching == 1
    assert not diff.needs_restore


def test_each_kind_of_difference_is_reported_once(tmp_path: Path) -> None:
    local = LocalArtifactStore(tmp_path)
    local.put_manifest("differs", {"status": "running"})
    local.put_manifest("unreadable", {"status": "ok"})
    (tmp_path / "unreadable" / "manifest.json").write_bytes(b"\xff\xfe")
    local.put_manifest("file-only", {"status": "ok"})
    rows = [
        ("missing", json.dumps({"status": "ok"})),
        ("differs", json.dumps({"status": "ok"})),
        ("unreadable", json.dumps({"status": "ok"})),
        ("bad-row", "[1, 2]"),
        ("../escape", json.dumps({"status": "ok"})),
    ]

    diff = compare_manifests(rows, tmp_path, [("u2", "kosis"), ("u1", "datago")])

    assert diff.missing_on_disk == ["missing"]
    assert diff.differs == ["differs"]
    assert diff.unreadable_on_disk == ["unreadable"]
    assert diff.unreadable_in_db == ["bad-row"]
    assert diff.unsafe_run_ids == ["../escape"]
    assert diff.disk_only == 1
    assert diff.credentials == ["u1/datago", "u2/kosis"]
    assert diff.needs_restore


def test_a_row_the_file_already_falls_back_for_does_not_need_restoring(tmp_path: Path) -> None:
    LocalArtifactStore(tmp_path).put_manifest("r1", {"status": "ok"})
    diff = compare_manifests([("r1", "not json")], tmp_path)
    assert diff.unreadable_in_db == ["r1"]
    assert not diff.needs_restore


def test_main_exits_two_when_cubrid_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KPUBDATA_BUILDER_CUBRID_URL", raising=False)
    assert cubrid_reconcile.main(["--output-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "cannot read CUBRID" in err

"""CLI `build` command (#4): spec → run_build connection and exit code/output verification."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from kpubdata_builder import cli
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _write_spec(tmp_path: Path) -> Path:
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")
    return spec_path


def test_build_runs_pipeline_and_reports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    monkeypatch.setattr(cli, "_create_client", lambda: client)

    exit_code = cli.main(
        ["build", str(spec_path), "--output-dir", str(out_dir), "--run-id", "run1"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (out_dir / "run1" / "manifest.json").exists()
    manifest = json.loads((out_dir / "run1" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["build_id"] == "run1"
    assert "run1" in captured.out
    assert captured.err == ""


def test_build_reports_failure_with_exit_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec_path = _write_spec(tmp_path)
    out_dir = tmp_path / "out"
    # Client without source key → bronze fetch failure
    client = _FakeClient({"datago.other": [{"id": "1"}]})
    monkeypatch.setattr(cli, "_create_client", lambda: client)

    exit_code = cli.main(
        ["build", str(spec_path), "--output-dir", str(out_dir), "--run-id", "run1"]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err
    # manifest remains even on failure
    assert (out_dir / "run1" / "manifest.json").exists()


def test_build_requires_spec_argument(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = cli.main(["build"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err


def test_build_reports_spec_load_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.yaml"

    exit_code = cli.main(["build", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err


class TestTheCliCanMaterialise:
    """``--warehouse`` exposes the materialise-only end state (#703).

    Without a way to ask for it from the command line, the end state exists only for
    callers of the Python API.
    """

    def test_the_flag_commits_a_snapshot_and_says_so(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kpubdata_builder.warehouse import TableCatalog

        spec_path = _write_spec(tmp_path)
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        monkeypatch.setattr(cli, "_create_client", lambda: client)
        warehouse = tmp_path / "warehouse"

        exit_code = cli.main(
            [
                "build",
                str(spec_path),
                "--output-dir",
                str(tmp_path / "out"),
                "--run-id",
                "run-cli",
                "--warehouse",
                str(warehouse),
            ]
        )
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "snapshot snap_" in captured.out
        assert "revision 1" in captured.out

        tables = TableCatalog(warehouse).list_tables()
        assert len(tables) == 1
        assert tables[0].current_snapshot_id is not None

    def test_without_the_flag_nothing_is_committed_or_claimed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Silence rather than a success line: the two outcomes must not read alike."""
        spec_path = _write_spec(tmp_path)
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        monkeypatch.setattr(cli, "_create_client", lambda: client)

        exit_code = cli.main(
            ["build", str(spec_path), "--output-dir", str(tmp_path / "out"), "--run-id", "r"]
        )
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "snapshot" not in captured.out
        assert "no table committed" not in captured.out


class TestTheWarehouseIsReclaimed:
    """Committing without reclaiming grows the warehouse for ever (#738).

    `#733` built the reclamation and `#737` made builds commit. Nothing called the
    reclamation, so every refresh left a whole extra copy of Gold on disk — 365 of
    them in a year for a dataset refreshed daily. These tests are what that absence
    would fail.
    """

    def _build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        run_id: str,
        warehouse: Path,
        extra: list[str] | None = None,
    ) -> int:
        spec_path = _write_spec(tmp_path)
        client = _FakeClient({"datago.air_quality": [{"id": run_id, "v": 10}]})
        monkeypatch.setattr(cli, "_create_client", lambda: client)
        return cli.main(
            [
                "build",
                str(spec_path),
                "--output-dir",
                str(tmp_path / "out"),
                "--run-id",
                run_id,
                "--warehouse",
                str(warehouse),
                *(extra or []),
            ]
        )

    def test_a_refresh_reclaims_the_snapshot_it_superseded(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kpubdata_builder.warehouse import TableCatalog
        from kpubdata_builder.warehouse.layout import SnapshotLayout

        warehouse = tmp_path / "warehouse"
        assert self._build(tmp_path, monkeypatch, run_id="r1", warehouse=warehouse) == 0
        assert (
            self._build(
                tmp_path,
                monkeypatch,
                run_id="r2",
                warehouse=warehouse,
                extra=["--warehouse-keep", "1"],
            )
            == 0
        )
        _ = capsys.readouterr()

        catalog = TableCatalog(warehouse)
        table = catalog.list_tables()[0]
        snapshots = catalog.list_snapshots(table.id)

        assert len(snapshots) == 1
        assert snapshots[0].id == table.current_snapshot_id
        # The row going is not the point — the bytes are. A forgotten row with its
        # directory still on disk is exactly the leak this closes.
        layout = SnapshotLayout(warehouse, table.id)
        assert sorted(p.name for p in layout.snapshots_root.iterdir()) == [
            table.current_snapshot_id
        ]

    def test_keeping_every_snapshot_is_still_possible(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A negative keep turns reclamation off, for someone who wants the history."""
        from kpubdata_builder.warehouse import TableCatalog

        warehouse = tmp_path / "warehouse"
        assert self._build(tmp_path, monkeypatch, run_id="r1", warehouse=warehouse) == 0
        assert (
            self._build(
                tmp_path,
                monkeypatch,
                run_id="r2",
                warehouse=warehouse,
                extra=["--warehouse-keep", "-1"],
            )
            == 0
        )
        _ = capsys.readouterr()

        catalog = TableCatalog(warehouse)
        table = catalog.list_tables()[0]

        assert len(catalog.list_snapshots(table.id)) == 2

    def test_reclamation_failing_does_not_fail_a_committed_build(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The table exists either way; only the disk usage differs.

        A build that got as far as committing has already produced the dataset the
        user asked for. Failing it afterwards over housekeeping would throw away the
        thing that worked.
        """
        from kpubdata_builder.pipeline import orchestrator
        from kpubdata_builder.warehouse import TableCatalog

        warehouse = tmp_path / "warehouse"

        def _explode(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk went away")

        monkeypatch.setattr(orchestrator.warehouse_gc, "collect", _explode)

        exit_code = self._build(tmp_path, monkeypatch, run_id="r1", warehouse=warehouse)
        _ = capsys.readouterr()

        assert exit_code == 0
        assert TableCatalog(warehouse).list_tables()[0].current_snapshot_id is not None


class TestTheWarehouseGcCommand:
    """The age-based sweep a build cannot do from inside itself (#738).

    A crashed build leaves a `staging` row the catalog knows about, so
    `collect_orphan_staging` deliberately skips it and nothing else ever looks. The
    cut-off is a judgement about how long a build may take, which only a caller
    outside the build can make.
    """

    def test_it_reclaims_a_crashed_builds_staging_snapshot(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from kpubdata_builder.warehouse import TableCatalog
        from kpubdata_builder.warehouse.layout import SnapshotLayout

        warehouse = tmp_path / "warehouse"
        catalog = TableCatalog(warehouse)
        table = catalog.create_table("ws_personal", "dataset.sample.datago")
        snapshot = catalog.begin_snapshot(
            table.id, run_id="crashed", schema_version=1, coverage_hash="", artifact_digest=""
        )
        layout = SnapshotLayout(warehouse, table.id)
        staging = layout.begin(snapshot.id)
        _ = (staging / "data.jsonl").write_text("{}\n", encoding="utf-8")
        catalog.close()

        exit_code = cli.main(["warehouse-gc", str(warehouse), "--stale-hours", "0"])
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "marked 1 stale" in captured.out
        assert "reclaimed 1 directory" in captured.out
        assert TableCatalog(warehouse).list_snapshots(table.id) == []
        # One pass, not two. The bytes are what the disk cares about.
        assert not layout.staging_dir(snapshot.id).exists()

    def test_a_build_still_running_is_left_alone(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The catalog cannot tell a crashed build from a slow one, so the cut-off has
        to be believed. Reclaiming a staging directory out from under a live build is
        worse than keeping it a day too long."""
        from kpubdata_builder.warehouse import TableCatalog

        warehouse = tmp_path / "warehouse"
        catalog = TableCatalog(warehouse)
        table = catalog.create_table("ws_personal", "dataset.sample.datago")
        snapshot = catalog.begin_snapshot(
            table.id, run_id="running", schema_version=1, coverage_hash="", artifact_digest=""
        )
        catalog.close()

        exit_code = cli.main(["warehouse-gc", str(warehouse), "--stale-hours", "24"])
        captured = capsys.readouterr()

        assert exit_code == 0
        assert "marked" not in captured.out
        assert [s.id for s in TableCatalog(warehouse).list_snapshots(table.id)] == [snapshot.id]

    def test_a_missing_warehouse_is_an_error_not_an_empty_success(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Creating the directory would make a typo look like a clean warehouse."""
        exit_code = cli.main(["warehouse-gc", str(tmp_path / "nope")])
        captured = capsys.readouterr()

        assert exit_code == 1
        assert "no such warehouse directory" in captured.err
        assert not (tmp_path / "nope").exists()

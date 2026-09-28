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

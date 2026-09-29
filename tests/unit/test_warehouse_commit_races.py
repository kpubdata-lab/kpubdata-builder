"""A slow old build cannot replace a newer snapshot, and a failed commit is recorded (#787, #788).

``materialize()`` read the table revision just before committing, so the
compare-and-swap never saw a build that finished in the meantime: a build that started
earlier and ended later replaced the newer snapshot. And the commit ran after the
manifest said ``ok``, outside any handling, so a conflict ended the run with an
exception, no index row and a 500.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from pathlib import Path

import pytest

import kpubdata_builder.pipeline.orchestrator as orchestrator
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.warehouse import SnapshotStateError, TableCatalog

_SOURCE_KEY = "datago.air_quality"
_TABLE = f"race.table.{_SOURCE_KEY}"


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items: Iterable[dict[str, JsonValue]] = items


class _Dataset:
    def __init__(self, value: str, gate: threading.Event | None, started: threading.Event):
        self._value = value
        self._gate = gate
        self._started = started

    def list(self, **_params: object) -> _Result:
        self._started.set()
        if self._gate is not None:
            assert self._gate.wait(timeout=10)
        return _Result([{"id": "1", "value": self._value}])


class _Client:
    def __init__(self, value: str, gate: threading.Event | None = None) -> None:
        self.value = value
        self.gate = gate
        self.started = threading.Event()

    def dataset(self, _key: str) -> _Dataset:
        return _Dataset(self.value, self.gate, self.started)


def _spec() -> BuildSpec:
    return BuildSpec(
        dataset_id="race.table",
        title="Race",
        description="d",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def _current_run(catalog: TableCatalog) -> str:
    (table,) = [t for t in catalog.list_tables() if t.logical_name == _TABLE]
    assert table.current_snapshot_id is not None
    return catalog.get_snapshot(table.current_snapshot_id).run_id


def test_the_first_build_of_a_table_still_commits(tmp_path: Path) -> None:
    catalog = TableCatalog(tmp_path / "wh")

    result = run_build(
        _spec(),
        client=_Client("only"),
        output_root=tmp_path / "runs",
        run_id="only",
        catalog=catalog,
    )

    assert result.warehouse_failures == {}
    assert _current_run(catalog) == "only"


def test_a_build_that_started_earlier_does_not_replace_a_newer_commit(tmp_path: Path) -> None:
    """Acceptance (#787): B starts later and commits first; old A then conflicts."""
    catalog = TableCatalog(tmp_path / "wh")
    runs = tmp_path / "runs"
    gate = threading.Event()
    old = _Client("old", gate)
    results: dict[str, object] = {}

    thread = threading.Thread(
        target=lambda: results.setdefault(
            "a",
            run_build(_spec(), client=old, output_root=runs, run_id="a", catalog=catalog),
        )
    )
    thread.start()
    assert old.started.wait(timeout=10)  # A read its revision and is fetching

    newer = run_build(_spec(), client=_Client("new"), output_root=runs, run_id="b", catalog=catalog)
    gate.set()
    thread.join(timeout=10)

    stale = results["a"]
    assert newer.warehouse_failures == {}
    assert stale.warehouse_failures[_SOURCE_KEY]["reason"] == "conflict"  # type: ignore[attr-defined]
    assert _current_run(catalog) == "b"
    manifest = json.loads((runs / "a" / "manifest.json").read_text("utf-8"))
    assert manifest["warehouse_failures"][_SOURCE_KEY]["reason"] == "conflict"


def test_a_failed_commit_is_recorded_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance (#788): manifest records it, the index has the run, the API is not 500."""

    def failing_materialize(*_args: object, **_kwargs: object) -> object:
        raise SnapshotStateError("the promoted files do not match their digest")

    monkeypatch.setattr(orchestrator, "materialize", failing_materialize)
    runs = tmp_path / "runs"
    runs.mkdir()
    service = BuilderService(
        output_root=runs, client_factory=lambda **_: _Client("x"), warehouse_root=tmp_path / "wh"
    )
    spec_yaml = """\
dataset_id: race.table
title: Race
description: d
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: data.jsonl
"""

    response = service.build(spec_yaml, run_id="broken")

    assert response.status_code == 409
    assert response.body["warehouse_failures"] == {
        _SOURCE_KEY: {
            "reason": "commit_failed",
            "detail": "the snapshot could not be committed (SnapshotStateError)",
        }
    }
    manifest = json.loads((runs / "broken" / "manifest.json").read_text("utf-8"))
    assert manifest["status"] == "ok"
    assert manifest["warehouse_failures"][_SOURCE_KEY]["reason"] == "commit_failed"
    listed = service.list_builds(limit=10)
    assert "broken" in json.dumps(listed.body)


def test_a_manifest_without_failures_keeps_its_shape(tmp_path: Path) -> None:
    run_build(
        _spec(),
        client=_Client("x"),
        output_root=tmp_path / "runs",
        run_id="plain",
        catalog=TableCatalog(tmp_path / "wh"),
    )

    manifest = json.loads((tmp_path / "runs" / "plain" / "manifest.json").read_text("utf-8"))
    assert "warehouse_failures" not in manifest

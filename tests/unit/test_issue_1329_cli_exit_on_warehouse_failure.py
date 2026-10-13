"""``build --warehouse`` exits nonzero when a table was not committed (#1329).

The command printed ``no table committed`` and returned 0 whenever every source
succeeded, while the manifest recorded ``warehouse_failures``, every reading of the run
said ``failed`` (``run_status_from_manifest``) and ``POST /build`` answered 409. A
script reading the exit status took a refused commit for a refreshed table.

Each refusal below is arranged once and run through both the command and the service,
so the two cannot drift apart on what counts as a failed run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

import kpubdata_builder.pipeline.orchestrator as orchestrator
from kpubdata_builder import cli
from kpubdata_builder.manifest.status import (
    run_failure_cause,
    run_status_from_manifest,
    status_from_manifest,
)
from kpubdata_builder.service import BuilderService
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.warehouse import SnapshotConflict, SnapshotStateError, TableCatalog

_SOURCE_KEY = "m"
_SPEC = (
    "dataset_id: air\ntitle: Air\ndescription: d\nsources:\n"
    "  - provider: datago\n    dataset: air_station\n    alias: m\n"
    "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n"
)
_ROWS: list[dict[str, JsonValue]] = [{"stationName": "A", "pm10": 30 + i} for i in range(22)]


class _Result:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self.items = items


class _Source:
    """One provider dataset whose answer a refusal changes between builds."""

    def __init__(self) -> None:
        self.items: list[dict[str, JsonValue]] = list(_ROWS)

    def list(self, **_params: object) -> _Result:
        return _Result(list(self.items))

    def dataset(self, _key: str) -> _Source:
        return self


def _raising(error: Exception) -> Callable[..., object]:
    def materialize(*_args: object, **_kwargs: object) -> object:
        raise error

    return materialize


def _no_rows(source: _Source, _monkeypatch: pytest.MonkeyPatch) -> None:
    source.items = []


def _lost_the_race(_source: _Source, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator, "materialize", _raising(SnapshotConflict("revision 1 is not current"))
    )


def _broken_commit(_source: _Source, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        orchestrator,
        "materialize",
        _raising(SnapshotStateError("the promoted files do not match their digest")),
    )


@dataclass(frozen=True)
class _Refusal:
    """A table commit Builder refuses, arranged after a first build committed the table."""

    reason: str
    arrange: Callable[[_Source, pytest.MonkeyPatch], None]


#: The refusal fixtures the command and the service are both run against.
_REFUSALS = (
    _Refusal("conflict", _lost_the_race),
    _Refusal("empty_result", _no_rows),
    _Refusal("commit_failed", _broken_commit),
)
_refusals = pytest.mark.parametrize("refusal", _REFUSALS, ids=[r.reason for r in _REFUSALS])


def _cli_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: _Source,
    run_id: str,
    *,
    warehouse: bool = True,
) -> int:
    tmp_path.mkdir(parents=True, exist_ok=True)
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(_SPEC, encoding="utf-8")
    monkeypatch.setattr(cli, "_create_client", lambda: source)
    argv = ["build", str(spec_path), "--output-dir", str(tmp_path / "out"), "--run-id", run_id]
    if warehouse:
        argv += ["--warehouse", str(tmp_path / "wh")]
    return cli.main(argv)


def _manifest(root: Path, run_id: str) -> dict[str, object]:
    return cast(dict[str, object], json.loads((root / run_id / "manifest.json").read_text("utf-8")))


@_refusals
def test_a_refused_commit_exits_nonzero_and_says_why(
    refusal: _Refusal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _Source()
    assert _cli_build(tmp_path, monkeypatch, source, "first") == 0
    _ = capsys.readouterr()
    refusal.arrange(source, monkeypatch)

    exit_code = _cli_build(tmp_path, monkeypatch, source, "second")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no table committed" in captured.out
    lines = captured.err.splitlines()
    assert lines[0] == "error: build did not commit its table for one or more sources"
    assert lines[1].startswith(f"  - {_SOURCE_KEY}: {refusal.reason}: ")
    assert len(lines) == 2
    # The table the first build committed is the one still current.
    (table,) = TableCatalog(tmp_path / "wh").list_tables()
    assert table.revision == 1


@_refusals
def test_the_exit_status_agrees_with_the_manifest(
    refusal: _Refusal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _Source()
    committed_exit = _cli_build(tmp_path, monkeypatch, source, "first")
    refusal.arrange(source, monkeypatch)
    refused_exit = _cli_build(tmp_path, monkeypatch, source, "second")
    captured = capsys.readouterr()

    committed = _manifest(tmp_path / "out", "first")
    refused = _manifest(tmp_path / "out", "second")

    assert (committed_exit, run_status_from_manifest(committed)) == (0, "ok")
    assert "warehouse_failures" not in committed
    assert (refused_exit, run_status_from_manifest(refused)) == (1, "failed")
    # The artifacts are whole; it is the run that failed.
    assert status_from_manifest(refused) == "ok"
    recorded = cast(dict[str, dict[str, str]], refused["warehouse_failures"])[_SOURCE_KEY]
    assert recorded["reason"] == refusal.reason
    assert run_failure_cause(refused) == (refusal.reason, "warehouse")
    assert f"  - {_SOURCE_KEY}: {recorded['reason']}: {recorded['detail']}" in captured.err


@_refusals
def test_the_command_and_the_service_fail_the_same_refusal(
    refusal: _Refusal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_source = _Source()
    assert _cli_build(tmp_path / "cli", monkeypatch, cli_source, "first") == 0
    service_source = _Source()
    (tmp_path / "svc" / "out").mkdir(parents=True)
    service = BuilderService(
        output_root=tmp_path / "svc" / "out",
        client_factory=lambda **_: service_source,
        warehouse_root=tmp_path / "svc" / "wh",
    )
    try:
        assert service.build(_SPEC, run_id="first").status_code == 200
        _ = capsys.readouterr()

        refusal.arrange(cli_source, monkeypatch)
        refusal.arrange(service_source, monkeypatch)
        exit_code = _cli_build(tmp_path / "cli", monkeypatch, cli_source, "second")
        response = service.build(_SPEC, run_id="second")
    finally:
        service._async_builds.shutdown()
    captured = capsys.readouterr()

    body = cast(dict[str, Any], response.body)
    assert response.status_code == 409, body
    assert exit_code == 1
    # Both say the build made its artifacts, and name the same refusal for the source.
    assert body["status"] == "ok"
    served = body["warehouse_failures"][_SOURCE_KEY]
    assert served["reason"] == refusal.reason
    assert f"  - {_SOURCE_KEY}: {served['reason']}: {served['detail']}" in captured.err
    from_cli = _manifest(tmp_path / "cli" / "out", "second")
    from_service = _manifest(tmp_path / "svc" / "out", "second")
    assert from_cli["warehouse_failures"] == from_service["warehouse_failures"]
    assert run_status_from_manifest(from_cli) == run_status_from_manifest(from_service)


def test_the_diagnostic_holds_nothing_of_the_error_that_refused_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exception's text can name a path or a value; only its type is reported."""
    source = _Source()
    monkeypatch.setattr(
        orchestrator, "materialize", _raising(OSError("/secret/place: token=hunter2"))
    )

    exit_code = _cli_build(tmp_path, monkeypatch, source, "run")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.splitlines() == [
        "error: build did not commit its table for one or more sources",
        f"  - {_SOURCE_KEY}: commit_failed: the snapshot could not be committed (OSError)",
    ]
    assert "hunter2" not in captured.out + captured.err
    assert "/secret/place" not in captured.out + captured.err


@_refusals
def test_a_build_without_a_warehouse_still_succeeds(
    refusal: _Refusal,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing asked for a commit, so nothing that refuses one can fail the build."""
    source = _Source()
    assert _cli_build(tmp_path, monkeypatch, source, "first", warehouse=False) == 0
    refusal.arrange(source, monkeypatch)

    exit_code = _cli_build(tmp_path, monkeypatch, source, "second", warehouse=False)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "no table committed" not in captured.out
    manifest = _manifest(tmp_path / "out", "second")
    assert "warehouse_failures" not in manifest
    assert run_status_from_manifest(manifest) == "ok"


def test_a_committed_build_still_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _Source()
    assert _cli_build(tmp_path, monkeypatch, source, "first") == 0

    exit_code = _cli_build(tmp_path, monkeypatch, source, "refresh")
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.err == ""
    assert "revision 2" in captured.out


def test_a_failed_source_is_reported_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source that fails commits nothing; that is a build failure, not a refusal."""

    class _Unreachable(_Source):
        def list(self, **_params: object) -> _Result:
            raise RuntimeError("provider unreachable")

    exit_code = _cli_build(tmp_path, monkeypatch, _Unreachable(), "run")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.splitlines()[0] == "error: build failed for one or more sources"
    assert "did not commit" not in captured.err

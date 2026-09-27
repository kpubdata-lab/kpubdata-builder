"""Subcommand-level CLI smoke test (#595).

``tests/unit/test_cli.py`` covers parser/validate/publish; this covers
remaining subcommands (build/preview/serve/rebuild-index/prune-cancelled)
**argument parsing → exit code → output contract**. Network-dependent points
(run_build/preview_build/serve) are stubbed at boundaries, so actual fetch
does not occur here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import kpubdata_builder.cli as cli_module
from kpubdata_builder.cli import build_parser, dispatch, main

_VALID_SPEC = """\
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
"""

# Spec that loads but fails validate_spec — not SpecLoadError but
# ValidationError path (problems printed one per line) takes this path.
_UNVALIDATABLE_SPEC = _VALID_SPEC.replace("kind: jsonl", "kind: not-a-real-exporter")

_SUBCOMMANDS = (
    "validate",
    "preview",
    "build",
    "publish",
    "serve",
    "rebuild-index",
    "prune-cancelled",
)


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "spec.yaml"
    _ = path.write_text(_VALID_SPEC, encoding="utf-8")
    return path


@pytest.fixture
def unvalidatable_spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "bad-spec.yaml"
    _ = path.write_text(_UNVALIDATABLE_SPEC, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent CI runner/dev shell environment vars from changing default asserts."""
    monkeypatch.delenv("KPUBDATA_BUILDER_MAX_WORKERS", raising=False)
    monkeypatch.delenv("KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS", raising=False)


@pytest.fixture(autouse=True)
def _no_real_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block any test from creating real kpubdata Client."""

    def _forbidden(**_: object) -> object:
        raise AssertionError("CLI smoke test must not construct a real client")

    monkeypatch.setattr(cli_module, "_create_client", _forbidden)


# --- Parser contract -------------------------------------------------------------


@pytest.mark.parametrize("command", _SUBCOMMANDS)
def test_subcommand_help_exits_zero(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    """All subcommands --help exit 0 and print their name."""
    assert main([command, "--help"]) == 0
    assert command in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["validate"],
        ["preview"],
        ["build"],
        ["publish", "spec.yaml"],
        ["publish", "spec.yaml", "--destination", "d"],
        ["publish", "spec.yaml", "--artifacts-dir", "a"],
    ],
    ids=[
        "validate-no-spec",
        "preview-no-spec",
        "build-no-spec",
        "publish-no-required-flags",
        "publish-no-artifacts-dir",
        "publish-no-destination",
    ],
)
def test_missing_required_argument_exits_two(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Missing required arg is argparse convention exit 2 + usage."""
    assert main(argv) == 2
    err = capsys.readouterr().err
    assert "usage:" in err


@pytest.mark.parametrize(
    ("argv", "option"),
    [
        (["preview", "s.yaml", "--limit", "many"], "--limit"),
        (["serve", "--port", "http"], "--port"),
        (["serve", "--max-workers", "lots"], "--max-workers"),
        (["prune-cancelled", "--ttl-hours", "soon"], "--ttl-hours"),
    ],
)
def test_non_numeric_option_exits_two(
    argv: list[str], option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(argv) == 2
    assert option in capsys.readouterr().err


def test_parser_defaults_match_documented_values() -> None:
    """Parser must have defaults expected by docs and Dockerfile."""
    parser = build_parser()
    serve_args = parser.parse_args(["serve"])
    assert (serve_args.host, serve_args.port, serve_args.output_dir) == (
        "127.0.0.1",
        8000,
        "build",
    )
    assert serve_args.max_workers is None
    prune_args = parser.parse_args(["prune-cancelled"])
    assert prune_args.ttl_hours is None
    assert prune_args.apply is False


def test_dispatch_returns_two_for_unknown_command() -> None:
    """Programmatic calls bypassing argparse also don't silently succeed."""
    assert dispatch(SimpleNamespace(command="nope")) == 2  # type: ignore[arg-type]


def test_main_maps_string_systemexit_to_two(monkeypatch: pytest.MonkeyPatch) -> None:
    """If argparse exits with string code, normalize to 2."""

    def fake_parse_args(_self: object, _argv: object = None) -> object:
        raise SystemExit("boom")

    monkeypatch.setattr(cli_module.argparse.ArgumentParser, "parse_args", fake_parse_args)
    assert main(["validate", "x.yaml"]) == 2


def test_main_maps_none_systemexit_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_parse_args(_self: object, _argv: object = None) -> object:
        raise SystemExit(None)

    monkeypatch.setattr(cli_module.argparse.ArgumentParser, "parse_args", fake_parse_args)
    assert main(["validate", "x.yaml"]) == 0


# --- validate --------------------------------------------------------------


def test_validate_reports_each_validation_problem(
    unvalidatable_spec_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Spec that loads but fails validation prints problem one per line."""
    assert main(["validate", str(unvalidatable_spec_file)]) == 1
    err = capsys.readouterr().err
    assert "spec validation failed" in err
    assert "not-a-real-exporter" in err
    assert err.count("  - ") >= 1


# --- build -----------------------------------------------------------------


def _build_result(*, status: str, error: str | None = None) -> Any:
    outcome = SimpleNamespace(
        source_key="datago:air_quality",
        status="ok" if status == "ok" else "failed",
        stages_completed=["bronze", "silver", "gold"],
        error=error,
    )
    return SimpleNamespace(
        status=status,
        context=SimpleNamespace(run_id="run-1"),
        outcomes=[outcome],
        manifest_path=Path("build/run-1/manifest.json"),
    )


def test_build_prints_run_summary_and_exits_zero(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: object())

    def fake_run_build(spec: object, **kwargs: object) -> Any:
        captured_kwargs.update(kwargs)
        return _build_result(status="ok")

    monkeypatch.setattr(cli_module, "run_build", fake_run_build)

    exit_code = main(
        ["build", str(spec_file), "--output-dir", "out-dir", "--run-id", "run-1"],
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert captured_kwargs["output_root"] == Path("out-dir")
    assert captured_kwargs["run_id"] == "run-1"
    assert "build: dataset.sample (run run-1)" in out
    assert "datago:air_quality: ok [bronze, silver, gold]" in out
    assert "manifest:" in out


def test_build_reports_failed_sources_and_exits_one(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Any source failure → stderr + exit 1 — CI must not misinterpret as success."""
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: object())
    monkeypatch.setattr(
        cli_module,
        "run_build",
        lambda spec, **kwargs: _build_result(status="failed", error="upstream 503"),
    )

    exit_code = main(["build", str(spec_file)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "build failed for one or more sources" in captured.err
    assert "upstream 503" in captured.err


def test_build_rejects_unvalidatable_spec_before_touching_network(
    unvalidatable_spec_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validation failure must be caught before client creation (_no_real_client enforces this)."""
    assert main(["build", str(unvalidatable_spec_file)]) == 1
    assert "spec validation failed" in capsys.readouterr().err


def test_build_reports_missing_spec_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["build", str(tmp_path / "nope.yaml")]) == 1
    assert "failed to load spec" in capsys.readouterr().err


# --- preview ---------------------------------------------------------------


def _preview_result(*, status: str = "ok", error: str | None = None) -> Any:
    source = SimpleNamespace(
        source_key="datago:air_quality",
        status=status,
        error=error,
        schema=SimpleNamespace(
            columns=[
                SimpleNamespace(name="station", dtype="str"),
                SimpleNamespace(name="pm10", dtype="int"),
            ]
        ),
        preview=SimpleNamespace(rows=[{"station": "A", "pm10": 12}], total_rows=22),
    )
    return SimpleNamespace(previews=[source])


def test_preview_prints_schema_and_sample(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: object())

    def fake_preview(spec: object, **kwargs: object) -> Any:
        captured_kwargs.update(kwargs)
        return _preview_result()

    monkeypatch.setattr(cli_module, "preview_build", fake_preview)

    exit_code = main(["preview", str(spec_file), "--limit", "5"])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert captured_kwargs["limit"] == 5
    assert "preview: dataset.sample" in out
    assert "station (str), pm10 (int)" in out
    assert "sample (1 of 22 rows)" in out


def test_preview_uses_default_limit(spec_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kpubdata_builder.tabular import DEFAULT_PREVIEW_LIMIT

    captured_kwargs: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: object())

    def fake_preview(spec: object, **kwargs: object) -> Any:
        captured_kwargs.update(kwargs)
        return _preview_result()

    monkeypatch.setattr(cli_module, "preview_build", fake_preview)

    assert main(["preview", str(spec_file)]) == 0
    assert captured_kwargs["limit"] == DEFAULT_PREVIEW_LIMIT


def test_preview_failed_source_exits_one(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: object())
    monkeypatch.setattr(
        cli_module,
        "preview_build",
        lambda spec, **kwargs: _preview_result(status="failed", error="fetch timed out"),
    )

    exit_code = main(["preview", str(spec_file)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "preview failed for one or more sources" in captured.err
    assert "fetch timed out" in captured.err


def test_preview_invalid_limit_exits_one(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """User input error passing parser (like limit < 1) is ValueError → exit 1."""
    monkeypatch.setattr(cli_module, "_create_client", lambda **_: object())

    def raising_preview(spec: object, **kwargs: object) -> Any:
        raise ValueError("limit must be >= 1")

    monkeypatch.setattr(cli_module, "preview_build", raising_preview)

    assert main(["preview", str(spec_file), "--limit", "0"]) == 1
    assert "invalid preview input" in capsys.readouterr().err


def test_preview_reports_missing_spec_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["preview", str(tmp_path / "nope.yaml")]) == 1
    assert "failed to load spec" in capsys.readouterr().err


def test_preview_rejects_unvalidatable_spec(
    unvalidatable_spec_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["preview", str(unvalidatable_spec_file)]) == 1
    assert "spec validation failed" in capsys.readouterr().err


# --- publish ---------------------------------------------------------------


def test_publish_reports_missing_spec_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "a.jsonl").write_text("{}\n", encoding="utf-8")

    exit_code = main(
        [
            "publish",
            str(tmp_path / "nope.yaml"),
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(artifacts),
        ]
    )

    assert exit_code == 1
    assert "failed to load spec" in capsys.readouterr().err


def test_publish_rejects_unvalidatable_spec(
    unvalidatable_spec_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "a.jsonl").write_text("{}\n", encoding="utf-8")

    exit_code = main(
        [
            "publish",
            str(unvalidatable_spec_file),
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(artifacts),
        ]
    )

    assert exit_code == 1
    assert "spec validation failed" in capsys.readouterr().err


# --- serve -----------------------------------------------------------------


def test_serve_reads_max_workers_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--max-workers unspecified uses KPUBDATA_BUILDER_MAX_WORKERS (#374)."""
    import kpubdata_builder.service.http as http_module

    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_WORKERS", "3")
    seen: dict[str, object] = {}

    def fake_serve(service: object, *, host: str, port: int, max_workers: int) -> None:
        seen["max_workers"] = max_workers

    monkeypatch.setattr(http_module, "serve", fake_serve)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0
    assert seen["max_workers"] == 3


def test_serve_flag_overrides_env_max_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kpubdata_builder.service.http as http_module

    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_WORKERS", "3")
    seen: dict[str, object] = {}

    def fake_serve(service: object, *, host: str, port: int, max_workers: int) -> None:
        seen["max_workers"] = max_workers

    monkeypatch.setattr(http_module, "serve", fake_serve)

    assert main(["serve", "--output-dir", str(tmp_path), "--max-workers", "7"]) == 0
    assert seen["max_workers"] == 7


@pytest.mark.parametrize("workers", ["0", "-1"])
def test_serve_rejects_non_positive_max_workers(workers: str, tmp_path: Path) -> None:
    """Won't start with ≤0 workers — doesn't silently raise to 1."""
    with pytest.raises(SystemExit, match="max_workers must be >= 1"):
        main(["serve", "--output-dir", str(tmp_path), "--max-workers", workers])


def test_serve_handles_keyboard_interrupt_as_clean_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-C should be exit code 0, not stack trace."""
    import kpubdata_builder.service.http as http_module

    def interrupting_serve(service: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(http_module, "serve", interrupting_serve)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0
    assert "shutting down" in capsys.readouterr().err


# --- rebuild-index ---------------------------------------------------------


def test_rebuild_index_prints_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import kpubdata_builder.store as store_module

    seen: dict[str, object] = {}

    def fake_rebuild(output_root: Path) -> int:
        seen["output_root"] = output_root
        return 4

    monkeypatch.setattr(store_module, "rebuild_index", fake_rebuild)

    assert main(["rebuild-index", "--output-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert seen["output_root"] == tmp_path
    assert "rebuilt index with 4 build(s)" in out


def test_rebuild_index_reports_failure_as_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import kpubdata_builder.store as store_module

    def failing_rebuild(output_root: Path) -> int:
        raise OSError("disk gone")

    monkeypatch.setattr(store_module, "rebuild_index", failing_rebuild)

    assert main(["rebuild-index", "--output-dir", str(tmp_path)]) == 1
    assert "failed to rebuild index: disk gone" in capsys.readouterr().err


# --- prune-cancelled -------------------------------------------------------


def test_prune_cancelled_defaults_to_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without --apply, no deletion happens and output clearly says so."""
    import kpubdata_builder.retention as retention_module

    seen: dict[str, object] = {}

    def fake_prune(output_root: Path, *, ttl_hours: float | None, apply: bool) -> Any:
        seen.update(output_root=output_root, ttl_hours=ttl_hours, apply=apply)
        return SimpleNamespace(
            deleted=(),
            kept=(SimpleNamespace(run_id="run-kept"),),
            scanned=1,
            deleted_count=0,
        )

    monkeypatch.setattr(retention_module, "prune_cancelled_runs", fake_prune)

    assert main(["prune-cancelled", "--output-dir", str(tmp_path), "--ttl-hours", "48"]) == 0
    out = capsys.readouterr().out

    assert seen == {"output_root": tmp_path, "ttl_hours": 48.0, "apply": False}
    assert "dry run (nothing will be deleted)" in out
    assert "kept: run-kept" in out
    assert "scanned 1 cancelled partial run(s), deleted 0" in out


def test_prune_cancelled_apply_deletes_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import kpubdata_builder.retention as retention_module

    seen: dict[str, object] = {}

    def fake_prune(output_root: Path, *, ttl_hours: float | None, apply: bool) -> Any:
        seen["apply"] = apply
        return SimpleNamespace(deleted=("run-old",), kept=(), scanned=1, deleted_count=1)

    monkeypatch.setattr(retention_module, "prune_cancelled_runs", fake_prune)

    assert main(["prune-cancelled", "--output-dir", str(tmp_path), "--apply"]) == 0
    out = capsys.readouterr().out

    assert seen["apply"] is True
    assert "APPLY (deleting)" in out
    assert "deleted: run-old" in out
    assert "deleted 1" in out


def test_prune_cancelled_reads_ttl_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kpubdata_builder.retention as retention_module

    monkeypatch.setenv("KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS", "12.5")
    seen: dict[str, object] = {}

    def fake_prune(output_root: Path, *, ttl_hours: float | None, apply: bool) -> Any:
        seen["ttl_hours"] = ttl_hours
        return SimpleNamespace(deleted=(), kept=(), scanned=0, deleted_count=0)

    monkeypatch.setattr(retention_module, "prune_cancelled_runs", fake_prune)

    assert main(["prune-cancelled", "--output-dir", str(tmp_path)]) == 0
    assert seen["ttl_hours"] == 12.5


def test_prune_cancelled_flag_beats_env_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kpubdata_builder.retention as retention_module

    monkeypatch.setenv("KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS", "12.5")
    seen: dict[str, object] = {}

    def fake_prune(output_root: Path, *, ttl_hours: float | None, apply: bool) -> Any:
        seen["ttl_hours"] = ttl_hours
        return SimpleNamespace(deleted=(), kept=(), scanned=0, deleted_count=0)

    monkeypatch.setattr(retention_module, "prune_cancelled_runs", fake_prune)

    assert main(["prune-cancelled", "--output-dir", str(tmp_path), "--ttl-hours", "1"]) == 0
    assert seen["ttl_hours"] == 1.0


def test_prune_cancelled_rejects_non_numeric_env_ttl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If env var not numeric, don't attempt delete judgment, exit 1 (fail-closed)."""
    import kpubdata_builder.retention as retention_module

    monkeypatch.setenv("KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS", "yesterday")

    def forbidden_prune(*args: object, **kwargs: object) -> Any:
        raise AssertionError("must not prune with an unparsable TTL")

    monkeypatch.setattr(retention_module, "prune_cancelled_runs", forbidden_prune)

    assert main(["prune-cancelled", "--output-dir", str(tmp_path)]) == 1
    assert "is not a number of hours" in capsys.readouterr().err


def test_prune_cancelled_without_ttl_scans_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Without TTL set, real implementation also has no delete candidates —
    verify empty workspace path."""
    assert main(["prune-cancelled", "--output-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "scanned 0 cancelled partial run(s), deleted 0" in out

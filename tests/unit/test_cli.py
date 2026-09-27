"""Verify CLI entrypoint parser, exit codes, error messages."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from kpubdata import Client

import kpubdata_builder.cli as cli_module
from kpubdata_builder import __version__
from kpubdata_builder.cli import build_parser, main
from kpubdata_builder.publishers.base import PublishResult

# declare license. publish validates with publication-only rules so (#443) without it,
# publish tests all stop at validation stage — that's this gate's point.
VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
license: CC-BY-4.0
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)

INVALID_SPEC_YAML_NO_SOURCES = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources: []
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


def test_direct_cli_client_keeps_environment_cache_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct CLI client (not service) does not force cache override."""
    captured: list[dict[str, object]] = []

    def fake_from_env(**overrides: object) -> object:
        captured.append(overrides)
        return object()

    monkeypatch.setattr(Client, "from_env", fake_from_env)

    _ = cli_module._create_client()

    # Since kpubdata #276, from_env takes explicit parameters — unspecified values
    # None (environment rules apply) passed and cache not forced.
    assert captured == [{"provider_keys": None, "timeout": None, "cache": None}]


def test_build_parser_uses_program_name() -> None:
    # Confirm parser exposes expected program name.
    parser = build_parser()
    assert parser.prog == "kpubdata-builder"


def test_help_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # Verify --help call returns success exit code and help text.
    exit_code = main(["--help"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "kpubdata-builder" in captured.out
    assert "validate" in captured.out


def test_version_returns_zero(capsys: pytest.CaptureFixture[str]) -> None:
    # Confirm --version call prints version string.
    exit_code = main(["--version"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert __version__ in captured.out


def test_no_subcommand_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    # Verify argparse-style error code 2 returned when no subcommand.
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "kpubdata-builder" in captured.err


def test_unknown_command_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    # Confirm unknown command rejected with stderr.
    exit_code = main(["does-not-exist"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err


def test_validate_succeeds_for_valid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Valid YAML spec should succeed in validate command.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "dataset.sample" in captured.out
    assert captured.err == ""


def test_validate_fails_for_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If file missing, must return load failure message and exit code 1.
    missing = tmp_path / "missing.yaml"

    exit_code = main(["validate", str(missing)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err


def test_validate_fails_for_invalid_spec(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Confirm error printed if YAML syntax OK but required field validation fails.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(INVALID_SPEC_YAML_NO_SOURCES, encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err
    assert "sources" in captured.err


def test_validate_fails_for_malformed_yaml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Confirm broken YAML syntax treated as load failure.
    spec_path = tmp_path / "bad.yaml"
    _ = spec_path.write_text("{{{{not: valid: yaml: [", encoding="utf-8")

    exit_code = main(["validate", str(spec_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "failed to load spec" in captured.err


# ---------------------------------------------------------------------------
# publish command test
# ---------------------------------------------------------------------------


def test_publish_local_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --target local end-to-end: file copied to destination and summary printed.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "a.parquet").write_bytes(b"parquet1")
    (artifacts_dir / "b.parquet").write_bytes(b"parquet2")

    dest_dir = tmp_path / "dest"

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "local",
            "--destination",
            str(dest_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (dest_dir / "a.parquet").exists()
    assert (dest_dir / "b.parquet").exists()
    assert "publish: dataset.sample -> local" in captured.out
    assert "artifacts: 2" in captured.out
    assert captured.err == ""


def test_publish_missing_artifacts_dir_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If artifacts-dir doesn't exist, must return exit 1 and error message.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    missing_dir = tmp_path / "no-such-dir"

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(missing_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no artifacts found" in captured.err


def test_publish_empty_artifacts_dir_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # If artifacts-dir empty, must return exit 1 and error message.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "empty"
    artifacts_dir.mkdir()

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "no artifacts found" in captured.err


def test_publish_unknown_target_rejected_by_argparse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # --target zzz は argparse が拒否して exit code 2 を返す.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "f.txt").write_text("x", encoding="utf-8")

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "zzz",
            "--destination",
            str(tmp_path / "dest"),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )

    assert exit_code == 2


def test_publish_huggingface_stub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Replace HuggingFace publisher with stub to check publish called with correct args
    # and returns exit 0.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    artifact_file = artifacts_dir / "data.parquet"
    artifact_file.write_bytes(b"data")

    fake_result = PublishResult(
        publisher="huggingface",
        reference="https://huggingface.co/datasets/org/dataset",
        artifact_count=1,
    )
    stub = MagicMock()
    # Real HuggingFacePublisher takes per-file input, so stub matches similarly.
    stub.expects_directory = False
    stub.publish.return_value = fake_result

    import kpubdata_builder.cli as cli_module

    monkeypatch.setitem(cli_module.PUBLISHER_REGISTRY, "huggingface", stub)

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "huggingface",
            "--destination",
            "org/dataset",
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    stub.publish.assert_called_once_with(
        (artifact_file,),
        destination="org/dataset",
    )
    assert "publish: dataset.sample -> huggingface" in captured.out
    assert "artifacts: 1" in captured.out


def test_publish_publish_error_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # publisher が PublishError を送出した場合は exit 1 と stderr エラーを返す.
    # LocalPublisher の basename 衝突パスを使ってトリガーする.
    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    sub_a = artifacts_dir / "a"
    sub_b = artifacts_dir / "b"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    # Same basename file in different subdirs → LocalPublisher raises PublishError
    (sub_a / "data.parquet").write_bytes(b"1")
    (sub_b / "data.parquet").write_bytes(b"2")

    dest_dir = tmp_path / "dest"

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "local",
            "--destination",
            str(dest_dir),
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "publish failed" in captured.err


def test_publish_kaggle_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # --target kaggle end-to-end: CLI passes directory containing dataset-metadata.json
    # to KagglePublisher and verifies upload called via fake API (#176, #181).
    import json
    import sys
    import types

    spec_path = tmp_path / "spec.yaml"
    _ = spec_path.write_text(VALID_SPEC_YAML, encoding="utf-8")

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "data.csv").write_text("id\n1\n", encoding="utf-8")
    (artifacts_dir / "dataset-metadata.json").write_text(
        json.dumps({"id": "kpub/sample", "title": "Sample", "resources": []}),
        encoding="utf-8",
    )

    calls: list[str] = []

    class _FakeApi:
        def authenticate(self) -> None:
            calls.append("authenticate")

        def dataset_list(self, *, mine: bool, search: str) -> list[str]:
            del mine, search
            return []

        def dataset_create_new(self, *args: object, **kwargs: object) -> None:
            del args
            calls.append(f"create_new:public={kwargs.get('public')}")

        def dataset_create_version(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            calls.append("create_version")

    extended = types.ModuleType("kaggle.api.kaggle_api_extended")
    extended.KaggleApi = lambda: _FakeApi()  # type: ignore[attr-defined]
    api_pkg = types.ModuleType("kaggle.api")
    kaggle_pkg = types.ModuleType("kaggle")
    monkeypatch.setitem(sys.modules, "kaggle", kaggle_pkg)
    monkeypatch.setitem(sys.modules, "kaggle.api", api_pkg)
    monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", extended)

    exit_code = main(
        [
            "publish",
            str(spec_path),
            "--target",
            "kaggle",
            "--destination",
            "kpub/sample",
            "--artifacts-dir",
            str(artifacts_dir),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0, captured.err
    assert "authenticate" in calls
    # Without public flag, must be created private.
    assert "create_new:public=False" in calls
    assert "publish: dataset.sample -> kaggle" in captured.out
    assert "artifacts: 1" in captured.out


def test_serve_invokes_http_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """serve command must call http.serve with correct host/port (#249).

    이 테스트는 한동안 ``test_publish_kaggle_end_to_end`` 안에 중첩 정의돼 있어
    pytest 가 수집하지 못했고(#595), 본문만 kaggle 테스트 꼬리에 붙어 실행됐다.
    그래서 아래 ``delenv`` 가드(#374 review)는 한 번도 실행되지 않았다.
    """
    # Block leak of external KPUBDATA_BUILDER_MAX_WORKERS env (#374 review).
    monkeypatch.delenv("KPUBDATA_BUILDER_MAX_WORKERS", raising=False)

    import kpubdata_builder.service.http as http_module
    from kpubdata_builder.service import BuilderService

    captured_kwargs: dict[str, object] = {}

    def fake_serve(service: object, *, host: str, port: int, max_workers: int) -> None:
        captured_kwargs["host"] = host
        captured_kwargs["port"] = port
        captured_kwargs["max_workers"] = max_workers
        # Verify --output-dir correctly passed to BuilderService.output_root (#249 review).
        assert isinstance(service, BuilderService)
        captured_kwargs["output_root"] = service._output_root
        captured_kwargs["async_max_workers"] = service._async_builds._executor._max_workers

    monkeypatch.setattr(http_module, "serve", fake_serve)

    exit_code = main(
        [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9123",
            "--output-dir",
            str(tmp_path),
        ]
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    # --max-workers unspecified → default (10) is passed (#374).
    assert captured_kwargs == {
        "host": "0.0.0.0",
        "port": 9123,
        "max_workers": 10,
        "async_max_workers": 10,
        "output_root": tmp_path,
    }
    assert "serving kpubdata-builder" in out

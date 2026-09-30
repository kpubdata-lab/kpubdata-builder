"""Integration tests for production failure modes (#125).

Tests cover:
- Malformed YAML spec loading
- Invalid run_id in Bronze persistence
- Export path traversal safety
- CLI entrypoint error handling
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import load_spec
from kpubdata_builder.stages.bronze.models import BronzeArtifact
from kpubdata_builder.stages.bronze.persist import persist_bronze_artifact

# --- Malformed YAML ---


def test_load_spec_rejects_non_mapping_yaml(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="top-level YAML must be a mapping"):
        load_spec(tmp_path / "bad.yaml")


def test_load_spec_rejects_invalid_yaml_syntax(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("{{broken: yaml: [", encoding="utf-8")
    with pytest.raises(SpecLoadError, match="Failed to load"):
        load_spec(tmp_path / "bad.yaml")


def test_load_spec_rejects_missing_required_fields(tmp_path: Path) -> None:
    (tmp_path / "partial.yaml").write_text("dataset_id: test\n", encoding="utf-8")
    with pytest.raises(SpecLoadError):
        load_spec(tmp_path / "partial.yaml")


# --- Invalid run_id in Bronze persistence ---


def test_bronze_persist_rejects_path_traversal(tmp_path: Path) -> None:
    artifact = BronzeArtifact.from_records(source_key="datago.test", records=())
    with pytest.raises(ValueError, match="unsafe characters"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="../escape")


def test_bronze_persist_rejects_empty_run_id(tmp_path: Path) -> None:
    artifact = BronzeArtifact.from_records(source_key="datago.test", records=())
    with pytest.raises(ValueError, match="must not be empty"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="")


# --- CLI entrypoint error handling ---


def test_cli_validate_nonexistent_file(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["validate", "/nonexistent/spec.yaml"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "failed to load spec" in captured.err


def test_cli_no_args_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "kpubdata-builder" in captured.err

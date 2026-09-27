"""Canonical query artifact resolution tests."""

from __future__ import annotations

from pathlib import Path

import pytest

import kpubdata_builder.query.resolver as resolver_module
from kpubdata_builder.query.models import QueryRequest
from kpubdata_builder.query.resolver import (
    QueryArtifactUnavailableError,
    resolve_query_context,
)
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.stages._stage_reader import gold_source_dir, silver_source_dir


@pytest.mark.parametrize("stage", ["silver", "gold"])
def test_resolver_uses_stage_canonical_source_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    manifest = {"inputs": ["source"]}
    monkeypatch.setattr(
        resolver_module.datasets_service, "read_manifest", lambda root, run_id: manifest
    )
    monkeypatch.setattr(
        resolver_module.datasets_service,
        "read_snapshot_dataset_id",
        lambda root, run_id: "dataset-id",
    )
    monkeypatch.setattr(
        resolver_module.stages_service,
        "stage_status_for_source",
        lambda root, run_id, value, source: object(),
    )
    monkeypatch.setattr(
        resolver_module.stages_service,
        "stage_status_of",
        lambda status, requested_stage: "completed",
    )
    source_dir = (
        silver_source_dir(tmp_path, "run-1", "source")
        if stage == "silver"
        else gold_source_dir(tmp_path, "run-1", "source")
    )
    source_dir.mkdir(parents=True)
    table_path = source_dir / "table.parquet"
    table_path.write_bytes(b"fixture")
    request = QueryRequest(
        dataset_id="dataset-id",
        run_id="run-1",
        stage=stage,  # type: ignore[arg-type]
        source="source",
        sql="SELECT * FROM dataset",
    )

    resolved = resolve_query_context(tmp_path, request, Principal("dev"))

    assert resolved.table_path == table_path


@pytest.mark.parametrize("stage", ["silver", "gold"])
def test_path_safety_failure_is_fail_closed_without_path_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """ensure_within failure (ValueError) becomes artifact unavailable without path info,
    handled (#504 review — original exception does not leak as invalid_request)."""
    manifest = {"inputs": ["source"]}
    monkeypatch.setattr(
        resolver_module.datasets_service, "read_manifest", lambda root, run_id: manifest
    )
    monkeypatch.setattr(
        resolver_module.datasets_service,
        "read_snapshot_dataset_id",
        lambda root, run_id: "dataset-id",
    )
    monkeypatch.setattr(
        resolver_module.stages_service,
        "stage_status_for_source",
        lambda root, run_id, value, source: object(),
    )
    monkeypatch.setattr(
        resolver_module.stages_service,
        "stage_status_of",
        lambda status, requested_stage: "completed",
    )

    def _reject(_run_dir: Path, table_path: Path, label: str) -> None:
        raise ValueError(f"{label} escapes workspace: {table_path}")

    monkeypatch.setattr(resolver_module, "ensure_within", _reject)
    source_dir = (
        silver_source_dir(tmp_path, "run-1", "source")
        if stage == "silver"
        else gold_source_dir(tmp_path, "run-1", "source")
    )
    source_dir.mkdir(parents=True)
    (source_dir / "table.parquet").write_bytes(b"fixture")
    request = QueryRequest(
        dataset_id="dataset-id",
        run_id="run-1",
        stage=stage,  # type: ignore[arg-type]
        source="source",
        sql="SELECT * FROM dataset",
    )

    with pytest.raises(QueryArtifactUnavailableError):
        resolve_query_context(tmp_path, request, Principal("dev"))

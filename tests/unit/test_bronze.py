"""Verify Bronze stage model, fetch, persist operations."""

from __future__ import annotations

import json
from collections.abc import Generator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze import (
    BronzeArtifact,
    ProvenanceEvent,
    build_bronze_artifact,
    persist_bronze_artifact,
)
from kpubdata_builder.stages.bronze.build import DatasetResult, SourceDataset


@dataclass(frozen=True)
class FakeResult:
    """Test result object satisfying DatasetResult Protocol."""

    items: list[dict[str, JsonValue]]


class FakeDataset:
    """Test dataset returning specified records as-is."""

    def __init__(self, records: list[dict[str, JsonValue]]) -> None:
        self.records = records
        self.seen_params: dict[str, JsonValue] | None = None

    def list(self, **params: JsonValue) -> DatasetResult:
        self.seen_params = dict(params)
        return FakeResult(items=self.records)


class FakePaginatedDataset:
    def __init__(self, pages: tuple[list[dict[str, JsonValue]], ...]) -> None:
        self.pages = pages
        self.list_calls = 0
        self.list_all_params: dict[str, JsonValue] | None = None

    def list(self, **params: JsonValue) -> DatasetResult:
        self.list_calls += 1
        return FakeResult(items=self.pages[0])

    def list_all(self, **params: JsonValue) -> Generator[DatasetResult, None, None]:
        self.list_all_params = dict(params)
        for page in self.pages:
            yield FakeResult(items=page)


class FakeClient:
    """Test client recording whether dataset was called."""

    def __init__(self, dataset: SourceDataset) -> None:
        self.dataset_instance = dataset
        self.seen_source_key = ""

    def dataset(self, source_key: str) -> SourceDataset:
        self.seen_source_key = source_key
        return self.dataset_instance


def test_bronze_models_preserve_record_count_and_timezone() -> None:
    # Check model preserves record count and timezone-aware datetime.
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    provenance = ProvenanceEvent(
        source_key="datago.apt_trade",
        fetch_params={"page": 1},
        fetched_at=fetched_at,
    )
    artifact = BronzeArtifact.from_records(
        source_key="datago.apt_trade",
        records=({"id": "1"}, {"id": "2"}),
        fetch_params={"page": 1},
        fetched_at=fetched_at,
        provenance=provenance,
    )

    assert artifact.record_count == 2
    assert artifact.fetched_at.tzinfo is not None
    assert artifact.provenance == provenance


def test_bronze_models_reject_naive_fetched_at() -> None:
    # Verify naive datetime is immediately rejected on provenance/model creation.
    naive = datetime(2026, 5, 8, 12, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        ProvenanceEvent(source_key="datago.apt_trade", fetched_at=naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        BronzeArtifact.from_records(source_key="datago.apt_trade", records=(), fetched_at=naive)


def test_build_bronze_artifact_fetches_raw_records_without_transforming() -> None:
    # Check fetch result wrapped as BronzeArtifact with provenance unchanged.
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    records: list[dict[str, JsonValue]] = [
        {"id": "1", "name": "강남구", "amount": 100, "nested": {"b": 2, "a": 1}},
        {"id": "2", "name": "서초구", "amount": None, "tags": ["raw", "bronze"]},
    ]
    dataset = FakeDataset(records)
    client = FakeClient(dataset)

    artifact = build_bronze_artifact(
        client,
        source_key="datago.apt_trade",
        fetch_params={"lawd_cd": "11680", "deal_ymd": "202501"},
        fetched_at=fetched_at,
    )

    assert client.seen_source_key == "datago.apt_trade"
    assert dataset.seen_params == {"lawd_cd": "11680", "deal_ymd": "202501"}
    assert artifact.source_key == "datago.apt_trade"
    assert artifact.fetch_params == {"lawd_cd": "11680", "deal_ymd": "202501"}
    assert artifact.fetched_at == fetched_at
    assert tuple(artifact.iter_records()) == tuple(records)
    assert list(artifact.iter_records())[0]["nested"] == {"b": 2, "a": 1}
    assert artifact.record_count == 2
    assert artifact.provenance == ProvenanceEvent(
        source_key="datago.apt_trade",
        fetch_params={"lawd_cd": "11680", "deal_ymd": "202501"},
        fetched_at=fetched_at,
    )


def test_build_bronze_artifact_uses_list_all_when_available() -> None:
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    dataset = FakePaginatedDataset(([{"id": "1"}], [{"id": "2"}]))
    client = FakeClient(dataset)

    artifact = build_bronze_artifact(
        client,
        source_key="datago.apt_trade",
        fetch_params={"page_size": 1},
        fetched_at=fetched_at,
    )

    assert dataset.list_calls == 0
    assert dataset.list_all_params == {"page_size": 1}
    assert tuple(artifact.iter_records()) == ({"id": "1"}, {"id": "2"})
    assert artifact.record_count == 2


def test_persist_bronze_artifact_writes_jsonl_and_metadata(tmp_path: Path) -> None:
    # Verify persist writes JSONL body and metadata summary to same directory.
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    artifact = BronzeArtifact.from_records(
        source_key="datago.apt_trade",
        records=(
            {"id": "1", "name": "강남구", "nested": {"b": 2, "a": 1}},
            {"id": "2", "name": "서초구", "amount": None},
        ),
        fetch_params={"lawd_cd": "11680"},
        fetched_at=fetched_at,
        provenance=ProvenanceEvent(
            source_key="datago.apt_trade",
            fetch_params={"lawd_cd": "11680"},
            fetched_at=fetched_at,
        ),
    )

    result = persist_bronze_artifact(artifact, output_root=tmp_path / "build", run_id="run-1")

    assert "run-1" in str(result.bronze_dir)
    assert "bronze" in str(result.bronze_dir)
    assert "datago.apt_trade" in str(result.bronze_dir)
    assert result.records_path == result.bronze_dir / "raw_records.jsonl"
    assert result.metadata_path == result.bronze_dir / "metadata.json"

    jsonl_records = [
        json.loads(line) for line in result.records_path.read_text(encoding="utf-8").splitlines()
    ]
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))

    assert jsonl_records == list(tuple(artifact.iter_records()))
    assert metadata["source_key"] == "datago.apt_trade"
    assert metadata["fetch_params"] == {"lawd_cd": "11680"}
    assert metadata["fetched_at"] == "2026-05-08T12:00:00+00:00"
    assert metadata["record_count"] == 2
    assert metadata["artifact_paths"] == {
        "records": "raw_records.jsonl",
        "metadata": "metadata.json",
    }
    assert metadata["provenance"] == {
        "operation": "fetch",
        "source_key": "datago.apt_trade",
        "fetch_params": {"lawd_cd": "11680"},
        "fetched_at": "2026-05-08T12:00:00+00:00",
    }


def test_persist_bronze_artifact_separates_different_params(tmp_path: Path) -> None:
    # Check different fetch_params use different artifact path even with same source_key.
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    artifact_a = BronzeArtifact.from_records(
        source_key="datago.apt_trade",
        records=({"id": "1"},),
        fetch_params={"lawd_cd": "11680"},
        fetched_at=fetched_at,
    )
    artifact_b = BronzeArtifact.from_records(
        source_key="datago.apt_trade",
        records=({"id": "2"},),
        fetch_params={"lawd_cd": "11650"},
        fetched_at=fetched_at,
    )

    result_a = persist_bronze_artifact(artifact_a, output_root=tmp_path, run_id="run-1")
    result_b = persist_bronze_artifact(artifact_b, output_root=tmp_path, run_id="run-1")

    assert result_a.bronze_dir != result_b.bronze_dir
    assert result_a.records_path.read_text(encoding="utf-8").strip() == '{"id": "1"}'
    assert result_b.records_path.read_text(encoding="utf-8").strip() == '{"id": "2"}'


def test_persist_bronze_artifact_rejects_unsafe_run_id(tmp_path: Path) -> None:
    # Verify run_id with path escape risk is blocked in advance.
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    artifact = BronzeArtifact.from_records(
        source_key="datago.apt_trade",
        records=(),
        fetched_at=fetched_at,
    )

    with pytest.raises(ValueError, match="unsafe characters"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="../escape")

    with pytest.raises(ValueError, match="unsafe characters"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="/absolute")

    with pytest.raises(ValueError, match="must not be empty"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="")


def test_canonical_line_writes_native_types_as_text() -> None:
    """#979: a Parquet upload's Decimal/date/datetime arrives as native Python
    objects; the persisted Bronze file is plain JSON, so they become text."""
    import datetime as dt
    from decimal import Decimal

    from kpubdata_builder.stages.bronze.writer import canonical_line

    record = {
        "v": Decimal("2.50"),
        "d": dt.date(2024, 1, 1),
        "ts": dt.datetime(2024, 1, 1, 12, 30),
        "s": "text",
        "n": 42,
        "ok": True,
        "nil": None,
    }

    line = canonical_line(record)
    parsed = json.loads(line)

    assert parsed["v"] == "2.50"
    assert parsed["d"] == "2024-01-01"
    assert "2024-01-01" in parsed["ts"]
    # JSON-native types are unaffected by the default handler.
    assert parsed["s"] == "text"
    assert parsed["n"] == 42
    assert parsed["ok"] is True
    assert parsed["nil"] is None

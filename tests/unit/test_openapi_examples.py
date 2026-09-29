"""Prevent schema, extraction format, runtime serializer drift of OpenAPI named examples."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

import kpubdata_builder.service.datasets as datasets_module
import kpubdata_builder.service.query_service_api as query_module
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.resolver import ResolvedQueryContext
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import API_CONTRACT_VERSION, BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets import RunRecord
from kpubdata_builder.service.quality import summarize_run_quality
from kpubdata_builder.spec import JsonValue, parse_spec
from kpubdata_builder.spec.serializer import write_buildspec_snapshot
from kpubdata_builder.stages._stage_reader import SourceStageSummary

from ._openapi import resolve_ref, validate

_ROOT = Path(__file__).parents[2]
_CONTRACT_PATH = _ROOT / "contract" / "builder-api.yaml"
_SCRIPT_PATH = _ROOT / "scripts" / "extract_openapi_examples.py"
_HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}


def _contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8")))


def _resolved(contract: dict[str, Any], node: Any) -> Any:
    seen: set[str] = set()
    while isinstance(node, dict) and set(node) == {"$ref"}:
        ref = node["$ref"]
        if not isinstance(ref, str) or ref in seen:
            raise ValueError(f"invalid local ref: {ref!r}")
        seen.add(ref)
        node = resolve_ref(contract, ref)
    return node


def _iter_media_examples() -> Iterator[tuple[str, str, str, str, dict[str, Any], dict[str, Any]]]:
    contract = _contract()
    for path, path_item in contract["paths"].items():
        for method, operation in path_item.items():
            if method not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            request = _resolved(contract, operation.get("requestBody"))
            if isinstance(request, dict):
                yield from _examples_in_content(
                    contract, path, method, "request", request.get("content", {})
                )
            for status, raw_response in operation.get("responses", {}).items():
                response = _resolved(contract, raw_response)
                if isinstance(response, dict):
                    yield from _examples_in_content(
                        contract,
                        path,
                        method,
                        f"response:{status}",
                        response.get("content", {}),
                    )


def _examples_in_content(
    contract: dict[str, Any], path: str, method: str, location: str, content: Any
) -> Iterator[tuple[str, str, str, str, dict[str, Any], dict[str, Any]]]:
    if not isinstance(content, dict):
        return
    for media_type, media in content.items():
        if not isinstance(media, dict):
            continue
        schema = media.get("schema")
        examples = media.get("examples", {})
        if not isinstance(schema, dict) or not isinstance(examples, dict):
            continue
        for name, raw_example in examples.items():
            example = _resolved(contract, raw_example)
            if isinstance(example, dict):
                yield path, method, location, media_type, {"name": name, **example}, schema


def _example(path: str, method: str, location: str, name: str) -> Any:
    for item_path, item_method, item_location, _media, example, _schema in _iter_media_examples():
        if (item_path, item_method, item_location, example["name"]) == (
            path,
            method.lower(),
            location,
            name,
        ):
            return example["value"]
    raise AssertionError(f"example not found: {method} {path} {location} {name}")


def test_every_named_media_example_conforms_to_its_schema() -> None:
    contract = _contract()
    found = 0
    for path, method, location, media_type, example, schema in _iter_media_examples():
        found += 1
        errors = validate(example["value"], schema, contract)
        assert not errors, (
            f"{method.upper()} {path} {location} {media_type} {example['name']}: "
            + "; ".join(errors)
        )
    assert found >= 50


def test_priority_routes_have_named_examples() -> None:
    covered = {
        (path, method, location) for path, method, location, *_rest in _iter_media_examples()
    }
    expected = {
        ("/validate", "post", "request"),
        ("/catalog", "get", "response:200"),
        ("/preview", "post", "response:200"),
        ("/build", "post", "response:200"),
        ("/builds", "get", "response:200"),
        ("/builds/{run_id}/spec", "get", "response:200"),
        ("/builds/{run_id}/manifest", "get", "response:200"),
        ("/artifacts/{run_id}", "get", "response:200"),
        ("/datasets/{dataset_id}", "get", "response:200"),
        ("/datasets/{dataset_id}/runs", "get", "response:200"),
        ("/builds/{run_id}/stages/{stage}", "get", "response:200"),
        ("/builds/{run_id}/quality", "get", "response:200"),
        ("/datasets/{dataset_id}/quality/history", "get", "response:200"),
        ("/providers/{provider}/status", "get", "response:200"),
        ("/providers/{provider}/credential", "put", "request"),
        ("/query", "post", "response:200"),
        ("/query", "post", "response:429"),
        ("/query", "post", "response:504"),
    }
    assert expected <= covered


def test_examples_are_secret_safe_and_have_no_internal_absolute_paths() -> None:
    serialized = json.dumps(
        [example["value"] for *_prefix, example, _schema in _iter_media_examples()],
        ensure_ascii=False,
    )
    assert "/home/" not in serialized
    assert "/tmp/" not in serialized
    assert "sk-" not in serialized
    assert "BEGIN PRIVATE KEY" not in serialized
    credential = _example("/providers/{provider}/credential", "put", "request", "ReplaceCredential")
    assert credential == {"credential": "replace-with-your-provider-key"}


def test_query_example_has_main_1_9_0_result_fields() -> None:
    result = _example("/query", "post", "response:200", "AveragePm10Result")
    assert set(result) == {
        "columns",
        "rows",
        "truncated",
        "execution_ms",
        "startup_ms",
        "engine_execution_ms",
    }


def test_extraction_script_emits_documented_json_shape() -> None:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp949"
    completed = subprocess.run(
        [sys.executable, str(_SCRIPT_PATH)],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
        # Script outputs with ensure_ascii=False (Korean originals), so Windows cp949
        # default decoding is not used (#553).
        encoding="utf-8",
        env=env,
    )
    payload = json.loads(completed.stdout)
    assert "—" in completed.stdout
    # Sourced from canonical. Hard-coding causes this to lag every contract version bump,
    # with failure saying only "version bumped" — we already know that.
    assert payload["contract_version"] == API_CONTRACT_VERSION
    assert len(payload["examples"]) >= 50
    assert set(payload["examples"][0]) == {
        "path",
        "method",
        "location",
        "media_type",
        "name",
        "summary",
        "value",
    }


class _ExampleQueryService:
    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del table_path, canonical_sql, limit
        return QueryResult(
            ("station_name", "avg_pm10"),
            (
                {"station_name": "종로구", "avg_pm10": 31.5},
                {"station_name": "중구", "avg_pm10": 28.0},
            ),
            False,
            7,
            12,
            5,
            (
                {"name": "station_name", "logical_type": "string", "wire_encoding": "string"},
                {"name": "avg_pm10", "logical_type": "float64", "wire_encoding": "number"},
            ),
        )


def test_query_success_example_matches_service_serializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        query_module,
        "resolve_query_context",
        lambda root, request, principal: ResolvedQueryContext(
            request.dataset_id,
            request.run_id,
            request.stage,
            request.source or "air",
            tmp_path / "table.parquet",
        ),
    )
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda: cast(object, None),
        query_service=cast(QueryService, _ExampleQueryService()),
    )
    request = _example("/query", "post", "request", "AveragePm10ByStation")

    response = service.query(request, principal=Principal("dev"))

    assert response.status_code == 200
    assert response.body == _example("/query", "post", "response:200", "AveragePm10Result")


def test_dataset_detail_example_matches_service_serializer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = parse_spec(
        {
            "dataset_id": "seoul-air-quality",
            "title": "서울 대기질 관측",
            "description": "서울 지역 측정소별 대기오염 관측 데이터",
            "sources": [{"provider": "datago", "dataset": "air_quality", "alias": "air"}],
            "exports": [{"kind": "jsonl", "output_path": "artifacts/air-quality.jsonl"}],
        }
    )
    write_buildspec_snapshot(spec, output_root=tmp_path, run_id="air-quality-20260815")
    manifest = {
        "started_at": "2026-08-15T09:30:00+00:00",
        "finished_at": "2026-08-15T09:30:07+00:00",
        "errors": [],
        "row_counts": {"air": 25},
    }
    (tmp_path / "air-quality-20260815" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    record = RunRecord(
        "air-quality-20260815",
        "seoul-air-quality",
        "ok",
        "2026-08-15T09:30:00+00:00",
        "2026-08-15T09:30:07+00:00",
        None,
        None,
    )
    older_record = RunRecord(
        "air-quality-20260814",
        "seoul-air-quality",
        "failed",
        "2026-08-14T09:30:00+00:00",
        "2026-08-14T09:30:02+00:00",
        None,
        None,
    )
    monkeypatch.setattr(
        datasets_module,
        "list_run_stages",
        lambda root, run_id, value: [
            SourceStageSummary("air", "completed", "completed", "completed")
        ],
    )
    service = BuilderService(output_root=tmp_path, client_factory=lambda: cast(object, None))
    # dataset record collection moved to datasets domain service (#596) — BuilderService's
    # same-name method only delegates, so patching it misses the call path.
    monkeypatch.setattr(
        service._datasets_api,
        "dataset_records_for",
        lambda dataset_id, principal: [record, older_record],
    )

    response = service.get_dataset("seoul-air-quality")

    assert response.status_code == 200
    assert response.body == _example(
        "/datasets/{dataset_id}", "get", "response:200", "AirQualityDataset"
    )


def _quality_result(status: str, *, rule: str, **overrides: JsonValue) -> dict[str, JsonValue]:
    return {
        "source_key": "air",
        "category": "row_count" if rule == "min_rows" else "missing",
        "rule": rule,
        "column": None if rule == "min_rows" else "pm10_value",
        "status": status,
        "actual": 25 if rule == "min_rows" else 0.04,
        "threshold": 10 if rule == "min_rows" else 0.01,
        "affected_rows": None if rule == "min_rows" else 1,
        "evaluated_rows": None if rule == "min_rows" else 25,
        "detail": None,
        **overrides,
    }


def test_quality_examples_match_service_serializers(tmp_path: Path) -> None:
    quality_results = {
        "air": [
            _quality_result("pass", rule="min_rows"),
            _quality_result("warn", rule="max_null_ratio"),
        ]
    }
    schema_drift = {
        "air": [
            {
                "kind": "column_added",
                "column": "station_code",
                "detail": "column added since previous successful run",
            }
        ]
    }
    manifest = {
        "inputs": ["air"],
        "row_counts": {"air": 25},
        "quality_results": quality_results,
        "schema_drift": schema_drift,
    }
    run_dir = tmp_path / "air-quality-20260815"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    service = BuilderService(output_root=tmp_path, client_factory=lambda: cast(object, None))

    detail = service.get_build_quality("air-quality-20260815")

    assert detail.body == _example(
        "/builds/{run_id}/quality", "get", "response:200", "EvaluatedQuality"
    )

    history_manifest = {
        "row_counts": {"air": 25},
        "quality_results": {
            "air": [
                _quality_result("pass", rule="min_rows"),
                _quality_result("pass", rule="min_rows"),
                _quality_result("warn", rule="max_null_ratio"),
            ]
        },
    }
    record = RunRecord(
        "air-quality-20260815",
        "seoul-air-quality",
        "ok",
        "2026-08-15T09:30:00+00:00",
        "2026-08-15T09:30:07+00:00",
        None,
        None,
    )
    history = _example(
        "/datasets/{dataset_id}/quality/history", "get", "response:200", "AirQualityHistory"
    )
    assert summarize_run_quality(record, history_manifest) == history["runs"][0]

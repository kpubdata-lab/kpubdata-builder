"""Builder Service Contract(#63, #226, #317, #319, #209) OpenAPI 스펙의 구조·런타임 검증.

설치된 OpenAPI validator가 없으므로, 계약이 OpenAPI 3.1이고 BuilderService
(service/app.py)가 실제로 구현한 동기 라우트와 wire 형태를 모두 담는지 구조적으로
검증한다. 계약은 이제 구현된 엔드포인트만 기술하며(#226), 한쪽만 바뀌는 조용한
드리프트를 막기 위해 구현 라우트 ↔ 계약 operationId 매핑을 명시적으로 고정한다(#317).
또한 YAML 계약과 실제 dispatch 구현 간의 양방향 일치성을 검증하며(#317),
상태 코드와 응답 스키마 검증으로 범위를 확장한다(#319).

구조 검증(위)은 YAML의 *선언*과 dispatch의 *라우팅 목록*이 일치하는지 본다(#317, #319).
``TestResponseConformance``(#209, ADR-0005)는 한 단계 더 나아가 **실제 dispatch 응답
본문이 선언된 스키마에 부합하는지**(wire-level conformance)를 순수 파이썬
validator(``_openapi.py``)로 검증한다 — #319의 정적 스키마 검사가 "스키마가 required를
선언했는가"만 보듯, app.py 응답에서 필수 필드가 빠지거나 타입이 바뀌어도 선언부가
그대로면 정적 검사는 통과하지만 이 런타임 검사는 잡는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

# dispatch에 구현된 (path, method, operationId) 매핑.
# service/app.py:dispatch의 라우팅 규칙을 기계적으로 추출하기 어렵기 때문에
# 명시적으로 선언하여 유지보수성을 높인다.
_DISPATCH_ROUTES: dict[tuple[str, str], str] = {
    ("/admin/runs", "GET"): "adminListRuns",
    ("/admin/config", "GET"): "adminGetConfig",
    ("/healthz", "GET"): "healthz",
    ("/version", "GET"): "getVersion",
    ("/catalog", "GET"): "getCatalog",
    ("/providers", "GET"): "listProviders",
    ("/providers/{provider}/status", "GET"): "getProviderStatus",
    ("/providers/{provider}/test", "POST"): "testProviderConnection",
    ("/providers/{provider}/credential", "GET"): "getProviderCredential",
    ("/providers/{provider}/credential", "PUT"): "putProviderCredential",
    ("/providers/{provider}/credential", "DELETE"): "deleteProviderCredential",
    ("/query", "POST"): "queryBuiltDataset",
    ("/validate", "POST"): "validateSpec",
    ("/preview", "POST"): "previewBuild",
    ("/build", "POST"): "createBuild",
    ("/builds", "GET"): "listBuilds",
    ("/builds", "POST"): "submitBuild",
    ("/builds/{run_id}", "GET"): "getBuildJob",
    ("/builds/{run_id}/cancel", "POST"): "cancelBuildJob",
    ("/builds/{run_id}/manifest", "GET"): "getBuildManifest",
    ("/builds/{run_id}/spec", "GET"): "getBuildSpecSnapshot",
    ("/artifacts/{run_id}", "GET"): "listBuildArtifacts",
    ("/datasets", "GET"): "listDatasets",
    ("/datasets/{dataset_id}", "GET"): "getDataset",
    ("/datasets/{dataset_id}/runs", "GET"): "listDatasetRuns",
    ("/datasets/{dataset_id}/quality/history", "GET"): "getDatasetQualityHistory",
    ("/builds/{run_id}/stages", "GET"): "listBuildStages",
    ("/builds/{run_id}/stages/{stage}", "GET"): "getBuildStageDetail",
    ("/builds/{run_id}/quality", "GET"): "getBuildQuality",
    ("/quality/summary", "GET"): "getQualitySummary",
    ("/builds/{run_id}/events", "GET"): "getBuildEvents",
    ("/builds/{run_id}/publish/readiness", "GET"): "getPublishReadiness",
    ("/builds/{run_id}/publish", "POST"): "publishBuild",
    ("/monitoring/summary", "GET"): "getMonitoringSummary",
    ("/monitoring/builds", "GET"): "getMonitoringBuilds",
    ("/uploads", "POST"): "createUpload",
    ("/uploads/{upload_id}", "GET"): "getUpload",
    ("/uploads/{upload_id}", "DELETE"): "deleteUpload",
}

# (path, method) 형태의 계약 필수 오퍼레이션. BuilderService.dispatch가 실제로
# 라우팅하는 동기 엔드포인트와 1:1로 대응한다.
_REQUIRED_OPERATIONS = [
    ("/healthz", "get"),
    ("/version", "get"),
    ("/catalog", "get"),
    ("/providers", "get"),
    ("/providers/{provider}/status", "get"),
    ("/providers/{provider}/test", "post"),
    ("/providers/{provider}/credential", "get"),
    ("/providers/{provider}/credential", "put"),
    ("/providers/{provider}/credential", "delete"),
    ("/query", "post"),
    ("/validate", "post"),
    ("/preview", "post"),
    ("/build", "post"),
    ("/builds", "post"),
    ("/builds/{run_id}", "get"),
    ("/builds/{run_id}/manifest", "get"),
    ("/builds/{run_id}/spec", "get"),
    ("/artifacts/{run_id}", "get"),
    ("/builds", "get"),
    ("/datasets", "get"),
    ("/datasets/{dataset_id}", "get"),
    ("/datasets/{dataset_id}/runs", "get"),
    ("/datasets/{dataset_id}/quality/history", "get"),
    ("/builds/{run_id}/stages", "get"),
    ("/builds/{run_id}/stages/{stage}", "get"),
    ("/builds/{run_id}/quality", "get"),
    ("/quality/summary", "get"),
    ("/builds/{run_id}/events", "get"),
    ("/builds/{run_id}/publish/readiness", "get"),
    ("/builds/{run_id}/publish", "post"),
    ("/monitoring/summary", "get"),
    ("/monitoring/builds", "get"),
    ("/uploads", "post"),
    ("/uploads/{upload_id}", "get"),
    ("/uploads/{upload_id}", "delete"),
]


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8")))


def test_contract_file_exists() -> None:
    assert _CONTRACT_PATH.is_file()


def test_is_openapi_3_1_with_info() -> None:
    contract = _load_contract()

    assert str(contract["openapi"]).startswith("3.1")
    assert contract["info"]["title"]
    assert contract["info"]["version"]


def test_covers_all_required_operations() -> None:
    paths = _load_contract()["paths"]

    for path, method in _REQUIRED_OPERATIONS:
        assert path in paths, f"missing path: {path}"
        assert method in paths[path], f"missing {method.upper()} {path}"
        assert paths[path][method].get("operationId"), f"missing operationId for {method} {path}"


def test_operation_ids_are_unique() -> None:
    paths = _load_contract()["paths"]
    operation_ids = [
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]

    assert len(operation_ids) == len(set(operation_ids))


def test_defines_standard_error_schema() -> None:
    schemas = _load_contract()["components"]["schemas"]

    # 실제 구현은 단순한 {"error": "<message>"} 형태를 사용한다(#226).
    assert "Error" in schemas
    assert "error" in schemas["Error"]["properties"]
    assert schemas["Error"]["properties"]["error"]["type"] == "string"


def test_service_api_version_matches_contract() -> None:
    # 코드의 API_CONTRACT_VERSION이 계약 문서의 info.version과 어긋나지 않도록 고정 (#209).
    from kpubdata_builder.service import API_CONTRACT_VERSION

    assert str(_load_contract()["info"]["version"]) == API_CONTRACT_VERSION


def test_query_response_requires_documented_nonnegative_timings() -> None:
    schema = _load_contract()["components"]["schemas"]["QueryResponse"]

    assert {"execution_ms", "startup_ms", "engine_execution_ms"} <= set(schema["required"])
    for field in ("execution_ms", "startup_ms", "engine_execution_ms"):
        assert schema["properties"][field]["type"] == "integer"
        assert schema["properties"][field]["minimum"] == 0
        assert schema["properties"][field]["description"]


def test_main_contract_version_is_stable_semver() -> None:
    """main 계약은 prerelease/build suffix 없는 식별 가능한 stable SemVer다 (#521)."""
    from kpubdata_builder.service import API_CONTRACT_VERSION

    parts = API_CONTRACT_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_build_manifest_does_not_publish_internal_owner_id() -> None:
    """persisted owner_id는 HTTP BuildManifest의 공개 property가 아니다 (#505)."""
    manifest = _load_contract()["components"]["schemas"]["BuildManifest"]

    assert "owner_id" not in manifest["properties"]


def test_credential_get_contract_exposes_only_frozen_metadata() -> None:
    """#492 GET credential wire에는 provider/owner_id/raw secret이 없어야 한다."""
    contract = _load_contract()
    operation = contract["paths"]["/providers/{provider}/credential"]["get"]
    schema_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
    schema = contract["components"]["schemas"][schema_ref.rsplit("/", 1)[-1]]

    assert set(schema["required"]) == {"configured", "masked", "updated_at"}
    assert set(schema["properties"]) == {"configured", "masked", "updated_at"}


def _complete_build_spec_payload() -> dict[str, Any]:
    return {
        "dataset_id": "dataset.contract",
        "title": "Contract fixture",
        "description": "all currently supported BuildSpec fields",
        "metadata": {
            "public": True,
            "tags": ["contract", 485],
            "coverage": {"year": 2026, "note": None},
        },
        "publish": False,
        "sources": [
            {
                "provider": "datago",
                "dataset": "air_quality",
                "params": {"page": 1, "filters": ["seoul", None]},
                "alias": "air",
                "schema": {
                    "required": ["id"],
                    "dtypes": {"id": "string"},
                    "casts": {"value": "float64"},
                },
            }
        ],
        "exports": [
            {
                "kind": "jsonl",
                "output_path": "out/data.jsonl",
                "options": {"pretty": False, "indent": 2},
            }
        ],
        "splits": {"mode": "ratio", "ratios": {"train": 0.8, "test": 0.2}, "seed": 7},
        "pii": {"mode": "warn", "allow_columns": ["contact_hint"]},
        "license": "CC-BY-4.0",
        "quality": {
            "max_duplicate_rate": 0.01,
            "max_duplicate_rate_severity": "warn",
            "max_null_ratio": {"value": 0.05},
            "max_null_ratio_severity": {"value": "fail"},
            "min_rows": 100,
            "min_rows_severity": "fail",
            "range": [{"column": "value", "min": 0, "max": 1000, "severity": "fail"}],
            "compare_columns": [
                {"left": "value", "operator": "gte", "right": "min_value", "severity": "warn"}
            ],
        },
    }


def test_build_spec_schema_matches_current_domain_models() -> None:
    """BuildSpec 선택 필드와 JSON-compatible 값 범위를 OpenAPI에 고정한다 (#485)."""
    contract = _load_contract()
    schemas = contract["components"]["schemas"]
    build_spec = schemas["BuildSpec"]

    assert {
        "publish",
        "splits",
        "pii",
        "license",
        "quality",
    } <= set(build_spec["properties"])
    assert "schema" in schemas["SourceRef"]["properties"]
    assert {"SchemaContract", "SplitSpec", "PiiPolicy", "QualityPolicy"} <= set(schemas)

    payload = _complete_build_spec_payload()
    assert validate(payload, build_spec, contract) == []


def test_removed_build_spec_fields_are_not_declared() -> None:
    """파서가 거부하는 stale 필드를 OpenAPI의 명명된 계약에서 제거한다 (#485)."""
    schemas = _load_contract()["components"]["schemas"]

    assert "transforms" not in schemas["BuildSpec"]["properties"]
    assert [entry["not"]["required"] for entry in schemas["BuildSpec"]["allOf"]] == [
        ["transforms"],
        ["normalization_mode"],
    ]
    assert "normalization_mode" not in schemas["SourceRef"]["properties"]
    assert schemas["SourceRef"]["not"]["required"] == ["normalization_mode"]


@pytest.mark.parametrize("stale_field", ["transforms", "normalization_mode"])
def test_build_spec_contract_rejects_top_level_stale_fields(stale_field: str) -> None:
    """명명된 stale 필드가 실제 payload validation에서 거부되어야 한다 (#485)."""
    contract = _load_contract()
    payload = _complete_build_spec_payload()
    payload[stale_field] = "removed"

    assert validate(payload, contract["components"]["schemas"]["BuildSpec"], contract)


def test_build_spec_contract_rejects_source_normalization_mode() -> None:
    """SourceRef의 제거된 normalization_mode도 실제 payload에서 거부한다 (#485)."""
    contract = _load_contract()
    payload = _complete_build_spec_payload()
    sources = cast(list[dict[str, Any]], payload["sources"])
    sources[0]["normalization_mode"] = "canonical"

    assert validate(payload, contract["components"]["schemas"]["BuildSpec"], contract)


@pytest.mark.parametrize("empty_field", ["sources", "exports"])
def test_build_spec_contract_rejects_empty_required_collections(empty_field: str) -> None:
    """sources/exports는 존재만 해서는 안 되고 한 개 이상의 항목이 필요하다 (#485)."""
    contract = _load_contract()
    payload = deepcopy(_complete_build_spec_payload())
    payload[empty_field] = []

    assert validate(payload, contract["components"]["schemas"]["BuildSpec"], contract)


def test_source_preview_schema_covers_success_and_failure_shape() -> None:
    """preview가 항상 반환하는 error/statistics 필드와 status enum을 고정한다 (#485)."""
    schemas = _load_contract()["components"]["schemas"]
    preview = schemas["SourcePreview"]

    assert {"error", "statistics"} <= set(preview["required"])
    assert preview["properties"]["status"]["enum"] == ["ok", "failed"]
    assert preview["properties"]["statistics"]["$ref"].endswith("/TableStatistics")
    assert set(schemas["TableStatistics"]["required"]) == {
        "row_count",
        "null_counts",
        "duplicate_rate",
    }


def test_source_preview_schema_covers_diff_and_sampling_shape() -> None:
    """#497: source_sample/sample_mode/diff_available/diffs/transform_summary가
    항상 반환되는 필수 필드로 계약에 고정돼 있는지, PreviewDiffItem이 잘못된
    index 비교를 만들지 않는다는 계약(transform nullable)을 지키는지 확인한다."""
    schemas = _load_contract()["components"]["schemas"]
    preview = schemas["SourcePreview"]

    assert {
        "source_sample",
        "sample_mode",
        "diff_available",
        "diffs",
        "transform_summary",
        "diff_truncated",
    } <= set(preview["required"])
    assert preview["properties"]["sample_mode"]["enum"] == ["first", "random"]
    assert preview["properties"]["diff_available"]["type"] == "boolean"
    assert preview["properties"]["diffs"]["items"]["$ref"].endswith("/PreviewDiffItem")
    assert preview["properties"]["diff_truncated"]["type"] == "boolean"

    diff_item = schemas["PreviewDiffItem"]
    assert set(diff_item["required"]) == {"row", "column", "before", "after", "transform"}
    assert diff_item["properties"]["transform"]["type"] == ["string", "null"]

    summary = schemas["PreviewTransformSummary"]
    assert set(summary["required"]) == {"changed_cells", "changed_rows"}


def test_preview_request_schema_declares_bounded_limit_and_sample_mode() -> None:
    """#497: limit 상한(1000, behavioral tightening)과 sample_mode/seed가 계약에 반영됐는지."""
    preview_request = _load_contract()["components"]["schemas"]["PreviewRequest"]

    assert preview_request["properties"]["limit"]["maximum"] == 1000
    assert preview_request["properties"]["sample_mode"]["enum"] == ["first", "random"]
    assert preview_request["properties"]["sample_mode"]["default"] == "first"
    assert preview_request["properties"]["seed"]["type"] == "integer"


# 계약이 기술하는 모든 오퍼레이션은 BuilderService에 실제로 구현돼 있어야 한다.
# 구현 경로 이름은 계약과 1:1로 일치한다(#226: aspirational 비동기/publish 라우트 제거).
_IMPLEMENTED_OPERATIONS = {
    "adminListRuns",
    "adminGetConfig",
    "healthz",
    "getVersion",
    "getCatalog",
    "listProviders",
    "getProviderStatus",
    "testProviderConnection",
    "getProviderCredential",
    "putProviderCredential",
    "deleteProviderCredential",
    "queryBuiltDataset",
    "validateSpec",
    "previewBuild",
    "createBuild",
    "submitBuild",
    "getBuildJob",
    "cancelBuildJob",
    "getBuildManifest",
    "getBuildSpecSnapshot",
    "listBuildArtifacts",
    "listBuilds",
    "listDatasets",
    "getDataset",
    "listDatasetRuns",
    "getDatasetQualityHistory",
    "listBuildStages",
    "getBuildStageDetail",
    "getBuildQuality",
    "getQualitySummary",
    "getBuildEvents",
    "getPublishReadiness",
    "publishBuild",
    "getMonitoringSummary",
    "getMonitoringBuilds",
    "createUpload",
    "getUpload",
    "deleteUpload",
}


def _contract_operation_ids() -> set[str]:
    paths = _load_contract()["paths"]
    return {
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def test_contract_operations_match_implementation() -> None:
    # 계약의 오퍼레이션 집합이 구현된 동기 라우트 집합과 정확히 일치해야 한다.
    # 계약에 미구현 오퍼레이션이 추가되거나 라우트가 사라지면 이 테스트가 깨진다 (#226).
    assert _contract_operation_ids() == _IMPLEMENTED_OPERATIONS


def test_build_responses_pin_wire_status_codes() -> None:
    # POST /build의 실제 상태 코드(200 성공, 502 부분 실패)를 계약이 고정해야 한다 (#226).
    build = _load_contract()["paths"]["/build"]["post"]["responses"]
    assert "200" in build
    assert "502" in build
    assert "400" in build


def test_build_failure_response_includes_error_summary() -> None:
    # 502 응답이 human-readable error 요약을 포함하는 것을 계약 수준에서 고정 (#226).
    schemas = _load_contract()["components"]["schemas"]
    failure = schemas["BuildFailureResponse"]
    assert "error" in failure["properties"]
    assert "error" in failure["required"]


def test_referenced_schemas_resolve() -> None:
    # 모든 로컬 $ref("#/components/...")가 실제로 존재하는지 확인한다.
    contract = _load_contract()

    def _iter_refs(node: object) -> list[str]:
        refs: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.append(value)
                else:
                    refs.extend(_iter_refs(value))
        elif isinstance(node, list):
            for item in node:
                refs.extend(_iter_refs(item))
        return refs

    for ref in _iter_refs(contract):
        assert ref.startswith("#/"), f"unexpected external ref: {ref}"
        target: Any = contract
        for part in ref.lstrip("#/").split("/"):
            assert part in target, f"unresolved $ref: {ref}"
            target = target[part]


def test_stage_detail_contract_has_explicit_wire_schemas() -> None:
    """stage별 핵심 wire field가 additionalProperties 뒤에 숨지 않는다 (#488)."""
    schemas = _load_contract()["components"]["schemas"]
    stage_detail = schemas["StageDetailResponse"]
    refs = {branch["$ref"].rsplit("/", 1)[-1] for branch in stage_detail["oneOf"]}
    assert refs == {
        "BronzeStageDetailResponse",
        "SilverStageDetailResponse",
        "GoldStageDetailResponse",
    }
    assert schemas["BronzeStageDetailResponse"]["additionalProperties"] is False
    assert schemas["SilverStageDetailResponse"]["additionalProperties"] is False
    assert schemas["GoldStageDetailResponse"]["additionalProperties"] is False
    assert {"provider", "dataset", "fetched_at", "record_count"} <= set(
        schemas["BronzeStageDetailResponse"]["properties"]
    )
    assert {"row_count", "schema", "statistics", "validation", "sample"} <= set(
        schemas["SilverStageDetailResponse"]["properties"]
    )
    assert {"row_count", "columns", "splits", "exports", "sample_available"} <= set(
        schemas["GoldStageDetailResponse"]["properties"]
    )


# =============================================================================
# 계약 커버리지 테스트 1단계: 경로/메서드 양방향 검증 (#317)
# =============================================================================


def _extract_yaml_operations() -> dict[tuple[str, str], dict[str, Any]]:
    """YAML에서 (path, method) -> operation 매핑을 추출한다."""
    contract = _load_contract()
    operations: dict[tuple[str, str], dict[str, Any]] = {}

    for path, methods in contract["paths"].items():
        for method_lower, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            method = method_lower.upper()
            key = (path, method)
            operations[key] = operation

    return operations


def _is_planned_operation(operation: dict[str, Any]) -> bool:
    """operation이 x-planned: true로 표시되었는지 확인한다."""
    return operation.get("x-planned") is True


def test_all_yaml_operations_implemented_in_dispatch() -> None:
    """YAML 계약에 정의된 모든 operation이 dispatch에 구현되어 있는지 검증한다(#317).

    x-planned: true로 표시된 operation은 아직 구현되지 않아도 되며, 검증에서 제외한다.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        key = (path, method)
        assert key in _DISPATCH_ROUTES, (
            f"YAML에 정의된 {method} {path}가 dispatch에 구현되어 있지 않습니다. "
            f"operationId: {operation.get('operationId')}"
        )

        yaml_operation_id = operation.get("operationId")
        dispatch_operation_id = _DISPATCH_ROUTES[key]
        assert yaml_operation_id == dispatch_operation_id, (
            f"operationId 불일치: YAML={yaml_operation_id}, dispatch={dispatch_operation_id}"
        )


def test_all_dispatch_routes_declared_in_yaml() -> None:
    """dispatch에 구현된 모든 경로가 YAML 계약에 선언되어 있는지 검증한다(#317).

    실제 구현이 있는데 YAML에 누락된 경우를 탐지한다.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation_id in _DISPATCH_ROUTES.items():
        key = (path, method)
        assert key in yaml_operations, (
            f"dispatch에 구현된 {method} {path}가 YAML 계약에 누락되었습니다. "
            f"operationId: {operation_id}"
        )

        yaml_op = yaml_operations[key]
        yaml_operation_id = yaml_op.get("operationId")
        assert yaml_operation_id == operation_id, (
            f"operationId 불일치: dispatch={operation_id}, YAML={yaml_operation_id}"
        )


def test_planned_operations_excluded_from_implementation_check() -> None:
    """x-planned: true operation이 구현 검증에서 제외되는지 확인한다(#317).

    계약에는 planned operation이 포함될 수 있지만, 실제 dispatch에는
    구현되지 않아도 된다. 이 규칙이 올바르게 동작하는지 검증한다.
    """
    yaml_operations = _extract_yaml_operations()

    # 현재 계약에는 x-planned operation이 없으므로, 모든 operation이 구현되어야 한다
    planned_operations = [
        (path, method, op.get("operationId"))
        for (path, method), op in yaml_operations.items()
        if _is_planned_operation(op)
    ]

    # 향후 planned operation이 추가되면 이 테스트가 그 존재를 검증한다
    # 현재는 계약에 planned operation이 없음을 확인
    for path, method, operation_id in planned_operations:
        assert operation_id, f"planned operation {method} {path}에 operationId가 없습니다"

    # 모든 planned operation은 dispatch에 없어도 됨
    for path, method, _operation_id in planned_operations:
        key = (path, method)
        if key in _DISPATCH_ROUTES:
            # planned인데 구현되어 있어도 에러는 아님 (구현이 빠른 경우)
            pass
        else:
            # planned이고 구현되지 않아도 정상
            pass


# =============================================================================
# 계약 커버리지 테스트 2단계: 상태 코드와 응답 스키마 검증 (#319)
# =============================================================================

# 각 operation이 YAML에 선언한 상태 코드와 실제 구현이 반환하는 상태 코드의
# 매핑. service/app.py:dispatch와 각 service 메서드를 분석하여 작성한다.
_OPERATION_STATUS_CODES: dict[str, set[int]] = {
    # 관리 엔드포인트(#679). 404가 없다 — 경로가 고정이고 run을 개별 조회하지
    # 않는다. 400도 없다 — limit은 거부하지 않고 clamp한다.
    "adminListRuns": {200, 403, 503},
    "adminGetConfig": {200, 403},
    "healthz": {200},
    "getVersion": {200},
    "getCatalog": {200, 502},
    "listProviders": {200, 403, 502},
    "getProviderStatus": {200, 403, 404, 502},
    "testProviderConnection": {200, 403, 404, 502},
    "getProviderCredential": {200, 403, 404, 502, 503},
    "putProviderCredential": {200, 400, 403, 404, 502, 503},
    "deleteProviderCredential": {200, 403, 404, 502, 503},
    "queryBuiltDataset": {200, 400, 403, 404, 429, 504},
    "validateSpec": {200, 400},
    "previewBuild": {200, 400, 502},
    "createBuild": {200, 400, 502},
    "submitBuild": {200, 202, 400, 409, 429, 500},
    "getBuildJob": {200, 400, 403, 404},
    "cancelBuildJob": {200, 400, 403, 404, 409},
    "getBuildManifest": {200, 400, 404, 500},
    "getBuildSpecSnapshot": {200, 400, 403, 404, 500},
    "listBuilds": {200, 400},
    "listBuildArtifacts": {200, 400, 404},
    "listDatasets": {200, 400},
    "getDataset": {200, 400, 404},
    "listDatasetRuns": {200, 400, 404},
    "getDatasetQualityHistory": {200, 400, 404},
    "listBuildStages": {200, 400, 403, 404},
    "getBuildStageDetail": {200, 400, 403, 404},
    "getBuildQuality": {200, 400, 403, 404},
    "getQualitySummary": {200, 400},
    "getBuildEvents": {200, 400, 403, 404},
    "getPublishReadiness": {200, 400, 403, 404},
    "publishBuild": {200, 400, 403, 404, 409, 502},
    "getMonitoringSummary": {200},
    "getMonitoringBuilds": {200, 400},
    "createUpload": {200, 400, 403, 413},
    "getUpload": {200, 403, 404},
    "deleteUpload": {200, 403, 404},
}


def _extract_declared_status_codes(operation: dict[str, Any]) -> set[int]:
    """YAML operation에서 선언된 상태 코드들을 추출한다."""
    responses = operation.get("responses", {})
    declared_codes: set[int] = set()

    for status_code_str in responses:
        if status_code_str == "default":
            continue
        try:
            declared_codes.add(int(status_code_str))
        except ValueError:
            # "default" 또는 기타 비숫자 상태 코드는 무시
            continue

    return declared_codes


def test_declared_status_codes_match_implementation() -> None:
    """YAML에 선언된 상태 코드와 실제 구현이 일치하는지 검증한다(#319).

    각 operation마다 YAML의 responses 키에 선언된 상태 코드와 실제 코드가
    반환할 수 있는 상태 코드가 일치해야 한다.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        operation_id = operation.get("operationId")
        if not operation_id:
            continue

        # YAML에 선언된 상태 코드 추출
        declared_codes = _extract_declared_status_codes(operation)

        # 401은 인증 게이트 안의 엔드포인트에 공통. 단 security: [] (무인증, #372)는 제외.
        is_unauthenticated = operation.get("security") == []
        implemented_codes = _OPERATION_STATUS_CODES.get(operation_id, set())
        all_implemented_codes = implemented_codes | (set() if is_unauthenticated else {401})

        assert declared_codes == all_implemented_codes, (
            f"상태 코드 불일치: {method} {path} (operationId: {operation_id})\n"
            f"  YAML 선언: {sorted(declared_codes)}\n"
            f"  실제 구현: {sorted(all_implemented_codes)}\n"
            f"  차이: {sorted(declared_codes ^ all_implemented_codes)}"
        )


def test_publish_request_contract_matches_huggingface_runtime() -> None:
    """#491 HTTP target/options와 실제 fail-closed runtime 계약을 고정한다."""
    contract = _load_contract()
    schemas = contract["components"]["schemas"]
    assert schemas["PublishTarget"]["enum"] == ["huggingface"]
    assert schemas["PublishRequest"]["additionalProperties"] is False
    hf_options = schemas["PublishHuggingFaceOptions"]
    assert hf_options["additionalProperties"] is False
    assert hf_options["properties"]["private"] == {
        "type": "boolean",
        "default": True,
    }

    operation = contract["paths"]["/builds/{run_id}/publish"]["post"]
    example = operation["requestBody"]["content"]["application/json"]["examples"]["HuggingFace"][
        "value"
    ]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert validate(example, schema, contract) == []
    assert validate(
        {
            "target": "huggingface",
            "destination": "owner/dataset",
            "options": {"visibility": "private"},
        },
        schema,
        contract,
    )


def _extract_response_schema_required_fields(
    contract: dict[str, Any], response_ref: str
) -> set[str]:
    """YAML 응답 스키마에서 required 필드들을 추출한다."""
    # $ref 형식: "#/components/schemas/SchemaName"
    if not response_ref.startswith("#/components/schemas/"):
        return set()

    schema_name = response_ref[len("#/components/schemas/") :]
    schema = contract["components"]["schemas"].get(schema_name, {})
    required = schema.get("required", [])

    if isinstance(required, list):
        return set(required)
    return set()


def test_response_schemas_have_required_fields() -> None:
    """YAML 응답 스키마가 주요 엔드포인트의 200 응답에 대한 필수 필드를
    정의하고 있는지 검증한다(#319).

    이 테스트는 계약 자체의 완전성을 확인한다: 각 operation이 성공 응답(200)에
    대한 스키마를 정의하고, 그 스키마에 required 필드들이 포함되어 있는지 확인.
    """
    contract = _load_contract()
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        operation_id = operation.get("operationId")
        if not operation_id:
            continue

        # 200 응답 확인
        responses = operation.get("responses", {})
        if "200" not in responses:
            raise AssertionError(
                f"{method} {path} (operationId: {operation_id})에 200 응답이 누락되었습니다"
            )

        response_200 = responses["200"]
        content = response_200.get("content", {})
        json_content = content.get("application/json", {})
        schema_ref = json_content.get("schema", {}).get("$ref", "")

        # $ref가 있으면 required 필드 확인
        if schema_ref:
            required_fields = _extract_response_schema_required_fields(contract, schema_ref)

            assert required_fields, (
                f"{method} {path} (operationId: {operation_id})의 200 응답 스키마에 "
                f"required 필드가 정의되어 있지 않습니다: {schema_ref}"
            )

            # 주요 엔드포인트들은 최소한 몇 개의 필수 필드를 가져야 함
            main_operations = {
                "getVersion",
                "validateSpec",
                "previewBuild",
                "createBuild",
                "listBuildArtifacts",
            }
            if operation_id in main_operations:
                assert len(required_fields) >= 1, (
                    f"{method} {path} (operationId: {operation_id})의 "
                    f"200 응답 스키마에 필수 필드가 너무 적습니다: {required_fields}"
                )


# ---------------------------------------------------------------------------
# 런타임 wire-level conformance (#209, ADR-0005)
#
# 정적 검증(위)은 계약 YAML과 dispatch 라우팅 목록이 일치하는지 본다. 아래 테스트들은
# 실제 dispatch()를 호출해 반환된 JSON 본문이 선언된 응답 스키마에 부합하는지 검증한다.
# 외부 의존 없이 순수 파이썬 validator(_openapi.py)를 쓰며, 이것이 ADR-0005 미해결 질문
# #1(스키마 대조를 순수 파이썬 경량으로 할지)을 "순수 파이썬 경량" 방향으로 마무리한다.
#
# #319의 test_response_schemas_have_required_fields는 스키마가 required를 *선언했는지*만
# 본다. app.py가 실제 응답에서 필수 필드를 빼거나 타입을 바꿔도 선언부가 그대로면 그 정적
# 검사는 통과한다 — 이 런타임 검사가 그 wire 드리프트를 잡는다. 계약에 선언된 6개 오퍼레이션
# 모두(/version, /validate, /preview, /build, /artifacts, /builds)를 성공+오류 상태 코드에
# 걸쳐 검증한다.
# ---------------------------------------------------------------------------

_CONFORM_SPEC_YAML = (
    "dataset_id: dataset.conform\n"
    "title: Conform Sample\n"
    "description: runtime conformance fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
)

# fetch를 실패시켜 502 경로를 만들기 위한 spec: fake client가 모르는 소스.
_FAILING_SPEC_YAML = _CONFORM_SPEC_YAML.replace("dataset: air_quality\n", "dataset: missing\n")

# 파싱은 통과하지만 validate_spec에서 미지원 exporter kind로 실패하는 spec.
_INVALID_SPEC_YAML = _CONFORM_SPEC_YAML.replace("kind: jsonl", "kind: unsupported_format")


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


def _conform_service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def _assert_conforms(resp: ServiceResponse, path: str, method: str) -> None:
    """실제 dispatch 응답이 계약 스키마에 부합하는지(상태 코드 선언 + 본문 형태) 검증."""
    contract = _load_contract()
    schema = response_schema(contract, path, method, resp.status_code)
    assert schema is not None, (
        f"{method} {path}: status {resp.status_code} is not declared in the contract"
    )
    errors = validate(resp.body, schema, contract)
    assert not errors, (
        f"{method} {path} {resp.status_code} response drifts from contract:\n  "
        + "\n  ".join(errors)
    )


class TestResponseConformance:
    """선언된 각 오퍼레이션의 실제 wire 응답이 OpenAPI 스키마에 부합하는지 고정."""

    def test_version_200(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/version", "GET")

    def test_validate_200(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/validate", {"spec": _CONFORM_SPEC_YAML}
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/validate", "POST")

    def test_validate_400_invalid_spec(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/validate", {"spec": _INVALID_SPEC_YAML}
        )
        assert resp.status_code == 400
        # 미지원 exporter → {"status": "invalid", "problems": [...]}
        _assert_conforms(resp, "/validate", "POST")

    def test_preview_200(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 2}
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_200_source_failure(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/preview",
            {"spec": _FAILING_SPEC_YAML, "limit": 2},
        )
        assert resp.status_code == 200
        previews = cast(list[dict[str, JsonValue]], resp.body["previews"])
        assert previews[0]["status"] == "failed"
        assert previews[0]["error"]
        assert previews[0]["schema"] == []
        assert previews[0]["sample"] == []
        assert previews[0]["statistics"] == {
            "row_count": 0,
            "null_counts": {},
            "duplicate_rate": 0.0,
        }
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_400_bad_limit(self, tmp_path: Path) -> None:
        # 400 응답은 oneOf(Error | ValidationError) — limit 오류는 Error 형태.
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 0}
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_400_limit_above_max(self, tmp_path: Path) -> None:
        # #497: limit 상한(1000) 신규 도입 — behavioral tightening.
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/preview",
            {"spec": _CONFORM_SPEC_YAML, "limit": 1001},
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_200_random_sample_mode_with_diff(self, tmp_path: Path) -> None:
        # #497: sample_mode=random + diff가 실제 발생하는 응답도 계약을 만족하는지.
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/preview",
            {"spec": _CONFORM_SPEC_YAML, "limit": 2, "sample_mode": "random", "seed": 1},
        )
        assert resp.status_code == 200
        previews = cast(list[dict[str, JsonValue]], resp.body["previews"])
        assert previews[0]["sample_mode"] == "random"
        assert previews[0]["diff_available"] is True
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_400_bad_sample_mode(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/preview",
            {"spec": _CONFORM_SPEC_YAML, "sample_mode": "shuffle"},
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_200_wide_dataset_diff_truncated(self, tmp_path: Path) -> None:
        # #497 sample/diff memory 상한: diffs가 실제로 잘려도(diff_truncated=true)
        # 응답이 여전히 계약을 만족하는지 wire-level로 확인한다.
        from kpubdata_builder.pipeline import MAX_PREVIEW_DIFF_ITEMS

        columns = [f"c{i}" for i in range(MAX_PREVIEW_DIFF_ITEMS + 10)]
        wide_spec_yaml = (
            "dataset_id: dataset.wide\n"
            "title: Wide Conform\n"
            "description: wide dataset conformance fixture\n"
            "sources:\n"
            "  - provider: datago\n"
            "    dataset: air_quality\n"
            "    schema:\n"
            "      casts:\n" + "".join(f"        {c}: int\n" for c in columns) + "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        client = _FakeClient({"datago.air_quality": [dict.fromkeys(columns, "1")]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = dispatch(service, "POST", "/preview", {"spec": wide_spec_yaml, "limit": 1})

        assert resp.status_code == 200
        previews = cast(list[dict[str, JsonValue]], resp.body["previews"])
        assert len(cast(list[object], previews[0]["diffs"])) == MAX_PREVIEW_DIFF_ITEMS
        assert previews[0]["diff_truncated"] is True
        _assert_conforms(resp, "/preview", "POST")

    def test_build_200_success(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/build",
            {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ok"},
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/build", "POST")

    def test_build_502_failure(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/build",
            {"spec": _FAILING_SPEC_YAML, "run_id": "conform-fail"},
        )
        assert resp.status_code == 502
        _assert_conforms(resp, "/build", "POST")

    def test_build_400_bad_run_id(self, tmp_path: Path) -> None:
        # 400 응답은 oneOf(Error | ValidationError) — run_id 타입 오류는 Error 형태.
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/build",
            {"spec": _CONFORM_SPEC_YAML, "run_id": 123},
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/build", "POST")

    def test_artifacts_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-art"})
        resp = dispatch(service, "GET", "/artifacts/conform-art", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/artifacts/{run_id}", "GET")

    def test_artifacts_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/artifacts/nope", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/artifacts/{run_id}", "GET")

    def test_builds_200_empty(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds", None, query="")
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds", "GET")

    def test_builds_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-list"})
        resp = dispatch(service, "GET", "/builds", None, query="")
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds", "GET")

    def test_manifest_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-man"})
        resp = dispatch(service, "GET", "/builds/conform-man/manifest", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/manifest", "GET")

    def test_manifest_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds/nope/manifest", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/manifest", "GET")

    def test_spec_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "spec-ok"})
        resp = dispatch(service, "GET", "/builds/spec-ok/spec", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/spec", "GET")

    def test_spec_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds/nope/spec", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/spec", "GET")

    def test_builds_400_bad_limit(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds", None, query="limit=0")
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds", "GET")

    # -------------------------------------------------------------------
    # Dataset/Stage API conformance (#488)
    # -------------------------------------------------------------------

    def test_datasets_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ds"})
        resp = dispatch(service, "GET", "/datasets", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/datasets", "GET")

    def test_datasets_400_bad_limit(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/datasets", None, query="limit=0")
        assert resp.status_code == 400
        _assert_conforms(resp, "/datasets", "GET")

    def test_dataset_detail_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ds-detail"}
        )
        resp = dispatch(service, "GET", "/datasets/dataset.conform", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/datasets/{dataset_id}", "GET")

    def test_dataset_detail_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/datasets/nope", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/datasets/{dataset_id}", "GET")

    def test_dataset_runs_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ds-runs"}
        )
        resp = dispatch(service, "GET", "/datasets/dataset.conform/runs", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/datasets/{dataset_id}/runs", "GET")

    def test_dataset_runs_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/datasets/nope/runs", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/datasets/{dataset_id}/runs", "GET")

    def test_build_stages_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-stg"})
        resp = dispatch(service, "GET", "/builds/conform-stg/stages", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/stages", "GET")

    def test_build_stages_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds/nope/stages", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/stages", "GET")

    @pytest.mark.parametrize("stage", ["bronze", "silver", "gold"])
    def test_build_stage_detail_200(self, tmp_path: Path, stage: str) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-stg-detail"}
        )
        resp = dispatch(
            service,
            "GET",
            f"/builds/conform-stg-detail/stages/{stage}",
            None,
            query="source=datago.air_quality",
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/stages/{stage}", "GET")

    def test_build_stage_detail_400_missing_source(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-stg-400"}
        )
        resp = dispatch(service, "GET", "/builds/conform-stg-400/stages/bronze", None)
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds/{run_id}/stages/{stage}", "GET")

    def test_build_stage_detail_404_unknown_source(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-stg-404"}
        )
        resp = dispatch(
            service,
            "GET",
            "/builds/conform-stg-404/stages/bronze",
            None,
            query="source=nope",
        )
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/stages/{stage}", "GET")

    # -------------------------------------------------------------------
    # Quality History/Detail API conformance (#486)
    # -------------------------------------------------------------------

    def test_dataset_quality_history_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-qh"})
        resp = dispatch(service, "GET", "/datasets/dataset.conform/quality/history", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/datasets/{dataset_id}/quality/history", "GET")

    def test_dataset_quality_history_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/datasets/nope/quality/history", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/datasets/{dataset_id}/quality/history", "GET")

    def test_dataset_quality_history_400_bad_limit(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "GET",
            "/datasets/dataset.conform/quality/history",
            None,
            query="limit=0",
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/datasets/{dataset_id}/quality/history", "GET")

    def test_build_quality_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-bq"})
        resp = dispatch(service, "GET", "/builds/conform-bq/quality", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/quality", "GET")

    def test_build_quality_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds/nope/quality", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/quality", "GET")

    # -------------------------------------------------------------------
    # Run Event Timeline API conformance (#496)
    # -------------------------------------------------------------------

    def test_build_events_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ev"})
        resp = dispatch(service, "GET", "/builds/conform-ev/events", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/events", "GET")

    def test_build_events_200_with_limit_and_tail(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ev-tail"}
        )
        resp = dispatch(
            service,
            "GET",
            "/builds/conform-ev-tail/events",
            None,
            query="limit=2&tail=true",
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/events", "GET")

    def test_build_events_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds/nope/events", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/events", "GET")

    def test_build_events_400_bad_limit(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ev-400"}
        )
        resp = dispatch(service, "GET", "/builds/conform-ev-400/events", None, query="limit=0")
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds/{run_id}/events", "GET")

    def test_build_events_400_bad_tail(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(
            service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ev-tail-400"}
        )
        resp = dispatch(
            service, "GET", "/builds/conform-ev-tail-400/events", None, query="tail=yes"
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds/{run_id}/events", "GET")

    def test_monitoring_summary_200(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/monitoring/summary", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/monitoring/summary", "GET")

    def test_monitoring_summary_200_after_build(self, tmp_path: Path) -> None:
        # 요청 처리 후 latency 표본이 기록되어도 계약을 벗어나지 않는지 확인.
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-mon"})
        resp = dispatch(service, "GET", "/monitoring/summary", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/monitoring/summary", "GET")
        assert resp.body["api"]["sample_count"] >= 1  # type: ignore[index]

    def test_monitoring_builds_200_empty(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/monitoring/builds", None, query="")
        assert resp.status_code == 200
        _assert_conforms(resp, "/monitoring/builds", "GET")

    def test_monitoring_builds_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-mon-b"})
        resp = dispatch(service, "GET", "/monitoring/builds", None, query="window=24h&bucket=hour")
        assert resp.status_code == 200
        _assert_conforms(resp, "/monitoring/builds", "GET")

    def test_monitoring_builds_400_bad_window(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "GET", "/monitoring/builds", None, query="window=7d"
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/monitoring/builds", "GET")

    def test_monitoring_builds_400_bad_bucket(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "GET", "/monitoring/builds", None, query="bucket=day"
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/monitoring/builds", "GET")

    def test_create_upload_200(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/uploads",
            None,
            query="format=csv&filename=trades.csv",
            raw_body=b"a,b\n1,2\n",
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/uploads", "POST")

    def test_create_upload_400_missing_format(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/uploads", None, raw_body=b"a,b\n1,2\n"
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/uploads", "POST")

    def test_get_upload_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        created = dispatch(
            service, "POST", "/uploads", None, query="format=csv", raw_body=b"a,b\n1,2\n"
        )
        upload_id = cast(str, created.body["upload_id"])
        resp = dispatch(service, "GET", f"/uploads/{upload_id}", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/uploads/{upload_id}", "GET")

    def test_get_upload_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", f"/uploads/upl_{'0' * 32}", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/uploads/{upload_id}", "GET")

    def test_delete_upload_200(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        created = dispatch(
            service, "POST", "/uploads", None, query="format=csv", raw_body=b"a,b\n1,2\n"
        )
        upload_id = cast(str, created.body["upload_id"])
        resp = dispatch(service, "DELETE", f"/uploads/{upload_id}", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/uploads/{upload_id}", "DELETE")

    def test_delete_upload_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "DELETE", f"/uploads/upl_{'0' * 32}", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/uploads/{upload_id}", "DELETE")


class TestBuildSpecSchemaContractIsPublished:
    """BuildSpec 선언 모델이 공개 OpenAPI 계약과 함께 움직인다 (#611).

    ``contract/builder-api.yaml``은 Studio와 타입 생성기가 읽는 HTTP 계약이다.
    파이썬 모델에만 필드를 더하면 손으로 쓴 YAML에서는 동작하지만 계약 소비자는
    그 기능을 발견하지도, 타이핑하지도 못한다. 드리프트를 테스트로 고정한다.
    """

    @staticmethod
    def _schemas() -> dict[str, Any]:
        return cast(dict[str, Any], _load_contract()["components"]["schemas"])

    def test_schema_contract_properties_cover_the_dataclass(self) -> None:
        from dataclasses import fields

        from kpubdata_builder.spec import SchemaContract

        published = set(self._schemas()["SchemaContract"]["properties"])
        declared = {f.name for f in fields(SchemaContract)}
        assert declared <= published, f"OpenAPI에 없는 SchemaContract 필드: {declared - published}"

    def test_derived_column_properties_cover_the_dataclass(self) -> None:
        from dataclasses import fields

        from kpubdata_builder.spec import DerivedColumn

        published = set(self._schemas()["DerivedColumn"]["properties"])
        declared = {f.name for f in fields(DerivedColumn)}
        assert declared <= published, f"OpenAPI에 없는 DerivedColumn 필드: {declared - published}"

    def test_derived_column_kind_enum_matches_the_supported_kinds(self) -> None:
        from kpubdata_builder.spec.models import DERIVED_KINDS

        published = self._schemas()["DerivedColumn"]["properties"]["kind"]["enum"]
        assert sorted(published) == sorted(DERIVED_KINDS)

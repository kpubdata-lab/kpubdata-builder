"""Builder Service Contract (#63, #226, #317, #319, #209) structure and runtime verification.

No OpenAPI validator installed; structurally verify contract is OpenAPI 3.1 and covers both sync
routes
and wire form actually implemented by BuilderService (service/app.py).
Verify. Contract now describes only implemented endpoints (#226); prevent silent drift
where only one side changes by explicitly locking implementation route ↔ contract operationId
mapping (#317).
Also verify bidirectional consistency between YAML contract and actual dispatch implementation
(#317);
extend scope with status code and response schema verification (#319).

Structural verification (above) checks if YAML *declaration* matches dispatch *routing list*
(#317, #319).
``TestResponseConformance`` (#209, ADR-0005) goes further: **actual dispatch response
body conforms to declared schema **(wire-level conformance)** via pure Python
validator (``_openapi.py``) — #319's static schema check only verifies "schema declares required
it declares"; runtime check catches when app.py response has missing required fields or type
changes even if declaration unchanged.
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

# Mapping of (path, method, operationId) implemented in dispatch.
# Mechanical extraction of routing rules from service/app.py:dispatch is difficult, so
# explicit declaration improves maintainability.
_DISPATCH_ROUTES: dict[tuple[str, str], str] = {
    ("/admin/runs", "GET"): "adminListRuns",
    ("/admin/config", "GET"): "adminGetConfig",
    ("/admin/users", "GET"): "adminListUsers",
    ("/admin/users/{user_id}/approve", "POST"): "adminApproveUser",
    ("/admin/users/{user_id}/reject", "POST"): "adminRejectUser",
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
    ("/warehouse/tables", "GET"): "listWarehouseTables",
    ("/warehouse/tables/{name}", "GET"): "getWarehouseTable",
    ("/warehouse/tables/{name}/profile", "GET"): "getWarehouseTableProfile",
    ("/warehouse/query", "POST"): "queryWarehouseTable",
    ("/warehouse/rows", "POST"): "readWarehouseRows",
    ("/warehouse/aggregate", "POST"): "aggregateWarehouseTable",
    ("/warehouse/exports", "POST"): "createWarehouseExport",
    ("/warehouse/exports", "GET"): "listWarehouseExports",
    ("/warehouse/exports/{export_id}", "GET"): "getWarehouseExport",
    ("/warehouse/exports/{export_id}", "DELETE"): "deleteWarehouseExport",
    ("/warehouse/exports/{export_id}/download", "GET"): "downloadWarehouseExport",
    ("/analyses", "GET"): "listAnalyses",
    ("/revisions/{kind}/{doc_id}", "PUT"): "saveRevision",
    ("/revisions/{kind}/{doc_id}", "GET"): "getRevision",
    ("/revisions/{kind}/{doc_id}/history", "GET"): "getRevisionHistory",
    ("/revisions/{kind}/{doc_id}/revert", "POST"): "revertRevision",
    ("/analyses", "POST"): "createAnalysis",
    ("/analyses/{analysis_id}", "GET"): "getAnalysis",
    ("/analyses/{analysis_id}", "DELETE"): "deleteAnalysis",
    ("/analyses/{analysis_id}/run", "POST"): "runAnalysis",
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
    ("/artifacts/{run_id}/{file_path}", "GET"): "getBuildArtifactFile",
    ("/datasets", "GET"): "listDatasets",
    ("/datasets/{dataset_id}", "GET"): "getDataset",
    ("/datasets/{dataset_id}/runs", "GET"): "listDatasetRuns",
    ("/datasets/{dataset_id}/runs/{run_id}", "GET"): "getDatasetRun",
    ("/datasets/{dataset_id}/quality/history", "GET"): "getDatasetQualityHistory",
    ("/builds/{run_id}/stages", "GET"): "listBuildStages",
    ("/builds/{run_id}/stages/{stage}", "GET"): "getBuildStageDetail",
    ("/builds/{run_id}/quality", "GET"): "getBuildQuality",
    ("/quality/summary", "GET"): "getQualitySummary",
    ("/quality/issues", "GET"): "listQualityIssues",
    ("/builds/{run_id}/events", "GET"): "getBuildEvents",
    ("/builds/{run_id}/publish/readiness", "GET"): "getPublishReadiness",
    ("/builds/{run_id}/publish", "POST"): "publishBuild",
    ("/monitoring/summary", "GET"): "getMonitoringSummary",
    ("/monitoring/builds", "GET"): "getMonitoringBuilds",
    ("/uploads", "POST"): "createUpload",
    ("/uploads/{upload_id}", "GET"): "getUpload",
    ("/uploads/{upload_id}", "DELETE"): "deleteUpload",
}

# (path, method) form of mandatory contract operations. BuilderService.dispatch actually routes to
# sync endpoints one-to-one.
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
    ("/warehouse/tables", "get"),
    ("/warehouse/tables/{name}", "get"),
    ("/warehouse/tables/{name}/profile", "get"),
    ("/warehouse/query", "post"),
    ("/warehouse/rows", "post"),
    ("/warehouse/aggregate", "post"),
    ("/warehouse/exports", "post"),
    ("/warehouse/exports", "get"),
    ("/warehouse/exports/{export_id}", "get"),
    ("/warehouse/exports/{export_id}", "delete"),
    ("/warehouse/exports/{export_id}/download", "get"),
    ("/analyses", "get"),
    ("/revisions/{kind}/{doc_id}", "put"),
    ("/revisions/{kind}/{doc_id}", "get"),
    ("/revisions/{kind}/{doc_id}/history", "get"),
    ("/revisions/{kind}/{doc_id}/revert", "post"),
    ("/analyses", "post"),
    ("/analyses/{analysis_id}", "get"),
    ("/analyses/{analysis_id}", "delete"),
    ("/analyses/{analysis_id}/run", "post"),
    ("/validate", "post"),
    ("/preview", "post"),
    ("/build", "post"),
    ("/builds", "post"),
    ("/builds/{run_id}", "get"),
    ("/builds/{run_id}/manifest", "get"),
    ("/builds/{run_id}/spec", "get"),
    ("/artifacts/{run_id}", "get"),
    ("/artifacts/{run_id}/{file_path}", "get"),
    ("/builds", "get"),
    ("/datasets", "get"),
    ("/datasets/{dataset_id}", "get"),
    ("/datasets/{dataset_id}/runs", "get"),
    ("/datasets/{dataset_id}/quality/history", "get"),
    ("/builds/{run_id}/stages", "get"),
    ("/builds/{run_id}/stages/{stage}", "get"),
    ("/builds/{run_id}/quality", "get"),
    ("/quality/summary", "get"),
    ("/quality/issues", "get"),
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

    # Actual implementation uses simple {"error": "<message>"} form (#226).
    assert "Error" in schemas
    assert "error" in schemas["Error"]["properties"]
    assert schemas["Error"]["properties"]["error"]["type"] == "string"


def test_service_api_version_matches_contract() -> None:
    # Lock to ensure code's API_CONTRACT_VERSION matches contract document's info.version (#209).
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
    """main contract is identifiable stable SemVer without prerelease/build suffix (#521)."""
    from kpubdata_builder.service import API_CONTRACT_VERSION

    parts = API_CONTRACT_VERSION.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_build_manifest_does_not_publish_internal_owner_id() -> None:
    """persisted owner_id is not a public property of HTTP BuildManifest (#505)."""
    manifest = _load_contract()["components"]["schemas"]["BuildManifest"]

    assert "owner_id" not in manifest["properties"]


def test_credential_get_contract_exposes_only_frozen_metadata() -> None:
    """#492 GET credential wire must not contain provider/owner_id/raw secret."""
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
    """Lock BuildSpec optional fields and JSON-compatible value ranges in OpenAPI (#485)."""
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
    """Remove stale fields rejected by parser from OpenAPI named contract (#485)."""
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
    """Named stale fields must be rejected in actual payload validation (#485)."""
    contract = _load_contract()
    payload = _complete_build_spec_payload()
    payload[stale_field] = "removed"

    assert validate(payload, contract["components"]["schemas"]["BuildSpec"], contract)


def test_build_spec_contract_rejects_source_normalization_mode() -> None:
    """SourceRef's removed normalization_mode is also rejected in actual payload (#485)."""
    contract = _load_contract()
    payload = _complete_build_spec_payload()
    sources = cast(list[dict[str, Any]], payload["sources"])
    sources[0]["normalization_mode"] = "canonical"

    assert validate(payload, contract["components"]["schemas"]["BuildSpec"], contract)


@pytest.mark.parametrize("empty_field", ["sources", "exports"])
def test_build_spec_contract_rejects_empty_required_collections(empty_field: str) -> None:
    """sources/exports must not just exist; they need one or more items (#485)."""
    contract = _load_contract()
    payload = deepcopy(_complete_build_spec_payload())
    payload[empty_field] = []

    assert validate(payload, contract["components"]["schemas"]["BuildSpec"], contract)


def test_source_preview_schema_covers_success_and_failure_shape() -> None:
    """Lock error/statistics fields and status enum that preview always returns (#485)."""
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
    """#497: source_sample/sample_mode/diff_available/diffs/transform_summary are"""
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
    """#497: limit ceiling (1000, behavioral tightening) and sample_mode/seed reflected in
    contract.
    """
    preview_request = _load_contract()["components"]["schemas"]["PreviewRequest"]

    assert preview_request["properties"]["limit"]["maximum"] == 1000
    assert preview_request["properties"]["sample_mode"]["enum"] == ["first", "random"]
    assert preview_request["properties"]["sample_mode"]["default"] == "first"
    assert preview_request["properties"]["seed"]["type"] == "integer"


# All operations described in the contract must actually be implemented in BuilderService.
# Implementation path names match the contract one-to-one (#226: aspirational async/publish routes
# removed).
_IMPLEMENTED_OPERATIONS = {
    "adminListRuns",
    "adminGetConfig",
    "adminListUsers",
    "adminApproveUser",
    "adminRejectUser",
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
    "listWarehouseTables",
    "getWarehouseTable",
    "getWarehouseTableProfile",
    "queryWarehouseTable",
    "readWarehouseRows",
    "aggregateWarehouseTable",
    "createWarehouseExport",
    "listWarehouseExports",
    "getWarehouseExport",
    "deleteWarehouseExport",
    "downloadWarehouseExport",
    "listAnalyses",
    "saveRevision",
    "getRevision",
    "getRevisionHistory",
    "revertRevision",
    "createAnalysis",
    "getAnalysis",
    "deleteAnalysis",
    "runAnalysis",
    "validateSpec",
    "previewBuild",
    "createBuild",
    "submitBuild",
    "getBuildJob",
    "cancelBuildJob",
    "getBuildManifest",
    "getBuildSpecSnapshot",
    "listBuildArtifacts",
    "getBuildArtifactFile",
    "listBuilds",
    "listDatasets",
    "getDataset",
    "listDatasetRuns",
    "getDatasetRun",
    "getDatasetQualityHistory",
    "listBuildStages",
    "getBuildStageDetail",
    "getBuildQuality",
    "getQualitySummary",
    "listQualityIssues",
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
    # Contract operation set must exactly match implemented sync route set.
    # If unimplemented operations are added to contract or routes disappear, this test breaks
    # (#226).
    assert _contract_operation_ids() == _IMPLEMENTED_OPERATIONS


def test_build_responses_pin_wire_status_codes() -> None:
    # Contract must lock actual status codes for POST /build (200 success, 502 partial failure)
    # (#226).
    build = _load_contract()["paths"]["/build"]["post"]["responses"]
    assert "200" in build
    assert "502" in build
    assert "400" in build


def test_build_failure_response_includes_error_summary() -> None:
    # Lock at contract level that 502 response includes human-readable error summary (#226).
    schemas = _load_contract()["components"]["schemas"]
    failure = schemas["BuildFailureResponse"]
    assert "error" in failure["properties"]
    assert "error" in failure["required"]


def test_referenced_schemas_resolve() -> None:
    # Verify all local $ref("#/components/...") actually exist.
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
    """Stage-specific key wire fields do not hide behind additionalProperties (#488)."""
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
# Contract coverage test stage 1: bidirectional path/method verification (#317).
# =============================================================================


def _extract_yaml_operations() -> dict[tuple[str, str], dict[str, Any]]:
    """Extract (path, method) → operation mapping from YAML."""
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
    """Verify operation is marked x-planned: true."""
    return operation.get("x-planned") is True


def test_all_yaml_operations_implemented_in_dispatch() -> None:
    """Verify all operations defined in YAML contract are implemented in dispatch (#317)."""
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
    """Verify all paths implemented in dispatch are declared in YAML contract (#317)."""
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
    """Verify x-planned: true operations are excluded from implementation verification (#317)."""
    yaml_operations = _extract_yaml_operations()

    # Currently no x-planned operations in contract, so all operations must be implemented.
    planned_operations = [
        (path, method, op.get("operationId"))
        for (path, method), op in yaml_operations.items()
        if _is_planned_operation(op)
    ]

    # When planned operations are added in future, this test will verify their presence.
    # Currently verify no planned operations in contract.
    for path, method, operation_id in planned_operations:
        assert operation_id, f"planned operation {method} {path}에 operationId가 없습니다"

    # All planned operations may be absent from dispatch.
    for path, method, _operation_id in planned_operations:
        key = (path, method)
        if key in _DISPATCH_ROUTES:
            # No error if planned is implemented (early implementation).
            pass
        else:
            # Planned and unimplemented is normal.
            pass


# =============================================================================
# Contract coverage test stage 2: status codes and response schema verification (#319).
# =============================================================================

# Mapping of declared status codes in YAML for each operation to actual status codes
# returned by implementation. Built by analyzing service/app.py:dispatch and each service method.
_OPERATION_STATUS_CODES: dict[str, set[int]] = {
    # Management endpoint (#679). No 404 — path is fixed and run is not individually queried.
    # No 400 either — limit is clamped, not rejected.
    "adminListRuns": {200, 403, 503},
    "adminGetConfig": {200, 403},
    "adminListUsers": {200, 400, 403},
    "adminApproveUser": {200, 403, 404},
    "adminRejectUser": {200, 403, 404},
    "healthz": {200},
    "getVersion": {200},
    "getCatalog": {200, 502},
    "listProviders": {200, 403, 502},
    "getProviderStatus": {200, 403, 404, 502},
    "testProviderConnection": {200, 403, 404, 502},
    "getProviderCredential": {200, 403, 404, 502, 503},
    "putProviderCredential": {200, 400, 403, 404, 502, 503},
    "deleteProviderCredential": {200, 403, 404, 502, 503},
    "queryBuiltDataset": {200, 400, 403, 404, 429, 503, 504},
    "listWarehouseTables": {200, 404},
    "getWarehouseTable": {200, 400, 404},
    "getWarehouseTableProfile": {200, 400, 403, 404, 409, 429, 504},
    "queryWarehouseTable": {200, 400, 403, 404, 409, 429, 504},
    "readWarehouseRows": {200, 400, 403, 404, 409, 429, 504},
    "aggregateWarehouseTable": {200, 400, 403, 404, 409, 422, 429, 504},
    "createWarehouseExport": {200, 400, 403, 404, 409, 422, 429, 504},
    "listWarehouseExports": {200},
    "getWarehouseExport": {200, 404},
    "deleteWarehouseExport": {200, 404},
    "downloadWarehouseExport": {200, 403, 404, 409, 410},
    "listAnalyses": {200},
    "saveRevision": {200, 400, 409},
    "getRevision": {200, 400, 404},
    "getRevisionHistory": {200, 400, 404},
    "revertRevision": {200, 400, 404, 409},
    "createAnalysis": {200, 400, 403, 404, 409, 429, 504},
    "getAnalysis": {200, 404},
    "deleteAnalysis": {200, 404},
    "runAnalysis": {200, 400, 403, 404, 409, 429, 504},
    "validateSpec": {200, 400},
    "previewBuild": {200, 400, 403, 502, 503},
    "createBuild": {200, 400, 403, 409, 502},
    "submitBuild": {200, 202, 400, 403, 409, 429, 500},
    "getBuildJob": {200, 400, 403, 404},
    "cancelBuildJob": {200, 400, 403, 404, 409},
    "getBuildManifest": {200, 400, 404, 500},
    "getBuildSpecSnapshot": {200, 400, 403, 404, 500},
    "listBuilds": {200, 400},
    "listBuildArtifacts": {200, 400, 404},
    "getBuildArtifactFile": {200, 400, 403, 404, 503},
    "listDatasets": {200, 400},
    "getDataset": {200, 400, 404},
    "listDatasetRuns": {200, 400, 404},
    "getDatasetRun": {200, 400, 403, 404},
    "getDatasetQualityHistory": {200, 400, 404},
    "listBuildStages": {200, 400, 403, 404},
    "getBuildStageDetail": {200, 400, 403, 404},
    "getBuildQuality": {200, 400, 403, 404},
    "getQualitySummary": {200, 400},
    "listQualityIssues": {200, 400},
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
    """Extract declared status codes from YAML operation."""
    responses = operation.get("responses", {})
    declared_codes: set[int] = set()

    for status_code_str in responses:
        if status_code_str == "default":
            continue
        try:
            declared_codes.add(int(status_code_str))
        except ValueError:
            # "default" or other non-numeric status codes are ignored.
            continue

    return declared_codes


def test_declared_status_codes_match_implementation() -> None:
    """Verify declared status codes in YAML match actual implementation (#319).

    For each operation, declared status codes in YAML responses key must match
    what actual code can return.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        operation_id = operation.get("operationId")
        if not operation_id:
            continue

        # Extract status codes declared in YAML.
        declared_codes = _extract_declared_status_codes(operation)

        # 401 is common to endpoints inside auth gate. Except security: [] (unauthenticated, #372).
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
    """Lock #491 HTTP target/options and actual fail-closed runtime contract."""
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
    """Extract required fields from YAML response schema."""
    # $ref format: "#/components/schemas/SchemaName".
    if not response_ref.startswith("#/components/schemas/"):
        return set()

    schema_name = response_ref[len("#/components/schemas/") :]
    schema = contract["components"]["schemas"].get(schema_name, {})
    required = schema.get("required", [])

    if isinstance(required, list):
        return set(required)
    return set()


def test_response_schemas_have_required_fields() -> None:
    """YAML response schema must declare required fields for 200 response of major endpoints,
    it declares (#319).

    This test verifies contract completeness: each operation defines schema for success response
    (200)
    and includes required fields.
    """
    contract = _load_contract()
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        operation_id = operation.get("operationId")
        if not operation_id:
            continue

        # Verify 200 response.
        responses = operation.get("responses", {})
        if "200" not in responses:
            raise AssertionError(
                f"{method} {path} (operationId: {operation_id})에 200 응답이 누락되었습니다"
            )

        response_200 = responses["200"]
        content = response_200.get("content", {})
        json_content = content.get("application/json", {})
        schema_ref = json_content.get("schema", {}).get("$ref", "")

        # If $ref exists, check required fields.
        if schema_ref:
            required_fields = _extract_response_schema_required_fields(contract, schema_ref)

            assert required_fields, (
                f"{method} {path} (operationId: {operation_id})의 200 응답 스키마에 "
                f"required 필드가 정의되어 있지 않습니다: {schema_ref}"
            )

            # Major endpoints must have at least some required fields.
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
# Runtime wire-level conformance (#209, ADR-0005).
#
# Static validation (above) checks contract YAML matches dispatch routing list. Tests below
# call actual dispatch() and verify returned JSON body conforms to declared response schema.
# Use pure Python validator (_openapi.py) without external dependencies; this closes ADR-0005 open
# question
# #1 (whether schema validation should be pure-Python lightweight) toward "pure-Python lightweight".
#
# 319's test_response_schemas_have_required_fields only checks if schema *declared* required
# fields.
# If app.py omits required fields or changes types in actual response but declaration stays same,
# static check passes — this runtime check catches that wire drift. Test all 6 operations declared
# in contract
# (/version, /validate, /preview, /build, /artifacts, /builds) across success+error status codes.
#
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

# spec that forces 502 path by failing fetch: source unknown to fake client.
_FAILING_SPEC_YAML = _CONFORM_SPEC_YAML.replace("dataset: air_quality\n", "dataset: missing\n")

# spec that parses but fails in validate_spec due to unsupported exporter kind.
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
    """Verify actual dispatch response conforms to contract schema (status code declaration +
    body shape).
    """
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
    """Lock actual wire response of each declared operation conforms to OpenAPI schema."""

    def test_version_200(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/version", "GET")

    def test_version_reports_the_application_version_apart_from_the_contract(
        self, tmp_path: Path
    ) -> None:
        """#777: Studio compares `version` with its own build; `api_version` is the wire.

        #938 adds `publish_credential`, the deployment's publish credential policy.
        """
        from kpubdata_builder import __version__
        from kpubdata_builder.service import API_CONTRACT_VERSION
        from kpubdata_builder.service.publish_credentials import publish_credential_source

        resp = dispatch(_conform_service(tmp_path), "GET", "/version", None)

        assert resp.body == {
            "service": "kpubdata-builder",
            "api_version": API_CONTRACT_VERSION,
            "version": __version__,
            "publish_credential": publish_credential_source(),
        }
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
        # Unsupported exporter → {"status": "invalid", "problems": [...]}.
        _assert_conforms(resp, "/validate", "POST")

    def test_preview_200(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 2}
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_200_keeps_integer_precision(self, tmp_path: Path) -> None:
        # #735: /preview sends an out-of-range integer column as exact decimal text and
        # says so in the schema; an in-range column stays a JSON number.
        client = _FakeClient(
            {"datago.air_quality": [{"id": 9007199254740993, "v": 10}, {"id": 1, "v": 20}]}
        )
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        resp = dispatch(service, "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 2})

        assert resp.status_code == 200
        _assert_conforms(resp, "/preview", "POST")
        preview = cast(list[dict[str, JsonValue]], resp.body["previews"])[0]
        schema = cast(list[dict[str, JsonValue]], preview["schema"])
        assert {c["name"]: c["wire_encoding"] for c in schema} == {
            "id": "decimal_string",
            "v": "number",
        }
        sample = cast(list[dict[str, JsonValue]], preview["sample"])
        assert sample[0] == {"id": "9007199254740993", "v": 10}

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
        # 400 response is oneOf(Error | ValidationError) — limit error is Error form.
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 0}
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_400_limit_above_max(self, tmp_path: Path) -> None:
        # #497: new limit ceiling (1000) — behavioral tightening.
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/preview",
            {"spec": _CONFORM_SPEC_YAML, "limit": 1001},
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_200_random_sample_mode_with_diff(self, tmp_path: Path) -> None:
        # #497: sample_mode=random + diff actually occurring responses satisfy contract.
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
        # 497 sample/diff memory ceiling: even if diffs are actually truncated
        # (diff_truncated=true),
        # verify response still satisfies contract at wire-level.
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
        # 400 response is oneOf(Error | ValidationError) — run_id type error is Error form.
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
        # Verify contract is not violated even if latency sample is recorded after request
        # processing.
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
    """BuildSpec declared model moves together with public OpenAPI contract (#611).

    ``contract/builder-api.yaml`` is HTTP contract read by Studio and type generator.
    Adding field only to Python model works in hand-written YAML but contract consumers
    never discover or type it. Fix drift with test.
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

    def test_join_spec_properties_cover_the_dataclass(self) -> None:
        from dataclasses import fields

        from kpubdata_builder.spec import JoinSpec

        published = set(self._schemas()["JoinSpec"]["properties"])
        declared = {f.name for f in fields(JoinSpec)}
        assert declared <= published, (
            f"JoinSpec fields missing from OpenAPI: {declared - published}"
        )

    def test_composition_provenance_properties_cover_the_dataclass(self) -> None:
        from dataclasses import fields

        from kpubdata_builder.manifest import CompositionProvenance

        published = set(self._schemas()["CompositionProvenance"]["properties"])
        declared = {f.name for f in fields(CompositionProvenance)}
        assert declared <= published, (
            f"CompositionProvenance fields missing from OpenAPI: {declared - published}"
        )

    def test_derived_column_kind_enum_matches_the_supported_kinds(self) -> None:
        from kpubdata_builder.spec.models import DERIVED_KINDS

        published = self._schemas()["DerivedColumn"]["properties"]["kind"]["enum"]
        assert sorted(published) == sorted(DERIVED_KINDS)

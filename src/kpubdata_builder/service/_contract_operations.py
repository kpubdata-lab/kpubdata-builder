"""The operations of the service contract, generated from it (#1109).

Do not edit. `scripts/generate_operations.py` writes this module from
`contract/builder-api.yaml`, and the unit tests fail when it is stale. Read it
through `kpubdata_builder.service.operations`.
"""

# One operation per line, whatever its length: the table is read as a table.
# ruff: noqa: E501
# fmt: off

from __future__ import annotations

#: The contract version this table was generated from.
CONTRACT_VERSION = "1.113.0"

#: (method, path, operation_id, provider_key, publish_credential, authenticated)
OPERATIONS: tuple[tuple[str, str, str, bool, bool, bool], ...] = (
    ("GET", "/healthz", "healthz", False, False, False),
    ("GET", "/version", "getVersion", False, False, True),
    ("GET", "/catalog", "getCatalog", False, False, True),
    ("GET", "/providers", "listProviders", True, False, True),
    ("GET", "/providers/{provider}/status", "getProviderStatus", True, False, True),
    ("POST", "/providers/{provider}/test", "testProviderConnection", True, False, True),
    ("POST", "/providers/{provider}/probe", "probeProviderKey", True, False, True),
    ("GET", "/providers/{provider}/credential", "getProviderCredential", False, False, True),
    ("PUT", "/providers/{provider}/credential", "putProviderCredential", False, False, True),
    ("DELETE", "/providers/{provider}/credential", "deleteProviderCredential", False, False, True),
    ("GET", "/uploads", "listUploads", False, False, True),
    ("POST", "/uploads", "createUpload", False, False, True),
    ("GET", "/uploads/{upload_id}", "getUpload", False, False, True),
    ("DELETE", "/uploads/{upload_id}", "deleteUpload", False, False, True),
    ("POST", "/validate", "validateSpec", False, False, True),
    ("POST", "/preview", "previewBuild", True, False, True),
    ("POST", "/build", "createBuild", True, False, True),
    ("GET", "/artifacts/{run_id}", "listBuildArtifacts", False, False, True),
    ("GET", "/artifacts/{run_id}/{file_path}", "getBuildArtifactFile", False, False, True),
    ("GET", "/builds/{run_id}", "getBuildJob", False, False, True),
    ("POST", "/builds/{run_id}/cancel", "cancelBuildJob", False, False, True),
    ("GET", "/builds/{run_id}/manifest", "getBuildManifest", False, False, True),
    ("GET", "/builds/{run_id}/spec", "getBuildSpecSnapshot", False, False, True),
    ("GET", "/builds", "listBuilds", False, False, True),
    ("POST", "/builds", "submitBuild", True, False, True),
    ("GET", "/datasets", "listDatasets", False, False, True),
    ("GET", "/datasets/{dataset_id}", "getDataset", False, False, True),
    ("GET", "/datasets/{dataset_id}/runs", "listDatasetRuns", False, False, True),
    ("GET", "/datasets/{dataset_id}/runs/{run_id}", "getDatasetRun", False, False, True),
    ("GET", "/builds/{run_id}/events", "getBuildEvents", False, False, True),
    ("GET", "/builds/{run_id}/publish/readiness", "getPublishReadiness", False, True, True),
    ("GET", "/builds/{run_id}/publish/receipt", "getPublishReceipt", False, True, True),
    ("DELETE", "/builds/{run_id}/publish/receipt", "resetPublishReceipt", False, True, True),
    ("POST", "/builds/{run_id}/publish/reconcile", "reconcilePublish", False, True, True),
    ("GET", "/builds/{run_id}/publish/audit", "getPublishAudit", False, False, True),
    ("POST", "/builds/{run_id}/publish", "publishBuild", False, True, True),
    ("GET", "/builds/{run_id}/stages", "listBuildStages", False, False, True),
    ("GET", "/builds/{run_id}/stages/{stage}", "getBuildStageDetail", False, False, True),
    ("GET", "/datasets/{dataset_id}/quality/history", "getDatasetQualityHistory", False, False, True),
    ("GET", "/builds/{run_id}/quality", "getBuildQuality", False, False, True),
    ("GET", "/quality/issues", "listQualityIssues", False, False, True),
    ("GET", "/quality/summary", "getQualitySummary", False, False, True),
    ("POST", "/query", "queryBuiltDataset", False, False, True),
    ("GET", "/warehouse/tables", "listWarehouseTables", False, False, True),
    ("GET", "/warehouse/tables/{name}/profile", "getWarehouseTableProfile", False, False, True),
    ("GET", "/warehouse/tables/{name}", "getWarehouseTable", False, False, True),
    ("POST", "/warehouse/query", "queryWarehouseTable", False, False, True),
    ("POST", "/warehouse/rows", "readWarehouseRows", False, False, True),
    ("POST", "/warehouse/aggregate", "aggregateWarehouseTable", False, False, True),
    ("GET", "/warehouse/exports", "listWarehouseExports", False, False, True),
    ("POST", "/warehouse/exports", "createWarehouseExport", False, False, True),
    ("GET", "/warehouse/exports/{export_id}", "getWarehouseExport", False, False, True),
    ("DELETE", "/warehouse/exports/{export_id}", "deleteWarehouseExport", False, False, True),
    ("GET", "/warehouse/exports/{export_id}/download", "downloadWarehouseExport", False, False, True),
    ("GET", "/analyses", "listAnalyses", False, False, True),
    ("POST", "/analyses", "createAnalysis", False, False, True),
    ("GET", "/analyses/{analysis_id}", "getAnalysis", False, False, True),
    ("DELETE", "/analyses/{analysis_id}", "deleteAnalysis", False, False, True),
    ("POST", "/analyses/{analysis_id}/run", "runAnalysis", False, False, True),
    ("GET", "/revisions/{kind}/{doc_id}", "getRevision", False, False, True),
    ("PUT", "/revisions/{kind}/{doc_id}", "saveRevision", False, False, True),
    ("GET", "/revisions/{kind}/{doc_id}/history", "getRevisionHistory", False, False, True),
    ("POST", "/revisions/{kind}/{doc_id}/revert", "revertRevision", False, False, True),
    ("GET", "/monitoring/summary", "getMonitoringSummary", False, False, True),
    ("GET", "/monitoring/builds", "getMonitoringBuilds", False, False, True),
    ("GET", "/admin/runs", "adminListRuns", False, False, True),
    ("GET", "/admin/users", "adminListUsers", False, False, True),
    ("POST", "/admin/users/{user_id}/approve", "adminApproveUser", False, False, True),
    ("POST", "/admin/users/{user_id}/reject", "adminRejectUser", False, False, True),
    ("GET", "/admin/config", "adminGetConfig", False, False, True),
)

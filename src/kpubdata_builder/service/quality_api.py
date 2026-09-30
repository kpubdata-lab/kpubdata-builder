"""Quality domain service (#596, fifth segment).

Encapsulates per-run structured quality (#486/#514) and recent-window aggregates
(#486 follow-up).

**Depends on the datasets domain** — the run set for 24h aggregates uses
``DatasetsApiService``'s canonical record collection directly. The reason that
helper was made public in the prior segment (#605): duplicating the same collection
logic causes two surfaces to eventually see different run sets.

Maintains one boundary — **does not mix domain quality with system observability
(`/monitoring`) in one response.** So monitoring is not included in this service.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import quality as quality_service
from kpubdata_builder.service import stages as stages_service
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.datasets_api import DatasetsApiService
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.artifacts import ArtifactStore

#: Statuses a row of ``GET /quality/issues`` can have, in the order rows are sorted.
ISSUE_STATUSES = ("fail", "warn", "drift")


class QualityApiService:
    """Per-run structured quality retrieval and recent-window aggregate (#486/#514)."""

    def __init__(
        self,
        *,
        output_root: Path,
        store: ArtifactStore,
        datasets: DatasetsApiService,
    ) -> None:
        self._output_root = output_root
        self._store = store
        self._datasets = datasets

    def get_build_quality(self, run_id: str) -> ServiceResponse:
        """Retrieve structured Quality results and schema drift for run (#486, #514).

        Exposes quality_results/schema_drift per source_key already stored in
        manifest.json — no separate recalculation (manifest is canonical).
        ``availability``/``evaluated_checks`` distinguish whether empty mapping means
        "evaluated but zero checks" or "never calculated" (legacy/partial run)
        (#514).
        """
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        known_sources = stages_service.known_source_keys(manifest)
        availability, evaluated_checks = quality_service.quality_availability(
            manifest, known_sources
        )
        quality_results = manifest.get("quality_results")
        schema_drift = manifest.get("schema_drift")
        return ServiceResponse(
            200,
            {
                "run_id": run_id,
                "availability": availability,
                "evaluated_checks": evaluated_checks,
                "quality_results": cast(
                    JsonValue, quality_results if isinstance(quality_results, dict) else {}
                ),
                "schema_drift": cast(
                    JsonValue, schema_drift if isinstance(schema_drift, dict) else {}
                ),
            },
        )

    def quality_summary(
        self, *, window: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """Summarize structured quality within recent ``window`` as PASS/WARN/FAIL
        run counts (#486 follow-up, additive — API 1.22.0).

        Individual run ``quality_results``/dataset/owner are not exposed —
        that is ``GET /builds/{run_id}/quality``'s responsibility. Does not mix
        system observability (``/monitoring``) with domain quality in one response.

        Run set reuses canonical record collection from datasets domain — narrows
        candidates by manifest mtime (+ derived BuildIndex time window), then
        re-confirms with canonical snapshot + manifest (ENFORCE_OWNERSHIP + if
        oidc principal, own runs only); does not re-parse all-history manifest.
        Index is a derivative so not trusted alone (ADR 0003).
        """
        if window != "24h":
            return ServiceResponse(400, {"error": f"unsupported window: {window!r} (only '24h')"})
        now = datetime.now(timezone.utc)
        base: dict[str, JsonValue] = {
            "window": "24h",
            "generated_at": now.isoformat(timespec="seconds"),
        }
        try:
            records = self._datasets.recent_canonical_records(
                principal,
                now=now,
                window_seconds=quality_service.QUALITY_SUMMARY_WINDOW_SECONDS,
            )
        except Exception:
            # Only unavailable if run enumeration itself is impossible — distinct from "0 runs".
            return ServiceResponse(
                200,
                {
                    **base,
                    "availability": "unavailable",
                    "total_runs": 0,
                    "evaluated_runs": 0,
                    "pass_runs": 0,
                    "warn_runs": 0,
                    "fail_runs": 0,
                },
            )
        entries = (
            (record, datasets_service.read_manifest(self._output_root, record.run_id))
            for record in records
        )
        counts = quality_service.aggregate_quality_window(
            entries,
            now=now,
            window_seconds=quality_service.QUALITY_SUMMARY_WINDOW_SECONDS,
        )
        return ServiceResponse(200, {**base, "availability": "available", **counts})

    def list_issues(
        self,
        *,
        principal: Principal | None = None,
        statuses: frozenset[str] = frozenset(ISSUE_STATUSES),
        dataset_id: str | None = None,
        category: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ServiceResponse:
        """Actionable quality findings across every table the caller may see (#843).

        Each table's **latest** run is read — the state its data is in now — and every
        check with status ``warn`` or ``fail`` becomes a row, as do schema drift
        findings (``status: drift``; drift gates nothing, so it is never counted as a
        failure). Results use the per-run vocabulary unchanged; nothing is re-judged.

        ``coverage`` counts tables apart from findings, so "no issues" is never read
        off tables that were not evaluated: ``not_evaluated`` (no results, or zero
        checks), ``partial`` (some sources only) and ``unreadable`` (manifest
        missing). ``cursor`` is the opaque position returned as ``next_cursor``.
        """
        try:
            offset = int(cursor) if cursor is not None else 0
        except ValueError:
            return ServiceResponse(400, {"error": "cursor is not one this endpoint returned"})
        if offset < 0:
            return ServiceResponse(400, {"error": "cursor is not one this endpoint returned"})
        records = self._datasets.dataset_records(principal)
        latest = datasets_service.group_latest_by_dataset(records)
        coverage = {"tables": 0, "evaluated": 0, "not_evaluated": 0, "partial": 0, "unreadable": 0}
        issues: list[dict[str, JsonValue]] = []
        for record in sorted(latest.values(), key=lambda r: r.dataset_id):
            if dataset_id is not None and record.dataset_id != dataset_id:
                continue
            coverage["tables"] += 1
            manifest = datasets_service.read_manifest(self._output_root, record.run_id)
            if manifest is None:
                coverage["unreadable"] += 1
                continue
            availability, evaluated = quality_service.quality_availability(
                manifest, stages_service.known_source_keys(manifest)
            )
            if availability == "unavailable" or evaluated == 0:
                coverage["not_evaluated"] += 1
            elif availability == "partial":
                coverage["partial"] += 1
            else:
                coverage["evaluated"] += 1
            spec = datasets_service.read_snapshot_spec(self._output_root, record.run_id)
            base: dict[str, JsonValue] = {
                "dataset_id": record.dataset_id,
                "title": spec.title if spec is not None else None,
                "run_id": record.run_id,
                "finished_at": record.finished_at,
            }
            issues.extend(_issues_of(manifest, base))
        selected = [
            issue
            for issue in issues
            if issue["status"] in statuses and (category is None or issue["category"] == category)
        ]
        selected.sort(key=_issue_order)
        page = selected[offset : offset + limit]
        more = offset + limit < len(selected)
        return ServiceResponse(
            200,
            {
                "issues": cast(JsonValue, page),
                "total": len(selected),
                "next_cursor": str(offset + limit) if more else None,
                "coverage": cast(JsonValue, coverage),
            },
        )


def _issues_of(
    manifest: dict[str, object], base: dict[str, JsonValue]
) -> list[dict[str, JsonValue]]:
    rows: list[dict[str, JsonValue]] = []
    results = manifest.get("quality_results")
    if isinstance(results, dict):
        for source_key, checks in results.items():
            if not isinstance(checks, list):
                continue
            for check in checks:
                if isinstance(check, dict) and check.get("status") in ("warn", "fail"):
                    rows.append(
                        {
                            **base,
                            "source_key": str(source_key),
                            "kind": "check",
                            "status": cast(JsonValue, check["status"]),
                            "category": cast(JsonValue, check.get("category")),
                            "check": cast(JsonValue, check),
                            "drift": None,
                        }
                    )
    drift = manifest.get("schema_drift")
    if isinstance(drift, dict):
        for source_key, findings in drift.items():
            if not isinstance(findings, list):
                continue
            for finding in findings:
                if isinstance(finding, dict):
                    rows.append(
                        {
                            **base,
                            "source_key": str(source_key),
                            "kind": "drift",
                            "status": "drift",
                            "category": "schema_drift",
                            "check": None,
                            "drift": cast(JsonValue, finding),
                        }
                    )
    return rows


def _issue_order(issue: dict[str, JsonValue]) -> tuple[int, str, str, str, str]:
    detail = issue["check"] if isinstance(issue["check"], dict) else issue["drift"]
    detail = detail if isinstance(detail, dict) else {}
    return (
        ISSUE_STATUSES.index(cast(str, issue["status"])),
        str(issue["dataset_id"]),
        str(issue["source_key"]),
        str(issue["category"]),
        str(detail.get("rule") or detail.get("kind") or "") + str(detail.get("column") or ""),
    )


__all__ = ["ISSUE_STATUSES", "QualityApiService"]

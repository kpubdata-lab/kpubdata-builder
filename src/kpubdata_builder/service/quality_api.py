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


__all__ = ["QualityApiService"]

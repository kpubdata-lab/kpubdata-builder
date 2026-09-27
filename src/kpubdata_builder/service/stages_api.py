"""Run stage retrieval service (#596 follow-up, #637).

Encapsulates ``/builds/{run_id}/stages`` and per-stage details (Bronze/Silver/Gold).

Read-only; needs only ``output_root`` and artifact store — the domain boundary is
clearest in this segment, so it is extracted first.

**wire contract is unchanged.** ``BuilderService`` delegates with the same
signature.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from kpubdata_builder.service import datasets as datasets_service
from kpubdata_builder.service import stages as stages_service
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.artifacts import ArtifactStore


class StagesApiService:
    """Per-run per-stage artifact retrieval (#488)."""

    def __init__(self, *, output_root: Path, store: ArtifactStore) -> None:
        self._output_root = output_root
        self._store = store

    def list_run_stages(self, run_id: str) -> ServiceResponse:
        """Return Bronze/Silver/Gold status for all known sources in run (#488).

        Before calling, run_id validation, existence check, and ownership gating must
        be complete (dispatch processes /builds/{run_id}/* routes in the same order).
        """
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        summaries = stages_service.list_run_stages(self._output_root, run_id, manifest)
        sources: list[JsonValue] = [
            {
                "source_key": s.source_key,
                "bronze": {"status": s.bronze, "available": s.bronze == "completed"},
                "silver": {"status": s.silver, "available": s.silver == "completed"},
                "gold": {"status": s.gold, "available": s.gold == "completed"},
            }
            for s in summaries
        ]
        return ServiceResponse(200, {"run_id": run_id, "sources": sources})

    def get_run_stage_detail(
        self, run_id: str, stage: str, source_key: str, *, limit: int
    ) -> ServiceResponse:
        """Return safe summary/preview for single stage of single source (#488).

        Order: stage name validation (structure) → check known source in manifest →
        each stage reader reads only sidecar to build response. Raw fetch_params/
        export options/credentials/absolute paths are never included.
        """
        if stage not in stages_service.STAGE_NAMES:
            return ServiceResponse(
                400, {"error": f"invalid stage: {stage!r}; must be one of bronze/silver/gold"}
            )
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return ServiceResponse(404, {"error": f"manifest not found: {run_id}"})
        summary = stages_service.stage_status_for_source(
            self._output_root, run_id, manifest, source_key
        )
        if summary is None:
            return ServiceResponse(404, {"error": f"unknown source: {source_key}"})

        status = stages_service.stage_status_of(summary, stage)
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "stage": stage,
            "source_key": source_key,
            "status": status,
            "available": status == "completed",
        }

        if stage == "bronze":
            bronze = stages_service.bronze_detail(self._output_root, run_id, source_key)
            spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
            matched = stages_service.match_source_ref(spec, source_key) if spec else None
            body["provider"] = matched.provider if matched is not None else None
            body["dataset"] = matched.dataset if matched is not None else None
            body["fetched_at"] = bronze.fetched_at if bronze is not None else None
            body["record_count"] = bronze.record_count if bronze is not None else None
        elif stage == "silver":
            silver = stages_service.silver_detail(
                self._output_root, run_id, source_key, limit=limit
            )
            body["row_count"] = silver.row_count if silver is not None else None
            body["schema"] = silver.schema if silver is not None else []
            body["statistics"] = silver.statistics if silver is not None else None
            body["validation"] = silver.validation if silver is not None else None
            body["sample"] = silver.sample if silver is not None else []
        else:  # gold
            gold = stages_service.gold_detail(self._output_root, run_id, source_key)
            body["row_count"] = gold.row_count if gold is not None else None
            body["columns"] = cast(JsonValue, gold.columns) if gold is not None else []
            body["splits"] = cast(JsonValue, gold.splits) if gold is not None else None
            body["exports"] = (
                cast(JsonValue, [{"kind": kind} for kind in gold.export_kinds])
                if gold is not None
                else []
            )
            # Gold sample sidecar doesn't exist yet — no synthesis; explicitly
            # represent unavailable rather than faking Silver sample.
            body["sample"] = None
            body["sample_available"] = False

        return ServiceResponse(200, body)

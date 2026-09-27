"""System observability service (#596 follow-up, #637).

The ``/monitoring`` surface — queue status, recent build trends, request latency.

**Does not mix with quality domain.** That boundary was established in #606 and
applies here too: ``/quality`` describes data characteristics, ``/monitoring``
describes system state. Mixing them in one response causes consumers reading one
to be dragged along by changes in the other.

**Wire contract is stable.** ``BuilderService`` delegates with the same signature.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from kpubdata_builder.service import monitoring as monitoring_service
from kpubdata_builder.service import ownership as ownership_module
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.service.monitoring import LatencyRecorder
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store.build_index import BuildIndex


class MonitoringApiService:
    """Queue/build/latency observability (#516)."""

    def __init__(
        self,
        *,
        output_root: Path,
        build_index: BuildIndex,
        async_builds: AsyncBuildExecutor,
        latency_recorder: LatencyRecorder,
    ) -> None:
        self._output_root = output_root
        self._build_index = build_index
        self._async_builds = async_builds
        self._latency_recorder = latency_recorder

    def monitoring_summary(self) -> ServiceResponse:
        """Builder API/Queue/Worker/Artifact Store system status summary (#516).

        Contains only system aggregate; does not include individual run dataset/
        owner/credential information — no ownership filtering needed. Provider
        status (#492) would require actual network probe on each request, so it is
        not included in this PR.
        """
        api = monitoring_service.api_status(self._latency_recorder)
        queue = monitoring_service.queue_status(self._async_builds)
        workers = monitoring_service.worker_status(self._async_builds)
        artifact_store = monitoring_service.artifact_store_status(
            self._output_root, self._build_index
        )
        status = monitoring_service.aggregate_status(
            api=api, queue=queue, workers=workers, artifact_store=artifact_store
        )
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return ServiceResponse(
            200,
            {
                "generated_at": generated_at,
                "status": status,
                "api": {
                    "availability": api.availability,
                    "sample_count": api.sample_count,
                    "p95_latency_ms": api.p95_latency_ms,
                },
                "queue": {
                    "availability": queue.availability,
                    "waiting": queue.waiting,
                    "running": queue.running,
                    "total": queue.total,
                },
                "workers": {
                    "availability": workers.availability,
                    "active": workers.active,
                    "capacity": workers.capacity,
                    "utilization": workers.utilization,
                },
                "artifact_store": {
                    "availability": artifact_store.availability,
                    "last_write_at": artifact_store.last_write_at,
                },
            },
        )

    def monitoring_builds(
        self, *, window: str, bucket: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """Return build statistics and recent runs by window/bucket (#516).

        When ENFORCE_OWNERSHIP+oidc principal, aggregate and expose only runs
        accessible by this principal (#505) — prevent other principals' run
        metadata from leaking as a side channel.
        """
        validated_window = monitoring_service.validate_window(window)
        if validated_window is None:
            return ServiceResponse(400, {"error": f"unsupported window: {window!r} (only '24h')"})
        validated_bucket = monitoring_service.validate_bucket(bucket)
        if validated_bucket is None:
            return ServiceResponse(400, {"error": f"unsupported bucket: {bucket!r} (only 'hour')"})

        stats = monitoring_service.build_statistics(
            self._build_index,
            window=validated_window,
            bucket=validated_bucket,
            principal=principal,
            enforce_ownership=ownership_module.enforce_ownership(),
        )
        buckets: list[JsonValue] = [
            {
                "bucket_start": b.bucket_start,
                "bucket_end": b.bucket_end,
                "total": b.total,
                # Wire contract is success/failed/cancelled (#527) — keep internal BuildIndex
                # status value "ok" as-is, only map the external field name to match contract.
                "success": b.success,
                "failed": b.failed,
                "cancelled": b.cancelled,
            }
            for b in stats.buckets
        ]
        recent_runs: list[JsonValue] = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in stats.recent_runs
        ]
        return ServiceResponse(
            200,
            {
                "window": stats.window,
                "bucket": stats.bucket,
                "availability": stats.availability,
                "excluded_count": stats.excluded_count,
                "buckets": buckets,
                "recent_runs": recent_runs,
            },
        )

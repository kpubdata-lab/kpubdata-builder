"""Thin wrapper for pipeline to safely record events (#496).

``pipeline.orchestrator`` records events only through this recorder — never
touches directly. Two layers enforce this.

1. **Message length limit**: Any caller passes only bounded, safe message, but
   defensively truncate length in one place (truncating length only, don't fabricate
   new info or expose hidden content).
2. **Append failure doesn't change already-in-progress build execution/outcome**:
   ``BuildEventStore`` itself doesn't swallow failures (#496, this store is sole event canonical)
   — but most callers wrapped by this recorder have real side effects (bronze/silver/gold persist,
   source fetch) *already happened* before call (``stage_completed``, ``run_finished`` etc.).
   Propagating exception here lets temporary **different subsystem** (event store) failure flip
   already-succeeded source to failure (``_run_source_pipeline`` common except interprets all
   exceptions as "this source failed"), or halt run_build before manifest write, creating state
   where ``manifest.json`` never created (violates AGENTS.md "manifest missing forbidden").
   This is **different** from why ``BuildIndex`` write is best-effort (ADR 0003 — index is
   derived/rebuilitable): here event store isn't derived, but event record failure must not
   breach *other canonical* (manifest/source outcome), so absorbed. But not completely silent
   — beyond ``logger.error``, accumulate failed calls via ``dropped_events()`` so caller
   (orchestrator) can populate existing ``BuildManifest.warnings`` (existed pre-#496,
   no one fills yet, authoritative field) — API consumers can verify via
   ``GET /builds/{run_id}/manifest`` if this run's event timeline actually has holes.
   Don't add new API fields or new status values.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import cast

from ..quality.models import QualityCheckResult
from ..spec.models import JsonValue
from .models import BuildEvent, EventName, EventStatus, StageName
from .store import BuildEventStore

logger = logging.getLogger(__name__)

# Defensive limit on event message. Prevents arbitrarily large quality failure message
# lists from bloating API/store unlimited. Truncates length only, doesn't expose hidden content.
_MAX_MESSAGE_LENGTH = 500


def _bounded_message(message: str | None) -> str | None:
    if message is None:
        return None
    if len(message) <= _MAX_MESSAGE_LENGTH:
        return message
    return message[:_MAX_MESSAGE_LENGTH] + "…"


def _quality_status(results: Sequence[QualityCheckResult]) -> EventStatus:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "ok"


class BuildEventRecorder:
    """Event recorder bound to single run that never propagates exceptions.

    If ``store`` is ``None`` (calling ``run_build`` directly without event store,
    CLI/low-level test path) all methods do nothing — pipeline code doesn't need to
    branch on recorder presence each time.

    Multiple sources record events concurrently to same recorder instance via
    ``ThreadPoolExecutor`` (#247), so failure list accumulated in ``dropped_events()``
    also protected by lock.
    """

    def __init__(self, store: BuildEventStore | None, *, run_id: str) -> None:
        self._store = store
        self._run_id = run_id
        self._dropped_lock = threading.Lock()
        self._dropped_events: list[str] = []

    def dropped_events(self) -> tuple[str, ...]:
        """Return bounded warning list describing failed event appends (#496).

        Original exception messages (sqlite error text may mix internal paths) are
        Does not hold unsafe values — composed only of identifiers already verified safe,
        like event/source_key/stage. Caller (``run_build``) places this value in
        ``BuildManifest.warnings`` for API exposure.
        """
        with self._dropped_lock:
            return tuple(self._dropped_events)

    def _record(
        self,
        event: str,
        status: EventStatus,
        *,
        source_key: str | None = None,
        stage: StageName | None = None,
        message: str | None = None,
        metrics: Mapping[str, JsonValue] | None = None,
    ) -> None:
        if self._store is None:
            return
        built = BuildEvent(
            seq=0,
            timestamp=datetime.now(tz=timezone.utc),
            run_id=self._run_id,
            event=cast(EventName, event),
            status=status,
            source_key=source_key,
            stage=stage,
            message=_bounded_message(message),
            metrics=metrics,
        )
        try:
            self._store.append(built)
        except Exception:
            logger.error(
                "build event append failed (run_id=%s, event=%s, source_key=%s, stage=%s); "
                "recorded as a manifest warning instead of failing the build (#496)",
                self._run_id,
                event,
                source_key,
                stage,
                exc_info=True,
            )
            detail = f"event recording failed: {event}"
            if source_key is not None:
                detail += f" (source_key={source_key})"
            if stage is not None:
                detail += f" (stage={stage})"
            with self._dropped_lock:
                self._dropped_events.append(detail)

    # --- run lifecycle -------------------------------------------------
    #
    # "run_submitted" not here — only event where this recorder's absorption (never-raise) policy
    # doesn't fit: when recorded, job not yet queued to executor (#496), no real side effect,
    # so failure propagation doesn't violate other canonical (should prevent queuing entirely).
    # ``BuilderService.submit_build`` directly appends via
    # ``AsyncBuildExecutor.submit(on_accept=...)``
    # hook, leveraging that propagation.
    #
    # "run_cancelled" (#481) also not here — place that confirms terminal state
    # of cancelled job
    # is service's job registry (queued cancellation means pipeline never runs, no recorder exists).
    # Only from that one place ensures exactly one terminal event per run.
    # Pipeline observes cancellation
    # and does its part by not recording run_finished/run_failed.

    def run_started(self) -> None:
        self._record("run_started", "ok", message="pipeline execution started")

    def run_finished(self) -> None:
        self._record("run_finished", "ok", message="build completed")

    def run_failed(self, *, failed_source_count: int) -> None:
        self._record("run_failed", "fail", message=f"{failed_source_count} source(s) failed")

    # --- source fetch (#498 resolver boundary: common to public_api/file/url) ---

    def source_fetch_started(self, source_key: str, *, message: str | None = None) -> None:
        """``message`` says where a fetch that does not start from nothing continues
        from — the checkpoint of the run this one retries (#1071)."""
        self._record("source_fetch_started", "ok", source_key=source_key, message=message)

    def source_fetch_progress(self, source_key: str, *, done: int, total: int) -> None:
        """One ``param_grid`` combination fetched (#648).

        A 1,500-combination fetch used to be silent for an hour. This is the only
        event that can repeat within a stage, so it carries its position in metrics.
        """
        self._record(
            "source_fetch_progress",
            "ok",
            source_key=source_key,
            message="combination fetched",
            metrics={"done": done, "total": total},
        )

    def source_fetch_completed(self, source_key: str, *, record_count: int) -> None:
        self._record(
            "source_fetch_completed",
            "ok",
            source_key=source_key,
            message="source fetched",
            metrics={"records": record_count},
        )

    def source_fetch_failed(
        self, source_key: str, *, message: str, reason: str | None = None
    ) -> None:
        # A provider's refusal carries its reason (#1187) in metrics, the event's only
        # structured field.
        self._record(
            "source_fetch_failed",
            "fail",
            source_key=source_key,
            message=message,
            metrics={"reason": reason} if reason is not None else None,
        )

    # --- medallion stage -------------------------------------------------

    def stage_started(self, source_key: str, stage: StageName) -> None:
        self._record("stage_started", "ok", source_key=source_key, stage=stage)

    def stage_completed(
        self,
        source_key: str,
        stage: StageName,
        *,
        message: str,
        metrics: Mapping[str, JsonValue] | None = None,
    ) -> None:
        self._record(
            "stage_completed",
            "ok",
            source_key=source_key,
            stage=stage,
            message=message,
            metrics=metrics,
        )

    def stage_failed(self, source_key: str, stage: StageName, *, message: str) -> None:
        self._record("stage_failed", "fail", source_key=source_key, stage=stage, message=message)

    # --- quality/schema checkpoint (#486 reflect results as-is, no re-judgment) --

    def quality_evaluated(self, source_key: str, results: Sequence[QualityCheckResult]) -> None:
        pass_count = sum(1 for r in results if r.status == "pass")
        warn_count = sum(1 for r in results if r.status == "warn")
        fail_count = sum(1 for r in results if r.status == "fail")
        self._record(
            "quality_evaluated",
            _quality_status(results),
            source_key=source_key,
            metrics={
                "check_count": len(results),
                "pass_count": pass_count,
                "warn_count": warn_count,
                "fail_count": fail_count,
            },
        )


__all__ = ["BuildEventRecorder"]

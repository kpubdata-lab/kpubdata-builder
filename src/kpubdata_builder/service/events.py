"""Run event timeline HTTP API service logic (#496).

Contains pure read/serialize logic for ``GET /builds/{run_id}/events``.
Format validation, existence check, and ownership gating are handled first by
dispatch/BuilderService in service/app.py. This module starts after that
(trusted run_id) — same responsibility separation as ``service.stages``.
"""

from __future__ import annotations

from ..events import BuildEvent
from ..spec import JsonValue

# limit query parameter bounded defaults/max. Same convention as other routes
# (``service.stages`` DEFAULT_STAGE_PREVIEW_LIMIT/MAX_STAGE_PREVIEW_LIMIT) —
# keep constants in one place and reuse from both route/service (no magic number duplication).
DEFAULT_EVENTS_LIMIT = 200
MAX_EVENTS_LIMIT = 1000


def event_to_json(event: BuildEvent) -> dict[str, JsonValue]:
    """Convert BuildEvent to wire JSON format."""
    return {
        "seq": event.seq,
        "timestamp": event.timestamp.isoformat(),
        "run_id": event.run_id,
        "event": event.event,
        "status": event.status,
        "source_key": event.source_key,
        "stage": event.stage,
        "message": event.message,
        "metrics": dict(event.metrics) if event.metrics is not None else None,
    }


__all__ = ["DEFAULT_EVENTS_LIMIT", "MAX_EVENTS_LIMIT", "event_to_json"]

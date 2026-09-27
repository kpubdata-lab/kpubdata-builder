"""Per-run structured event timeline (#496).

Provides append-only event timeline so build execution (run/source fetch/medallion
stage/quality checkpoint)
can be queried without raw logger parsing.

Key components:
    - BuildEvent: Single structured event model
    - BuildEventStore: append-only SQLite store (authoritative event timeline)
    - BuildEventRecorder: Wrapper for pipeline to safely record events
"""

from __future__ import annotations

from .models import (
    BuildEvent,
    EventName,
    EventStatus,
    QualityEventName,
    RunEventName,
    SourceFetchEventName,
    StageEventName,
    StageName,
)
from .recorder import BuildEventRecorder
from .store import BuildEventStore

__all__ = [
    "BuildEvent",
    "BuildEventRecorder",
    "BuildEventStore",
    "EventName",
    "EventStatus",
    "QualityEventName",
    "RunEventName",
    "SourceFetchEventName",
    "StageEventName",
    "StageName",
]

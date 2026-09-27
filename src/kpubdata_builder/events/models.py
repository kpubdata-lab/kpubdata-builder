"""Per-run structured event timeline model (#496).

Allows querying the build execution process without raw logger parsing. Main
Express transitions (run/sourceetch/stage/quality) as explicit structured models.

Principles:
    - event/status/stage vocabulary is bounded (Literal) and deterministic. Arbitrary
      don't elevate logger message strings to API contract.
    - arbitrary object dumping, exception object serialization, stack trace, raw
      provider response, raw path, credential not stored here — caller
      (``events.recorder``/``pipeline.orchestrator``) already verified safe
      values only.
    - ``seq`` is monotonic ordering identifier assigned by store at append time
      (#496) — Even if parallel source workers append concurrently, the global order
      can be trusted as-is. Placeholder (0) before append.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ..spec.models import JsonValue

# Run-level transition. "run_cancelled" (#481) is an async job actually recorded as
# terminal state-confirming only when confirmed ``cancelled`` — queued cancellation (pipeline
# not-run) and running cancellation (safety boundary closure) both end with this one,
# never appearing with ``run_finished``/``run_failed`` in same run. Transient state
# (``cancelling``) exposed only via job status, no separate event.
RunEventName = Literal[
    "run_submitted", "run_started", "run_finished", "run_failed", "run_cancelled"
]

# source fetch transition (#498 resolver boundary: common to public_api/file/url).
SourceFetchEventName = Literal[
    "source_fetch_started", "source_fetch_completed", "source_fetch_failed"
]

# medallion stage transition. "export" is BuildSpec.exports execution phase.
StageEventName = Literal["stage_started", "stage_completed", "stage_failed"]

# quality/schema evaluation checkpoint (#486 reflect results as-is, no re-judgment).
QualityEventName = Literal["quality_evaluated"]

EventName = RunEventName | SourceFetchEventName | StageEventName | QualityEventName

# Event outcome. "ok" = success/normal completion, "warn" = quality_evaluated has WARN
# but no FAIL, "fail" = failure. run/source/stage transitions use ok or fail only
# (started=ok, completed=ok, failed=fail).
EventStatus = Literal["ok", "warn", "fail"]

StageName = Literal["bronze", "silver", "gold", "export"]


@dataclass(frozen=True, slots=True)
class BuildEvent:
    """Single structured run event.

    Attributes:
        seq: Monotonic ordering identifier assigned by store at append time.
            0 before append (placeholder) — ``BuildEventStore.append()`` returns
            new instance with actual value filled.
        timestamp: Timezone-aware UTC time.
        run_id: Run this event belongs to.
        event: What transition occurred (bounded vocabulary).
        status: Outcome of that transition (ok/warn/fail).
        source_key: Related source identifier. None for run-level events.
        stage: Related medallion stage. None for source fetch/run-level events.
        message: Human-readable bounded, safe summary. None if absent — don't
            fabricate arbitrary values.
        metrics: JSON-serializable safe numeric summary (e.g. rows/check_count).
            Does not hold raw provider responses or arbitrary objects.
    """

    seq: int
    timestamp: datetime
    run_id: str
    event: EventName
    status: EventStatus
    source_key: str | None = None
    stage: StageName | None = None
    message: str | None = None
    metrics: Mapping[str, JsonValue] | None = None


__all__ = [
    "BuildEvent",
    "EventName",
    "EventStatus",
    "QualityEventName",
    "RunEventName",
    "SourceFetchEventName",
    "StageEventName",
    "StageName",
]

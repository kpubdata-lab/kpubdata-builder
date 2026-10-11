"""Why a run stopped without failing in a stage: cancelled, or never finished (#1120).

A source that fails, a composition that fails and a table commit that is refused are
recorded in the manifest's ``failures``. Two more endings were not recorded anywhere an
administrator could read:

- a **cancelled** run — on request, by the server's build time limit, or by a
  shutdown's grace period running out. Its partial manifest said ``cancelled`` and
  nothing about why or where;
- a run that **ended without a manifest** — one a restart interrupted, one still queued
  when the server shut down, one whose provider keys were gone when it could start, one
  that could not be queued, and one cancelled before it produced anything.

This module is the vocabulary for both: a stable code for the cause, the stage the run
had reached, and the sentence made of the two. Every sentence is put together from the
fixed phrases below. Nothing in it is taken from an exception, an event's message, a
spec or a request, because the build index copies it into the line ``GET /admin/runs``
serves for every owner's runs.
"""

from __future__ import annotations

from typing import Final, Literal

#: The cause of a cancellation.
CancellationCode = Literal["cancelled", "time_limit_exceeded", "server_shutdown"]

#: The cause of an ending that left no manifest.
RunEndingCode = Literal[
    "cancelled",
    "time_limit_exceeded",
    "server_shutdown",
    "interrupted",
    "credentials_required",
    "enqueue_failed",
]

#: How far a run that left no manifest had got: not started, started with no stage
#: recorded yet, or the stage of the last event recorded for it.
RunEndingStage = Literal["queued", "started", "bronze", "silver", "gold", "export"]

RunEndingStatus = Literal["failed", "cancelled"]

#: What happened, per code. A code whose sentence says when on its own takes no stage.
_CAUSES: Final[dict[str, tuple[str, bool]]] = {
    "cancelled": ("the run was cancelled on request", True),
    "time_limit_exceeded": (
        "the run passed the server's build time limit and was cancelled",
        True,
    ),
    "server_shutdown": ("the server was shutting down, and the run was stopped", True),
    "interrupted": ("the server restarted, and the run was interrupted", True),
    "credentials_required": (
        "the run's provider keys were no longer held when it could start",
        False,
    ),
    "enqueue_failed": ("the run could not be queued for execution", False),
}

#: Where it stopped, per stage: a manifest's ``failures[].stage`` or a ``RunEndingStage``.
_WHERE: Final[dict[str, str]] = {
    "queued": "before it started",
    "started": "before its first stage was recorded",
    "bronze": "at the bronze stage",
    "silver": "at the silver stage",
    "gold": "at the gold stage",
    "export": "at the export stage",
    "composition": "before the composition ran",
    "warehouse": "before the run's results were committed",
}

_UNKNOWN_CAUSE: Final = "the run did not finish"

CANCELLATION_CODES: Final[frozenset[str]] = frozenset(
    {"cancelled", "time_limit_exceeded", "server_shutdown"}
)
RUN_ENDING_CODES: Final[frozenset[str]] = frozenset(_CAUSES)
RUN_ENDING_STAGES: Final[frozenset[str]] = frozenset(
    {"queued", "started", "bronze", "silver", "gold", "export"}
)


def run_ending_summary(code: str, stage: str) -> str:
    """The sentence for a run that was cancelled or never finished.

    Made only of the phrases in this module: a code or a stage that is not one of
    them contributes nothing, so a value that reached here from outside cannot appear
    in the result.
    """
    cause, takes_stage = _CAUSES.get(code, (_UNKNOWN_CAUSE, True))
    where = _WHERE.get(stage) if takes_stage else None
    return f"{cause} {where}" if where else cause


__all__ = [
    "CANCELLATION_CODES",
    "RUN_ENDING_CODES",
    "RUN_ENDING_STAGES",
    "CancellationCode",
    "RunEndingCode",
    "RunEndingStage",
    "RunEndingStatus",
    "run_ending_summary",
]

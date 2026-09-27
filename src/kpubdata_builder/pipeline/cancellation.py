"""Minimal cooperative cancellation contract Pipeline understands (#481)."""

from __future__ import annotations

from typing import Protocol


class BuildCancelled(Exception):
    """Internal control-flow signal that cancellation was observed at a safe stage boundary.

    Not placed in the ``BuildError`` hierarchy — cancellation is not a build
    failure, and it must not get mixed into the existing failure handling
    (``except Exception`` → outcome "failed").
    """

    def __init__(self) -> None:
        super().__init__("build cancelled at a safe stage boundary")


class CancellationProbe(Protocol):
    """The minimal cooperative-cancellation interface the pipeline needs."""

    def cancel_requested(self) -> bool:
        """Whether cancellation was requested for this run. Pure lookup — no state change."""
        ...

    def commit(self) -> bool:
        """True when the run can still be finalized normally (success/failure manifest written)."""
        ...


def raise_if_cancelled(probe: CancellationProbe | None) -> None:
    """Standard check at safe boundaries; raises ``BuildCancelled`` when requested."""
    if probe is not None and probe.cancel_requested():
        raise BuildCancelled


__all__ = ["BuildCancelled", "CancellationProbe", "raise_if_cancelled"]

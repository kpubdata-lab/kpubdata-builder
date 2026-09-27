"""Build execution context (#48)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..spec import BuildSpec

_SAFE_RUN_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _utc_now() -> datetime:
    """Return the current UTC time with timezone information."""
    return datetime.now(tz=timezone.utc)


def _validate_run_id(run_id: str) -> None:
    """Reject run_id values that could escape the workspace."""
    if not run_id or run_id != run_id.strip() or not _SAFE_RUN_ID.match(run_id):
        raise ValueError(
            f"run_id contains unsafe characters: {run_id!r}. "
            "Only alphanumeric, dot, hyphen, and underscore are allowed."
        )


@dataclass(frozen=True)
class BuildContext:
    """Context for a single build execution."""

    run_id: str
    output_root: Path
    spec: BuildSpec
    started_at: datetime

    @classmethod
    def create(
        cls,
        spec: BuildSpec,
        *,
        output_root: Path,
        run_id: str | None = None,
        started_at: datetime | None = None,
    ) -> BuildContext:
        """Build a BuildContext, validating or generating the run_id."""
        started = started_at or _utc_now()
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        resolved_run_id = run_id or started.strftime("%Y%m%dT%H%M%S%fZ")
        _validate_run_id(resolved_run_id)
        return cls(
            run_id=resolved_run_id,
            output_root=output_root,
            spec=spec,
            started_at=started,
        )

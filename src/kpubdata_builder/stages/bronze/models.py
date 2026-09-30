"""Bronze stage artifact and source lineage models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ...spec import JsonValue


def utc_now() -> datetime:
    """returns UTC time with timezone information.

    Returns:
        datetime: current UTC time with tzinfo set.
    """
    return datetime.now(tz=timezone.utc)


def require_timezone_aware(value: datetime, *, field_name: str) -> None:
    """validates that datetime includes timezone information."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True)
class ProvenanceEvent:
    """records when and where Bronze source import occurred."""

    source_key: str
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    operation: str = "fetch"

    def __post_init__(self) -> None:
        """enforces fetched_at timezone validity immediately after creation."""
        require_timezone_aware(self.fetched_at, field_name="fetched_at")


TotalStatus = Literal["reported", "unknown", "inconsistent"]


@dataclass(frozen=True)
class CallTotal:
    """What the provider said the total was for one call, next to what was fetched (#816).

    One call is one parameter set: the only call of a plain source, or one
    ``param_grid`` combination. Its pages repeat the same total, so the total is read
    once per call and never added up across pages.

    Attributes:
        index: The call's position — 0 for a plain source, the combination's position in
            ``param_combinations`` otherwise.
        value: The provider's total. ``0`` is a reported zero; None is not.
        status: ``reported`` when every page that stated a total stated the same one;
            ``unknown`` when no page stated one (0 and unknown stay apart);
            ``inconsistent`` when pages disagreed — the value is then None rather than
            one of the disagreeing numbers.
        fetched_row_count: Records this call actually returned.
        pages: Pages read for this call.
    """

    index: int
    value: int | None
    status: TotalStatus
    fetched_row_count: int
    pages: int


@dataclass(frozen=True)
class BronzeArtifact:
    """raw source records collected by Bronze stage."""

    source_key: str
    raw_records: tuple[dict[str, JsonValue], ...]
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    provenance: ProvenanceEvent | None = None
    #: Provider-reported totals, one per call (#816). Empty for a file or URL source,
    #: which has no provider to report one.
    call_totals: tuple[CallTotal, ...] = ()
    #: ``param_grid`` combinations taken from a checkpoint rather than fetched in this
    #: run (#648). Non-zero makes the run not reproducible: its records came from two
    #: fetches at two times.
    resumed_combinations: int = 0

    def __post_init__(self) -> None:
        """enforces fetched_at timezone validity immediately after creation."""
        require_timezone_aware(self.fetched_at, field_name="fetched_at")

    @property
    def record_count(self) -> int:
        """returns count of preserved raw records.

        Returns:
            int: length of raw_records.
        """
        return len(self.raw_records)

"""Bronze stage artifact and source lineage models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

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


@dataclass(frozen=True)
class BronzeArtifact:
    """raw source records collected by Bronze stage."""

    source_key: str
    raw_records: tuple[dict[str, JsonValue], ...]
    fetch_params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=utc_now)
    provenance: ProvenanceEvent | None = None

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

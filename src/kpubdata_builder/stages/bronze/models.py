"""Bronze stage artifact and source lineage models."""

from __future__ import annotations

import shutil
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from ...spec import JsonValue
from .writer import BronzeWriter, new_staging_dir, read_records


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
    """A source's raw records, on disk, and where they came from (#622).

    The records are not held in memory. ``records_path`` is the working copy
    (``writer.WORKING_NAME``: records as the source gave them, keys in source order,
    types kept), which Silver reads and from which persisting writes the sorted-key
    Bronze file. It lives in ``staging_dir`` until the source is done with it;
    :meth:`discard` removes it.
    """

    source_key: str
    records_path: Path
    record_count: int
    staging_dir: Path
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
    #: A :class:`~.build.FetchBound` stopped the fetch while the source still had
    #: records (#1185): the records are a preview's sample, not the source.
    stopped_early: bool = False

    def __post_init__(self) -> None:
        """enforces fetched_at timezone validity immediately after creation."""
        require_timezone_aware(self.fetched_at, field_name="fetched_at")

    @classmethod
    def from_records(
        cls,
        source_key: str,
        records: Iterable[Mapping[str, JsonValue]],
        *,
        staging_dir: Path | None = None,
        fetch_params: dict[str, JsonValue] | None = None,
        fetched_at: datetime | None = None,
        provenance: ProvenanceEvent | None = None,
        call_totals: tuple[CallTotal, ...] = (),
        resumed_combinations: int = 0,
    ) -> BronzeArtifact:
        """Stage ``records`` and return the artifact for them — for records already in
        hand (tests, library callers). A fetch writes through ``BronzeWriter`` instead."""
        with BronzeWriter(staging_dir or new_staging_dir()) as writer:
            writer.write_batch(records)
            records_path, record_count = writer.commit()
        return cls(
            source_key=source_key,
            records_path=records_path,
            record_count=record_count,
            staging_dir=writer.staging_dir,
            fetch_params=dict(fetch_params or {}),
            fetched_at=fetched_at or utc_now(),
            provenance=provenance,
            call_totals=call_totals,
            resumed_combinations=resumed_combinations,
        )

    def iter_records(self) -> Iterator[dict[str, JsonValue]]:
        """The records in order, one at a time, keys as the source gave them."""
        return read_records(self.records_path)

    def records_at(self, indices: Sequence[int]) -> tuple[dict[str, JsonValue], ...]:
        """The records at ``indices``, in the order asked for, read in one pass."""
        wanted = set(indices)
        found: dict[int, dict[str, JsonValue]] = {}
        if wanted:
            last = max(wanted)
            for index, record in enumerate(self.iter_records()):
                if index in wanted:
                    found[index] = record
                if index >= last:
                    break
        return tuple(found[i] for i in indices if i in found)

    def discard(self) -> None:
        """Remove the staging directory — once persisting and every reader are done."""
        shutil.rmtree(self.staging_dir, ignore_errors=True)

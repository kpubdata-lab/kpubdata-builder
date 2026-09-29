"""Detailed provenance model for build manifest (#12).

This module holds immutable value objects for per-source provenance tracking
info (when/where/with what params fetched,
how many records received, what data checksums) and define builder.
Checksums reproducible: sorted-key JSON serialization then SHA-256.

Key components:
    - SourceProvenance: Single source fetch provenance snapshot
    - compute_data_checksum: Reproducible SHA-256 checksum of records
    - build_source_provenance: Raw input → SourceProvenance
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from ..spec import JsonValue
from ..stages.bronze.models import CallTotal

ReportedTotalStatus = Literal["reported", "unknown", "inconsistent", "not_summed", "not_reported"]
CoverageStatus = Literal["complete", "partial", "unknown"]


@dataclass(frozen=True)
class SourceReportedTotal:
    """How many records the provider said there were, kept apart from what was fetched.

    Attributes:
        status: ``reported`` — one call, its pages agreed, ``value`` is the total (``0``
            included). ``unknown`` — the provider stated none. ``inconsistent`` — pages
            of one call disagreed. ``not_summed`` — several calls (``param_grid``); their
            totals are in ``calls`` and deliberately not added up, because combinations
            can overlap. ``not_reported`` — the source has no provider total (a file or
            URL).
        value: The total when ``status`` is ``reported``; None otherwise — never a sum.
        observed_at: When the total was read (the fetch's completion time).
        calls: Each call's reported total and fetched rows, in call order.
    """

    status: ReportedTotalStatus
    value: int | None
    observed_at: str
    calls: tuple[CallTotal, ...] = ()


@dataclass(frozen=True)
class FetchCoverage:
    """Whether the fetch collected everything the provider said there was (#816).

    Attributes:
        status: ``complete`` — every call returned exactly its reported total.
            ``partial`` — some call returned fewer rows than it reported.
            ``unknown`` — no reported total to compare against, or one that cannot be
            trusted.
        reasons: Why it is not complete, one entry per affected call
            (``call <i>: <reason>``) or for the whole source.
    """

    status: CoverageStatus
    reasons: tuple[str, ...] = ()


def summarize_reported_totals(
    call_totals: Sequence[CallTotal], *, observed_at: str
) -> tuple[SourceReportedTotal, FetchCoverage]:
    """The source's reported total and its fetch coverage, from its calls' totals.

    A total is never added up: not across a call's pages (each page repeats it) and not
    across ``param_grid`` combinations (their ranges can overlap). Coverage compares each
    call with its own total.
    """
    calls = tuple(call_totals)
    if not calls:
        return (
            SourceReportedTotal(status="not_reported", value=None, observed_at=observed_at),
            FetchCoverage(status="unknown", reasons=("the source reports no total",)),
        )
    if len(calls) == 1:
        only = calls[0]
        total = SourceReportedTotal(
            status=only.status, value=only.value, observed_at=observed_at, calls=calls
        )
    else:
        total = SourceReportedTotal(
            status="not_summed", value=None, observed_at=observed_at, calls=calls
        )

    partial: list[str] = []
    unknown: list[str] = []
    for call in calls:
        if call.status == "unknown":
            unknown.append(f"call {call.index}: the provider reported no total")
        elif call.status == "inconsistent":
            unknown.append(f"call {call.index}: pages reported different totals")
        elif call.value is not None and call.fetched_row_count < call.value:
            partial.append(
                f"call {call.index}: fetched {call.fetched_row_count} of {call.value} reported"
            )
        elif call.value is not None and call.fetched_row_count > call.value:
            unknown.append(
                f"call {call.index}: fetched {call.fetched_row_count}, more than the "
                f"{call.value} reported"
            )
    if partial:
        coverage = FetchCoverage(status="partial", reasons=tuple(partial + unknown))
    elif unknown:
        coverage = FetchCoverage(status="unknown", reasons=tuple(unknown))
    else:
        coverage = FetchCoverage(status="complete")
    return total, coverage


@dataclass(frozen=True)
class SourceProvenance:
    """Detailed provenance info for single source fetch.

    Attributes:
        provider: Data provider identifier (e.g. datago).
        dataset: Dataset identifier.
        fetched_at: Fetch completion time (UTC ISO 8601 string).
        record_count: Number of records fetched.
        data_checksum: Reproducible data checksum ("sha256:..." format).
        api_version: Source API version. "unknown" if unavailable.
        params: Fetch request parameter snapshot.
        fetched_row_count: Records actually fetched — the same number as
            ``record_count``, under the name that says which count it is (#816). The
            snapshot's row count is the Gold count (``row_counts``), which filtering and
            deduplication can make smaller.
        source_reported_total: What the provider said the total was (#816). None for
            manifests written before it.
        coverage: Whether the fetch collected that total (#816). None for manifests
            written before it.
    """

    provider: str
    dataset: str
    fetched_at: str
    record_count: int
    data_checksum: str
    api_version: str = "unknown"
    params: dict[str, JsonValue] = field(default_factory=dict)
    fetched_row_count: int | None = None
    source_reported_total: SourceReportedTotal | None = None
    coverage: FetchCoverage | None = None


def snapshot_coverage(entry: SourceProvenance | None) -> dict[str, JsonValue] | None:
    """What a committed snapshot records about its fetch (#816), or None when unknown.

    The snapshot's own row count is the Gold count the catalog already keeps; this adds
    the fetched count, the provider's reported total and the coverage verdict, so a
    partial collection is visible wherever the snapshot is read.
    """
    if entry is None or entry.coverage is None or entry.source_reported_total is None:
        return None
    total = entry.source_reported_total
    return {
        "status": entry.coverage.status,
        "reasons": list(entry.coverage.reasons),
        "fetched_row_count": entry.fetched_row_count,
        "source_reported_total": {
            "status": total.status,
            "value": total.value,
            "observed_at": total.observed_at,
        },
    }


def compute_data_checksum(records: Sequence[Mapping[str, JsonValue]]) -> str:
    """Calculate reproducible SHA-256 checksum of records.

    Sorted-key JSON serialization removes key order differences, so identical data
    always produces identical hash.

    Args:
        records: Record sequence to calculate checksum from.

    Returns:
        str: Hexadecimal hash with "sha256:" prefix.
    """
    # Sort records by their serialized form to make checksum order-independent
    serialized_records = sorted(
        json.dumps(dict(record), ensure_ascii=False, sort_keys=True, default=str)
        for record in records
    )
    payload = "[" + ",".join(serialized_records) + "]"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def build_source_provenance(
    *,
    provider: str,
    dataset: str,
    fetched_at: datetime,
    records: Sequence[Mapping[str, JsonValue]],
    params: Mapping[str, JsonValue],
    api_version: str = "unknown",
    call_totals: Sequence[CallTotal] | None = None,
) -> SourceProvenance:
    """Create SourceProvenance from raw fetch info.

    Args:
        provider: Data provider identifier.
        dataset: Dataset identifier.
        fetched_at: Fetch completion time (timezone-aware).
        records: Fetched records (used for count and checksum calculation).
        params: Fetch request parameters.
        api_version: Source API version. "unknown" if omitted.
        call_totals: The provider's reported totals per call (#816). None leaves the
            reported total and coverage out, as for manifests written before them.

    Returns:
        SourceProvenance: Provenance snapshot filled with UTC ISO time and checksum.
    """
    observed_at = fetched_at.astimezone(timezone.utc).isoformat()
    total: SourceReportedTotal | None = None
    coverage: FetchCoverage | None = None
    if call_totals is not None:
        total, coverage = summarize_reported_totals(call_totals, observed_at=observed_at)
    return SourceProvenance(
        provider=provider,
        dataset=dataset,
        fetched_at=observed_at,
        record_count=len(records),
        data_checksum=compute_data_checksum(records),
        api_version=api_version,
        params=dict(params),
        fetched_row_count=len(records),
        source_reported_total=total,
        coverage=coverage,
    )


def compute_inputs_fingerprint(provenance: Sequence[SourceProvenance]) -> str | None:
    """Calculate reproducibility fingerprint for entire build input (#211).

    Sort and combine per-source data checksums as ``provider.dataset=sha256:...`` format,
    then hash once more. Regardless of source order, same input set produces same fingerprint.

    Args:
        provenance: Sequence of provenance snapshots per source.

    Returns:
        str | None: Fingerprint with "sha256:" prefix. None if provenance empty.
    """
    if not provenance:
        return None
    parts = sorted(f"{p.provider}.{p.dataset}={p.data_checksum}" for p in provenance)
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = [
    "FetchCoverage",
    "SourceProvenance",
    "SourceReportedTotal",
    "snapshot_coverage",
    "summarize_reported_totals",
    "build_source_provenance",
    "compute_data_checksum",
    "compute_inputs_fingerprint",
]

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

from ..spec import JsonValue


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
    """

    provider: str
    dataset: str
    fetched_at: str
    record_count: int
    data_checksum: str
    api_version: str = "unknown"
    params: dict[str, JsonValue] = field(default_factory=dict)


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
) -> SourceProvenance:
    """Create SourceProvenance from raw fetch info.

    Args:
        provider: Data provider identifier.
        dataset: Dataset identifier.
        fetched_at: Fetch completion time (timezone-aware).
        records: Fetched records (used for count and checksum calculation).
        params: Fetch request parameters.
        api_version: Source API version. "unknown" if omitted.

    Returns:
        SourceProvenance: Provenance snapshot filled with UTC ISO time and checksum.
    """
    return SourceProvenance(
        provider=provider,
        dataset=dataset,
        fetched_at=fetched_at.astimezone(timezone.utc).isoformat(),
        record_count=len(records),
        data_checksum=compute_data_checksum(records),
        api_version=api_version,
        params=dict(params),
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
    "SourceProvenance",
    "build_source_provenance",
    "compute_data_checksum",
    "compute_inputs_fingerprint",
]

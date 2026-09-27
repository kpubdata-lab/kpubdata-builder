"""Bronze stage source fetch helper.

This module fetches raw records from kpubdata-compatible clients and
provides minimal fetch layer to construct BronzeArtifact and provenance info.

Main components:
    - DatasetResult / SourceDataset / SourceClient: required minimum Protocol contract
    - build_bronze_artifact: convert raw fetch result to bronze output
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from ...spec import JsonValue
from .models import BronzeArtifact, ProvenanceEvent, require_timezone_aware, utc_now


class DatasetResult(Protocol):
    """Minimum result shape returned by compatible kpubdata dataset.

    Attributes:
        items: fetched raw records iterable.
    """

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        """Return fetched records."""
        ...


class SourceDataset(Protocol):
    """Minimum dataset shape used in bronze stage."""

    def list(self, **params: JsonValue) -> DatasetResult:
        """Fetch records for one parameter set."""
        ...


@runtime_checkable
class PaginatedSourceDataset(SourceDataset, Protocol):
    """kpubdata Dataset.list_all() pagination contract."""

    def list_all(self, **params: JsonValue) -> Iterable[DatasetResult]: ...


class SourceClient(Protocol):
    """Minimum client shape used in bronze stage."""

    def dataset(self, source_key: str) -> SourceDataset:
        """Return dataset object for source_key."""
        ...


def build_bronze_artifact(
    client: SourceClient,
    *,
    source_key: str,
    fetch_params: dict[str, JsonValue] | None = None,
    fetched_at: datetime | None = None,
    param_combinations: Sequence[dict[str, JsonValue]] | None = None,
) -> BronzeArtifact:
    """Fetch raw records from compatible client and return bronze output.

    Args:
        client: client providing dataset(source_key).
        source_key: source identifier in provider.dataset form.
        fetch_params: parameters passed to dataset.list call. if ``param_combinations``
            exists, this value is not used in calls but only preserved in provenance—
            common parameters are already merged into each combination.
        param_combinations: multiple call combinations (#613). if given, call once per
            combination and concatenate results **in declared order** into single artifact.
            Order is contract—if changed, artifact_id changes.
        fetched_at: fetch completion time; uses current UTC if omitted.

    Returns:
        BronzeArtifact: output containing raw records and provenance.

    Raises:
        ValueError: if fetched_at lacks timezone info.
    """
    resolved_params = dict(fetch_params or {})
    resolved_fetched_at = fetched_at or utc_now()
    require_timezone_aware(resolved_fetched_at, field_name="fetched_at")

    combinations = tuple(param_combinations) if param_combinations is not None else None
    if combinations is not None and not combinations:
        # Empty expansion succeeds as empty Bronze without any calls.
        # validator blocks at declaration, but also block direct library call path.
        raise ValueError("param_combinations must not be empty")
    calls = combinations if combinations is not None else (resolved_params,)

    dataset = client.dataset(source_key)
    records: list[dict[str, JsonValue]] = []
    for call_params in calls:
        # Concatenate in combination order. Order change alters raw_records.jsonl
        # bytes and artifact_id follows—R1 rebuild determinism depends on it.
        if isinstance(dataset, PaginatedSourceDataset):
            records.extend(
                record for batch in dataset.list_all(**call_params) for record in batch.items
            )
        else:
            records.extend(dataset.list(**call_params).items)
    raw_records = tuple(records)

    # Preserve all combinations in provenance. Without record of which
    # combination made Bronze, reproducibility loses grounding (#613). Single
    # call maintains legacy shape—unused features do not change provenance.
    provenance_params: dict[str, JsonValue] = dict(resolved_params)
    if combinations is not None:
        provenance_params = {
            **resolved_params,
            "param_combinations": cast(JsonValue, [dict(c) for c in combinations]),
        }
    provenance = ProvenanceEvent(
        source_key=source_key,
        fetch_params=provenance_params,
        fetched_at=resolved_fetched_at,
    )

    return BronzeArtifact(
        source_key=source_key,
        raw_records=raw_records,
        fetch_params=provenance_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
    )

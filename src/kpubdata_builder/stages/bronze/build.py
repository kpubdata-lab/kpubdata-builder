"""Bronze stage source fetch helper.

This module fetches raw records from kpubdata-compatible clients and
provides minimal fetch layer to construct BronzeArtifact and provenance info.

Main components:
    - DatasetResult / SourceDataset / SourceClient: required minimum Protocol contract
    - build_bronze_artifact: convert raw fetch result to bronze output
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from ...spec import JsonValue
from .checkpoint import CombinationCheckpoint
from .models import (
    BronzeArtifact,
    CallTotal,
    ProvenanceEvent,
    TotalStatus,
    require_timezone_aware,
    utc_now,
)
from .writer import BronzeWriter, Scrub, new_staging_dir


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
    on_combination_done: Callable[[int, int], None] | None = None,
    checkpoint: CombinationCheckpoint | None = None,
    staging_dir: Path | None = None,
    scrub: Scrub | None = None,
) -> BronzeArtifact:
    """Fetch raw records from a compatible client and write them as Bronze.

    Records are written page by page as they arrive (#622): a page is appended to the
    staging files and let go before the next is read, so memory holds one page, not the
    source. A ``list_all`` that fails part way has its earlier pages on disk until the
    writer aborts and removes them — nothing partial is ever returned.

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
        on_combination_done: called as ``(done, total)`` after each combination of
            ``param_combinations`` has been fetched (#648). A combination boundary is a
            safe point: the records so far are whole. The caller uses it to report
            progress and to stop when cancellation was asked for — by raising, which
            abandons the fetch and removes what was staged. Not called for a single call.
        checkpoint: where finished combinations are kept, and read back from on a
            rebuild of the same run (#648). Combinations it holds are not fetched again;
            the artifact says how many were taken from it. Ignored for a single call.
        staging_dir: where the records are written; a fresh private directory if
            omitted. The artifact points into it until :meth:`BronzeArtifact.discard`.
        scrub: applied to every record before it is written (#686).

    Returns:
        BronzeArtifact: the staged records and their provenance.

    Raises:
        ValueError: if fetched_at lacks timezone info, or a record holds a value JSON
            cannot (NaN, Infinity — #201).
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
    call_totals: list[CallTotal] = []
    use_checkpoint = checkpoint if combinations is not None else None
    resumed = use_checkpoint.load(calls) if use_checkpoint is not None else {}
    with BronzeWriter(staging_dir or new_staging_dir(), scrub=scrub) as writer:
        for done, call_params in enumerate(calls, start=1):
            # Written in combination order. Order change alters raw_records.jsonl
            # bytes and artifact_id follows—R1 rebuild determinism depends on it.
            index = done - 1
            if index in resumed:
                fragment_path, total = resumed[index]
                writer.write_working_lines(fragment_path)
                call_totals.append(total)
            elif use_checkpoint is not None:
                with use_checkpoint.fragment(index) as fragment:
                    reported = _fetch_call(dataset, call_params, fragment.write_batch)
                    total = _call_total(index, reported, fetched=fragment.record_count)
                    use_checkpoint.finish(
                        fragment, index=index, params=call_params, call_total=total
                    )
                writer.write_working_lines(fragment.path)
                call_totals.append(total)
            else:
                before = writer.record_count
                reported = _fetch_call(dataset, call_params, writer.write_batch)
                call_totals.append(
                    _call_total(index, reported, fetched=writer.record_count - before)
                )
            if combinations is not None and on_combination_done is not None:
                on_combination_done(done, len(calls))
        records_path, record_count = writer.commit()

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
        records_path=records_path,
        record_count=record_count,
        staging_dir=writer.staging_dir,
        fetch_params=provenance_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
        call_totals=tuple(call_totals),
        resumed_combinations=len(resumed),
    )


def _fetch_call(
    dataset: SourceDataset,
    params: dict[str, JsonValue],
    write: Callable[[Iterable[dict[str, JsonValue]]], object],
) -> list[int | None]:
    """Fetch one call page by page, handing each page to ``write``; returns page totals."""
    batches: Iterable[DatasetResult] = (
        dataset.list_all(**params)
        if isinstance(dataset, PaginatedSourceDataset)
        else (dataset.list(**params),)
    )
    reported: list[int | None] = []
    for batch in batches:
        write(batch.items)
        reported.append(_reported_total(batch))
    return reported


def _reported_total(batch: object) -> int | None:
    """The page's ``total_count`` (kpubdata ``RecordBatch``), or None when it has none.

    A bool or a negative number is not a count and reads as None.
    """
    value = getattr(batch, "total_count", None)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _call_total(index: int, reported: list[int | None], *, fetched: int) -> CallTotal:
    """One call's total: read once, not summed across its pages (#816)."""
    stated = {value for value in reported if value is not None}
    status: TotalStatus
    if not stated:
        status, value = "unknown", None
    elif len(stated) == 1:
        status, value = "reported", next(iter(stated))
    else:
        status, value = "inconsistent", None
    return CallTotal(
        index=index,
        value=value,
        status=status,
        fetched_row_count=fetched,
        pages=len(reported),
    )

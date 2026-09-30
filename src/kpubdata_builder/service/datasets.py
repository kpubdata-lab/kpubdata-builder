"""Built Dataset query logic (#488).

Uses ``BuildSpec.dataset_id`` as the built dataset identity, grouping multiple
runs sharing the same dataset_id into one dataset.

The source of truth is the BuildSpec snapshot (#487) and manifest.json. BuildIndex
is only a derived search index for dataset→run query performance; even if the index
is empty or corrupted (or missing entirely), this module falls back by reading the
source of truth directly from the filesystem — same principle as ADR 0003 applied
to ``/builds``. The dataset_id returned in the response is always re-validated by
re-reading the latest run's snapshot (``build_dataset_summary``) — the cached
dataset_id in the index is used only to narrow candidates, and even if stale or
corrupted, does not change the source of truth.

Legacy runs (#487 era, created before buildspec.yaml snapshot existed) have their
dataset_id guessed — they are silently excluded from dataset grouping. They still
appear in ``GET /builds``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..manifest import status_from_manifest
from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME
from ..stages._path_safety import ensure_within
from ..stages.bronze.resolve import source_identity
from ..store import BuildEntry, BuildIndex
from .auth import Principal
from .ownership import ownership_allows
from .stages import list_run_stages
from .vocabulary import access_status


@dataclass(frozen=True)
class RunRecord:
    """Lightweight run-level summary for dataset grouping (excludes sidecar).

    ``owner_id`` is canonical stable owner identity (#505, additive). Legacy
    runs lack this field and have None; ``filter_ownership`` falls back to
    ``created_by`` based legacy comparison. Response serialization code
    explicitly selects fields (e.g., ``build_dataset_summary``, dispatch runs
    list), so owner_id is not automatically exposed in wire responses.
    """

    run_id: str
    dataset_id: str
    status: str
    started_at: str | None
    finished_at: str | None
    spec_digest: str | None
    created_by: str | None
    owner_id: str | None = None


def sort_key(record: RunRecord) -> tuple[bool, str, str]:
    """Latest determination sort key (#488 semantics C).

    Ascending comparison of (finished_at exists, finished_at, run_id) —
    runs with finished_at are always treated as newer than runs without it;
    when finished_at matches, deterministic tiebreak by run_id string
    descending (the sort key itself is ascending, so "more recent" means
    larger key value).
    """
    return (record.finished_at is not None, record.finished_at or "", record.run_id)


def is_more_recent(candidate: RunRecord, current: RunRecord) -> bool:
    """Determine if candidate is judged as a more recent run than current."""
    return sort_key(candidate) > sort_key(current)


def pick_latest(records: Sequence[RunRecord]) -> RunRecord:
    """Deterministically select the latest run from records. Records must not be empty."""
    latest = records[0]
    for candidate in records[1:]:
        if is_more_recent(candidate, latest):
            latest = candidate
    return latest


def group_latest_by_dataset(records: Sequence[RunRecord]) -> dict[str, RunRecord]:
    """Deterministically select the latest run by dataset_id."""
    latest: dict[str, RunRecord] = {}
    for record in records:
        current = latest.get(record.dataset_id)
        if current is None or is_more_recent(record, current):
            latest[record.dataset_id] = record
    return latest


def filter_ownership(
    records: Sequence[RunRecord], principal: Principal | None, *, enforce: bool
) -> list[RunRecord]:
    """Keep only runs accessible by the same policy as list_builds._apply_ownership.

    Uses ``service.ownership.ownership_allows`` shared predicate (#504 review) —
    shares semantics with ``query.resolver``/``app._check_ownership``, and
    comparison follows ``principal_owns`` (#505: canonical owner_id prioritized,
    legacy created_by/label fallback). Filter only when ENFORCE_OWNERSHIP + oidc
    principal. dev/service principal and principal=None pass (admin privilege +
    backward compatibility). Even for the same dataset_id, runs from other users
    are completely excluded from grouping/latest selection (#488 semantics D) —
    filtering happens before grouping/latest selection, so other users' runs
    never become latest or mix into metadata.
    """
    if not (enforce and principal is not None and principal.kind == "oidc"):
        return list(records)
    return [
        r
        for r in records
        if ownership_allows(
            created_by=r.created_by, owner_id=r.owner_id, principal=principal, enforce=enforce
        )
    ]


def read_snapshot_dataset_id(output_root: Path, run_id: str) -> str | None:
    """Read only dataset_id from the run's canonical BuildSpec snapshot.

    Return None if snapshot is missing, unreadable, or unparseable — do not
    guess dataset_id for legacy runs (#488 semantics B).
    """
    doc = _read_snapshot_yaml(output_root, run_id)
    if doc is None:
        return None
    dataset_id = doc.get("dataset_id")
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def read_snapshot_identity(output_root: Path, run_id: str) -> tuple[str | None, str | None]:
    """The run's ``(dataset_id, title)`` from its BuildSpec snapshot; None for each unknown."""
    doc = _read_snapshot_yaml(output_root, run_id)
    if doc is None:
        return None, None
    dataset_id, title = doc.get("dataset_id"), doc.get("title")
    return (
        dataset_id if isinstance(dataset_id, str) and dataset_id else None,
        title if isinstance(title, str) and title else None,
    )


def read_snapshot_spec(output_root: Path, run_id: str) -> BuildSpec | None:
    """Parse the entire run's canonical BuildSpec snapshot. Return None if missing or fails."""
    doc = _read_snapshot_yaml(output_root, run_id)
    if doc is None:
        return None
    try:
        return parse_spec(doc)
    except Exception:
        # Defensive against extreme cases where canonical snapshot does not match
        # current parser expectations (e.g., parser schema changed after an old
        # snapshot was written). Do not guess; treat as query unavailable.
        return None


def _read_snapshot_yaml(output_root: Path, run_id: str) -> dict[str, object] | None:
    run_dir = output_root / run_id
    snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
    try:
        ensure_within(output_root, snapshot_path, label="BuildSpec snapshot")
    except ValueError:
        return None
    if not snapshot_path.is_file():
        return None
    try:
        raw = snapshot_path.read_text(encoding="utf-8")
        doc = yaml.safe_load(raw)
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    return doc if isinstance(doc, dict) else None


def read_manifest(output_root: Path, run_id: str) -> dict[str, object] | None:
    """Safely read manifest.json. Return None if outside run_dir, missing, or corrupted."""
    manifest_path = output_root / run_id / "manifest.json"
    try:
        ensure_within(output_root, manifest_path, label="manifest file")
    except ValueError:
        return None
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _entry_to_record(entry: BuildEntry) -> RunRecord | None:
    if entry.dataset_id is None:
        return None
    return RunRecord(
        run_id=entry.run_id,
        dataset_id=entry.dataset_id,
        status=entry.status,
        started_at=entry.started_at,
        finished_at=entry.finished_at,
        spec_digest=entry.spec_digest,
        created_by=entry.created_by,
        owner_id=entry.owner_id,
    )


def collect_run_records_from_index(build_index: BuildIndex) -> list[RunRecord] | None:
    """Get all runs with dataset_id from BuildIndex.

    Return None if index is empty (interpreted per ADR 0003 as "may not yet be
    populated"), signaling the caller to fall back to filesystem. Similarly, if
    index query raises an exception, return None for fallback — index is derived,
    so query failure must not become query failure itself.
    """
    try:
        entries = build_index.list_builds(limit=None)
    except Exception:
        return None
    if not entries:
        return None
    return [record for entry in entries if (record := _entry_to_record(entry)) is not None]


def collect_run_records_from_index_for_dataset(
    build_index: BuildIndex, dataset_id: str
) -> list[RunRecord] | None:
    """Get only runs for a specific dataset_id from BuildIndex.

    If the result for this dataset_id is empty, we must distinguish whether the
    index itself is not yet populated (ADR 0003) or this dataset_id truly does
    not exist — only the latter is trustworthy "not found". If the index contains
    any other run, it is populated, so trust the empty result; if the index is
    completely empty, it may not be populated yet, so return None for the caller
    to fall back to filesystem. Similarly, if query itself raises an exception,
    return None (fallback signal).
    """
    try:
        entries = build_index.list_by_dataset(dataset_id, limit=None)
        if not entries and not build_index.list_builds(limit=1):
            return None
    except Exception:
        return None
    return [record for entry in entries if (record := _entry_to_record(entry)) is not None]


def merge_run_records(
    index_records: Sequence[RunRecord], filesystem_records: Sequence[RunRecord]
) -> list[RunRecord]:
    """Deterministically merge derived index and filesystem source-of-truth candidates by run_id.

    When the same run_id exists in both, prioritize the filesystem record built
    from snapshot/manifest, but preserve spec_digest from the index (which
    filesystem scan alone cannot recover). Results are sorted by run_id
    regardless of input order.
    """
    merged = {record.run_id: record for record in index_records}
    for record in filesystem_records:
        cached = merged.get(record.run_id)
        merged[record.run_id] = RunRecord(
            run_id=record.run_id,
            dataset_id=record.dataset_id,
            status=(
                "cancelled"
                if cached is not None and cached.status == "cancelled"
                else record.status
            ),
            started_at=record.started_at,
            finished_at=record.finished_at,
            spec_digest=(
                cached.spec_digest
                if cached is not None and cached.spec_digest is not None
                else record.spec_digest
            ),
            created_by=record.created_by,
            owner_id=record.owner_id,
        )
    return [merged[run_id] for run_id in sorted(merged)]


def _canonical_record_from_run_id(
    output_root: Path,
    run_id: str,
    *,
    fallback_status: str | None = None,
    spec_digest: str | None = None,
) -> RunRecord | None:
    """Reconstruct run_id from snapshot+manifest. Return None if re-verification fails.

    Shared judgment between ``retain_canonical_run_records`` (input is ``RunRecord``)
    and ``canonical_records_for_run_ids`` (input is run_id set) — eliminates drift
    where the same manifest reads differently depending on path.
    """
    manifest = read_manifest(output_root, run_id)
    dataset_id = read_snapshot_dataset_id(output_root, run_id)
    if manifest is None or dataset_id is None:
        return None
    started_at = manifest.get("started_at")
    finished_at = manifest.get("finished_at")
    created_by = manifest.get("created_by")
    owner_id = manifest.get("owner_id")
    return RunRecord(
        run_id=run_id,
        dataset_id=dataset_id,
        status=status_from_manifest(manifest, fallback_status=fallback_status),
        started_at=started_at if isinstance(started_at, str) else None,
        finished_at=finished_at if isinstance(finished_at, str) else None,
        spec_digest=spec_digest,
        created_by=created_by if isinstance(created_by, str) else None,
        owner_id=owner_id if isinstance(owner_id, str) else None,
    )


def retain_canonical_run_records(
    output_root: Path, records: Sequence[RunRecord]
) -> list[RunRecord]:
    """Keep only runs re-verified by snapshot+manifest, applying source-of-truth metadata."""
    canonical: list[RunRecord] = []
    for record in records:
        rebuilt = _canonical_record_from_run_id(
            output_root,
            record.run_id,
            fallback_status=record.status,
            spec_digest=record.spec_digest,
        )
        if rebuilt is not None:
            canonical.append(rebuilt)
    return canonical


def canonical_records_for_run_ids(output_root: Path, run_ids: Iterable[str]) -> list[RunRecord]:
    """Confirm run_id set as canonical snapshot+manifest (exclude ids failing re-verification).

    Same re-verification as ``retain_canonical_run_records``, but input is run_id,
    not already-built ``RunRecord`` — for callers that narrowed candidates using
    derived signals (BuildIndex time window, filesystem mtime, etc.) before
    confirming only that subset as source-of-truth. Derived signals don't change
    the source of truth, so timestamp/ownership/status are re-read from manifest
    here.
    """
    canonical: list[RunRecord] = []
    for run_id in run_ids:
        rebuilt = _canonical_record_from_run_id(output_root, run_id)
        if rebuilt is not None:
            canonical.append(rebuilt)
    return canonical


def collect_run_records_from_filesystem(output_root: Path) -> list[RunRecord]:
    """Scan filesystem directly, building RunRecord for runs with dataset_id (index fallback).

    Runs missing manifest.json or unable to read dataset_id from buildspec.yaml
    snapshot (legacy or corrupted) are excluded from results (#488 semantics B) —
    these runs still appear in ``GET /builds`` but are not dataset grouping
    targets.
    """
    if not output_root.exists():
        return []
    records: list[RunRecord] = []
    for run_dir in output_root.iterdir():
        if not run_dir.is_dir():
            continue
        manifest = read_manifest(output_root, run_dir.name)
        if manifest is None:
            continue
        dataset_id = read_snapshot_dataset_id(output_root, run_dir.name)
        if dataset_id is None:
            continue
        started_at = manifest.get("started_at")
        finished_at = manifest.get("finished_at")
        created_by = manifest.get("created_by")
        owner_id = manifest.get("owner_id")
        records.append(
            RunRecord(
                run_id=run_dir.name,
                dataset_id=dataset_id,
                status=status_from_manifest(manifest),
                started_at=started_at if isinstance(started_at, str) else None,
                finished_at=finished_at if isinstance(finished_at, str) else None,
                spec_digest=None,
                created_by=created_by if isinstance(created_by, str) else None,
                owner_id=owner_id if isinstance(owner_id, str) else None,
            )
        )
    return records


def dataset_summary_renderable(output_root: Path, record: RunRecord) -> bool:
    """Check if ``build_dataset_summary`` can make canonical summary for this latest run.

    Only re-parses snapshot spec to verify ``dataset_id`` match; does not probe
    manifest or stage output artifacts.

    Must use exact same criteria as ``build_dataset_summary`` None return
    conditions (snapshot unparseable or ``dataset_id`` mismatch) — so ``GET
    /datasets`` ``total`` counts the same set of renderable datasets without
    expensive full summary for out-of-page items. The two functions must change
    this condition together.
    """
    spec = read_snapshot_spec(output_root, record.run_id)
    return spec is not None and spec.dataset_id == record.dataset_id


#: The last finished run's manifest status, in the Refresh axis's words (TERMINOLOGY).
_REFRESH_BY_RUN_STATUS = {"ok": "succeeded", "failed": "failed", "cancelled": "cancelled"}


def _fetched_part_of_a_source(manifest: dict[str, object]) -> bool:
    """Whether any source's fetch collected fewer rows than its provider reported (#816).

    Only an explicit ``partial`` counts. A manifest written before coverage was recorded,
    or a source with no reported total, says nothing either way.
    """
    provenance = manifest.get("provenance")
    if not isinstance(provenance, list):
        return False
    for entry in provenance:
        coverage = entry.get("coverage") if isinstance(entry, dict) else None
        if isinstance(coverage, dict) and coverage.get("status") == "partial":
            return True
    return False


def status_axes(
    manifest: dict[str, object], record: RunRecord, active_statuses: Sequence[str] = ()
) -> dict[str, JsonValue]:
    """The table's state on each axis kpubdata's TERMINOLOGY keeps apart (#781).

    Each axis answers a different question, so each is its own field, and an axis
    with nothing to go on says ``unknown`` rather than a guess:

    - **refresh** — a queued or running refresh wins over the last finished one.
    - **completeness** — from the latest run's manifest: ``partial`` when it is marked
      partial, failed with some sources written, or a source fetched fewer rows than
      its provider reported (#816); ``complete`` when it succeeded,
      ``unknown`` when there is no manifest or nothing was written.
    - **health**, **access**, **maturity** — ``unknown``. Stale needs a declared
      refresh interval, access needs kpubdata's probe results, maturity the source
      spec's grade; none of these reaches Builder yet (#781 leaves each a decision).
    """
    if any(status in ("running", "cancelling") for status in active_statuses):
        refresh = "running"
    elif "queued" in active_statuses:
        refresh = "queued"
    else:
        refresh = _REFRESH_BY_RUN_STATUS.get(record.status, "unknown")

    row_counts = manifest.get("row_counts")
    wrote_rows = isinstance(row_counts, dict) and any(
        isinstance(count, int) and count > 0 for count in row_counts.values()
    )
    if not manifest:
        completeness = "unknown"
    elif manifest.get("partial") is True or _fetched_part_of_a_source(manifest):
        completeness = "partial"
    elif record.status == "ok":
        completeness = "complete"
    elif record.status == "failed" and wrote_rows:
        completeness = "partial"
    else:
        completeness = "unknown"

    return {
        "refresh": refresh,
        "completeness": completeness,
        "health": "unknown",
        # No probe result reaches Builder yet; access_status(None) is Builder's
        # "unknown", and a probe status Builder has not mapped would be too (#831).
        "access": access_status(None),
        "maturity": "unknown",
    }


def build_dataset_summary(
    output_root: Path, record: RunRecord, *, active_statuses: Sequence[str] = ()
) -> dict[str, JsonValue] | None:
    """Build dataset response from latest run's canonical snapshot+manifest+stage status.

    Re-read snapshot to re-verify dataset_id — record.dataset_id was already
    obtained from BuildIndex or filesystem scan, but final response must always
    match the canonical snapshot re-read at this point (#488). If re-verification
    fails (snapshot disappeared, etc.) or values mismatch, return None so the
    caller skips this run.

    row_count is not condensed to a single scalar: for multi-source runs,
    provide both source-level row_counts map and total_row_count (#488 semantics F).

    quality is always None — not forward-implementing #486 (structured quality gate);
    current log-only quality warnings are not arbitrarily converted to
    PASS/WARN/FAIL (#488 semantics E).
    """
    # This guard must match dataset_summary_renderable() logic — keep both conditions
    # in sync so total count covers the same dataset set as list items.
    spec = read_snapshot_spec(output_root, record.run_id)
    if spec is None or spec.dataset_id != record.dataset_id:
        return None
    manifest = read_manifest(output_root, record.run_id) or {}

    # file/url kind (#498) always have empty provider/dataset, so fill canonical
    # identity by kind with source_identity() ("file"/upload_id, "url"/endpoint without
    # query) — public_api remains unchanged, using source.provider/source.dataset as-is.
    sources: list[JsonValue] = []
    for source in spec.sources:
        provider, dataset = source_identity(source)
        sources.append({"provider": provider, "dataset": dataset, "alias": source.alias})

    row_counts: dict[str, JsonValue] = {}
    total_row_count = 0
    raw_row_counts = manifest.get("row_counts")
    if isinstance(raw_row_counts, dict):
        for key, value in raw_row_counts.items():
            if isinstance(key, str) and isinstance(value, int):
                row_counts[key] = value
                total_row_count += value

    stage_summaries = list_run_stages(output_root, record.run_id, manifest)
    stages: dict[str, JsonValue] = {
        summary.source_key: {
            "bronze": summary.bronze,
            "silver": summary.silver,
            "gold": summary.gold,
        }
        for summary in stage_summaries
    }

    return {
        "dataset_id": record.dataset_id,
        "title": spec.title,
        "sources": sources,
        "latest_run_id": record.run_id,
        "status": record.status,
        "updated_at": record.finished_at or record.started_at,
        "row_counts": row_counts,
        "total_row_count": total_row_count,
        "stages": stages,
        "quality": None,
        "status_axes": status_axes(manifest, record, active_statuses),
    }


__all__ = [
    "RunRecord",
    "build_dataset_summary",
    "status_axes",
    "canonical_records_for_run_ids",
    "collect_run_records_from_filesystem",
    "collect_run_records_from_index",
    "collect_run_records_from_index_for_dataset",
    "dataset_summary_renderable",
    "filter_ownership",
    "group_latest_by_dataset",
    "is_more_recent",
    "pick_latest",
    "read_manifest",
    "read_snapshot_dataset_id",
    "read_snapshot_identity",
    "read_snapshot_spec",
    "merge_run_records",
    "retain_canonical_run_records",
    "sort_key",
]

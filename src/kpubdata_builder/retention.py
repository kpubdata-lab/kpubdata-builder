"""Hook for retention/cleanup of partial outputs of cancelled runs (#549, ADR 0008 follow-up).

Default is **retain** — partial artifacts from cancelled runs are audit evidence,
so never deleted without explicit config. Cleanup happens only behind two gates:

1. When caller explicitly passes ``apply=True`` (CLI ``prune-cancelled
   --apply``). Dry-run lists targets only without deletion.
2. When TTL expired — older than ``ttl_hours`` from ``finished_at``
   only cancelled+partial runs targeted. If TTL unset (``None``), no
   targets (inactive).

Deletion happens only at run workspace (``{output_root}/{run_id}``) granularity;
run_id validated via ``validate_path_segment`` first to block path manipulation.
Internal service state outside run (like ``_publish_receipts.sqlite``)
left untouched.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .manifest import status_from_manifest
from .stages._path_safety import validate_path_segment

CANCELLED_RUN_TTL_ENV = "KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS"


@dataclass(frozen=True)
class PruneCandidate:
    """Cleanup candidate run. Used in dry-run report regardless of delete."""

    run_id: str
    finished_at: datetime | None
    partial: bool


@dataclass(frozen=True)
class PruneReport:
    """Result of prune execution. Deletes count only what actually happened."""

    deleted: tuple[str, ...]
    kept: tuple[PruneCandidate, ...]
    scanned: int

    @property
    def deleted_count(self) -> int:
        return len(self.deleted)


def _load_manifest_status(run_dir: Path) -> tuple[str, bool] | None:
    """Read (terminal status, partial) from run workspace manifest.json.

    None if manifest missing or unparseable — excluded from cleanup target decision
    (don't target undecidable state for deletion, fail-closed).
    """
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    partial = raw.get("partial")
    return status_from_manifest(raw), isinstance(partial, bool) and partial


def _finished_at(run_dir: Path) -> datetime | None:
    manifest_path = run_dir / "manifest.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    finished = raw.get("finished_at")
    if not isinstance(finished, str) or not finished:
        return None
    try:
        parsed = datetime.fromisoformat(finished)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def find_cancelled_partial_runs(output_root: Path) -> list[PruneCandidate]:
    """List cancelled+partial runs under output_root (no deletion)."""
    if not output_root.is_dir():
        return []
    candidates: list[PruneCandidate] = []
    for run_dir in sorted(output_root.iterdir()):
        if not run_dir.is_dir():
            continue
        loaded = _load_manifest_status(run_dir)
        if loaded is None:
            continue
        status, partial = loaded
        if status == "cancelled" and partial:
            candidates.append(
                PruneCandidate(run_id=run_dir.name, finished_at=_finished_at(run_dir), partial=True)
            )
    return candidates


def prune_cancelled_runs(
    output_root: Path,
    *,
    ttl_hours: float | None,
    apply: bool = False,
    now: datetime | None = None,
) -> PruneReport:
    """Clean up TTL-expired cancelled+partial runs (#549).

    Args:
        output_root: Run workspace root.
        ttl_hours: Retention period (hours). If ``None``, inactive — delete nothing.
            Environment variable ``KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS`` reading
            is caller's (CLI) responsibility.
        apply: If ``False`` (default), dry-run — list targets only without deletion.
        now: Decision reference time (test injection). Default current UTC.

    Returns:
        :class:`PruneReport` containing deleted run_id list and retained candidates.
    """
    reference = now or datetime.now(timezone.utc)
    candidates = find_cancelled_partial_runs(output_root)

    deleted: list[str] = []
    kept: list[PruneCandidate] = []
    for candidate in candidates:
        if ttl_hours is None:
            kept.append(candidate)
            continue
        finished = candidate.finished_at
        if finished is None:
            # If end time unknown, age cannot be judged — preserve.
            kept.append(candidate)
            continue
        if reference - finished < timedelta(hours=ttl_hours):
            kept.append(candidate)
            continue
        if not apply:
            kept.append(candidate)
            continue

        run_id = candidate.run_id
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError:
            # If directory name violates run_id rules, delete nothing.
            kept.append(candidate)
            continue
        run_dir = output_root / run_id
        shutil.rmtree(run_dir)
        deleted.append(run_id)

    return PruneReport(
        deleted=tuple(deleted),
        kept=tuple(kept),
        scanned=len(candidates),
    )


__all__ = [
    "CANCELLED_RUN_TTL_ENV",
    "PruneCandidate",
    "PruneReport",
    "find_cancelled_partial_runs",
    "prune_cancelled_runs",
]

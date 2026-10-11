"""Retention of run workspaces: cancelled partial runs (#549) and expired runs (#1099).

Two collectors live here. ``prune_cancelled_runs`` is the older one and is described
first; ``collect_expired_runs`` follows it and has its own section below.

Cancelled partial runs (#549, ADR 0008 follow-up).

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
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from .manifest import status_from_manifest
from .stages._path_safety import validate_path_segment
from .store import BuildIndex, make_build_index, rebuild_index

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


# --------------------------------------------------------------------------------------
# Expired runs (#1099)
#
# ``prune_cancelled_runs`` reaches only cancelled partial runs. Every other run a build
# leaves — succeeded or failed — stayed on the disk for good, so the disk filled at the
# rate builds were run. ``collect_expired_runs`` removes the workspace of a run that
# ended ``retention_days`` ago, unless something still needs it:
#
# - ``running``: the run has a submission or start event and no terminal one. A run
#   with a manifest has ended, so this is the moment between the two, or a run a crash
#   left unfinished; either way it is not judged here.
# - ``retry_source``: an unfinished run retries it, and may take its checkpoints over
#   (#1103).
# - ``published``: a publish receipt names it, whatever the receipt's state. Its
#   artifacts are what was published, and the receipt is what stops the same publish
#   going out twice.
# - ``held`` / ``leased`` / ``snapshot``: the warehouse has a committed snapshot of it,
#   held (``warehouse-hold``), being read, or simply there. An export of a snapshot reads
#   the BuildSpec from the run that made it. ``warehouse-gc`` is what lets go of an old
#   snapshot, and the run follows on the next pass.
#
# The age is the manifest's ``finished_at``, never a file's modification time: a run
# whose manifest gives no time is kept. Cancelled runs keep their own policy
# (``prune-cancelled``): they are audit evidence of why a run stopped.
#
# Deleting is three steps, so that a crash at any point is finished by the next pass:
# the workspace is renamed into ``.run-gc/`` — one atomic step that takes the whole run
# out of the output directory at once — then the index row goes, then the files. A pass
# finds what an earlier one left in ``.run-gc/`` and completes it before anything else.
# The references are read again for each run just before it is renamed, so a publish or
# a retry that arrived while the pass ran keeps its run.
# --------------------------------------------------------------------------------------

RUN_RETENTION_DAYS_ENV = "KPUBDATA_BUILDER_RUN_RETENTION_DAYS"

#: Where a run being deleted is moved first, in the output directory (#1099).
RUN_GC_DIRNAME = ".run-gc"

#: The statuses whose runs expire. Cancelled runs are ``prune-cancelled``'s.
_EXPIRING_STATUSES = frozenset({"ok", "failed"})

KeepReason = Literal[
    "running",
    "retry_source",
    "published",
    "held",
    "leased",
    "snapshot",
    "no_finish_time",
    "unsafe_name",
]


@dataclass(frozen=True)
class ExpiredRun:
    """A run past its retention, with the bytes its workspace holds."""

    run_id: str
    status: str
    finished_at: datetime
    size_bytes: int


@dataclass(frozen=True)
class KeptRun:
    """A run past its retention that is kept, and why."""

    run_id: str
    reason: KeepReason


@dataclass(frozen=True)
class RunGcReport:
    """What a pass found, and — when it was applied — what it removed.

    ``expired`` are the runs that go (dry run) or were to go (applied); ``deleted`` the
    ones that went. A run another process referenced between the scan and its turn is
    in ``kept`` and not in ``deleted``.
    """

    applied: bool
    retention_days: int | None
    expired: tuple[ExpiredRun, ...] = ()
    deleted: tuple[str, ...] = ()
    kept: tuple[KeptRun, ...] = ()
    #: Runs that ended inside the retention.
    retained: int = 0
    #: Runs an interrupted pass had started to delete; finished when applied.
    interrupted: tuple[str, ...] = ()
    #: Whether the index was rebuilt because a row of a deleted run would not go.
    index_rebuilt: bool = False

    @property
    def freed_bytes(self) -> int:
        """Bytes removed (applied), or that would be (dry run)."""
        if not self.applied:
            return sum(run.size_bytes for run in self.expired)
        gone = set(self.deleted)
        return sum(run.size_bytes for run in self.expired if run.run_id in gone)


def tree_bytes(path: Path) -> int:
    """The size of the files under ``path``, symbolic links counted as links.

    Nothing outside ``path`` is followed or counted. A file that vanishes while it is
    walked is skipped.
    """
    total = 0
    for directory, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                continue
    return total


def _read_manifest(run_dir: Path) -> dict[str, object] | None:
    manifest_path = run_dir / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _parse_finished(manifest: dict[str, object]) -> datetime | None:
    finished = manifest.get("finished_at")
    if not isinstance(finished, str) or not finished:
        return None
    try:
        parsed = datetime.fromisoformat(finished)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class _References:
    """What still refers to a run: the run events, publish receipts and the warehouse.

    Each store is opened only when its file is there: a deployment that never published
    has no receipts, and this pass does not create the file.
    """

    def __init__(self, output_root: Path, warehouse_root: Path | None) -> None:
        from .events.store import BuildEventStore, events_store_path
        from .service.publish import PublishReceiptStore
        from .warehouse.catalog import CATALOG_FILENAME, TableCatalog

        self._stack = ExitStack()
        self._events = (
            BuildEventStore(output_root) if events_store_path(output_root).is_file() else None
        )
        if self._events is not None:
            self._stack.callback(self._events.close)
        receipts = PublishReceiptStore(output_root)
        self._receipts = receipts if receipts.path.is_file() else None
        self._catalog = (
            TableCatalog(warehouse_root)
            if warehouse_root is not None and (warehouse_root / CATALOG_FILENAME).is_file()
            else None
        )
        if self._catalog is not None:
            self._stack.callback(self._catalog.close)

    def close(self) -> None:
        self._stack.close()

    def _busy(self) -> dict[str, KeepReason]:
        if self._events is None:
            return {}
        busy: dict[str, KeepReason] = {}
        for run_id in self._events.unfinished_runs():
            busy[run_id] = "running"
            submission = self._events.submission(run_id)
            if submission is not None and submission.retry_of is not None:
                busy.setdefault(submission.retry_of, "retry_source")
        return busy

    def reason(self, run_id: str) -> KeepReason | None:
        """Why ``run_id`` must stay, read now; None when nothing refers to it."""
        busy = self._busy().get(run_id)
        if busy is not None:
            return busy
        if self._receipts is not None and self._receipts.has_receipt(run_id):
            return "published"
        if self._catalog is not None:
            snapshots = self._catalog.committed_by_run([run_id]).get(run_id, [])
            if any(self._catalog.live_holds(snapshot.id) for snapshot, _table in snapshots):
                return "held"
            if any(self._catalog.live_lease_count(snapshot.id) for snapshot, _table in snapshots):
                return "leased"
            if snapshots:
                return "snapshot"
        return None


def _run_dirs(output_root: Path) -> Iterator[Path]:
    """Directories that may be run workspaces: not dotted, not underscored, not links."""
    if not output_root.is_dir():
        return
    for run_dir in sorted(output_root.iterdir()):
        if run_dir.name.startswith((".", "_")):
            continue
        if run_dir.is_symlink() or not run_dir.is_dir():
            continue
        yield run_dir


def _close_index(index: BuildIndex) -> None:
    from .store import SqliteBuildIndex

    if isinstance(index, SqliteBuildIndex):
        # No checkpoint: ``TRUNCATE`` waits out the busy timeout for a server reading
        # the index, and nothing here moves the file (#1157).
        index.close(checkpoint=False)
    else:
        index.close()


def collect_expired_runs(
    output_root: Path,
    *,
    retention_days: int | None,
    apply: bool = False,
    warehouse_root: Path | None = None,
    now: datetime | None = None,
    index_factory: Callable[[Path], BuildIndex] = make_build_index,
) -> RunGcReport:
    """Remove — or, by default, only list — the runs past their retention (#1099).

    The caller holds the state lock on ``output_root`` (and the warehouse), as every
    writer does; ``serve`` may run beside it.

    Args:
        output_root: Run workspace root.
        retention_days: Days a succeeded or failed run is kept after it finished.
            ``None`` or ``0`` is off: no run expires. A pass still finishes what an
            interrupted one started.
        apply: Delete. Without it nothing is changed.
        warehouse_root: The table catalog root, whose snapshots keep their runs.
        now: The reference time (tests). Defaults to now, in UTC.
        index_factory: Opens the build index (tests).

    Returns:
        RunGcReport: What expired, what was kept and why, and what was deleted.
    """
    reference = now or datetime.now(timezone.utc)
    gc_dir = output_root / RUN_GC_DIRNAME
    interrupted = tuple(sorted(entry.name for entry in gc_dir.iterdir())) if gc_dir.is_dir() else ()
    days = retention_days if retention_days and retention_days > 0 else None

    expired: list[ExpiredRun] = []
    kept: list[KeptRun] = []
    retained = 0
    if days is not None:
        cutoff = reference - timedelta(days=days)
        references = _References(output_root, warehouse_root)
        try:
            for run_dir in _run_dirs(output_root):
                manifest = _read_manifest(run_dir)
                if manifest is None:
                    continue
                status = status_from_manifest(manifest)
                if status not in _EXPIRING_STATUSES:
                    continue
                finished = _parse_finished(manifest)
                if finished is None:
                    kept.append(KeptRun(run_dir.name, "no_finish_time"))
                    continue
                if finished > cutoff:
                    retained += 1
                    continue
                try:
                    validate_path_segment(run_dir.name, field_name="run_id")
                except ValueError:
                    kept.append(KeptRun(run_dir.name, "unsafe_name"))
                    continue
                reason = references.reason(run_dir.name)
                if reason is not None:
                    kept.append(KeptRun(run_dir.name, reason))
                    continue
                expired.append(ExpiredRun(run_dir.name, status, finished, tree_bytes(run_dir)))
        finally:
            references.close()

    if not apply:
        return RunGcReport(
            applied=False,
            retention_days=days,
            expired=tuple(expired),
            kept=tuple(kept),
            retained=retained,
            interrupted=interrupted,
        )

    deleted: list[str] = []
    index = index_factory(output_root)
    try:
        # What an interrupted pass renamed is gone from the output directory already;
        # its index row and files are finished first.
        for run_id in interrupted:
            index.delete(run_id)
            shutil.rmtree(gc_dir / run_id)
        if expired:
            references = _References(output_root, warehouse_root)
            try:
                for run in expired:
                    # Read again: a publish or a retry may have arrived since the scan.
                    reason = references.reason(run.run_id)
                    if reason is not None:
                        kept.append(KeptRun(run.run_id, reason))
                        continue
                    gc_dir.mkdir(exist_ok=True)
                    os.rename(output_root / run.run_id, gc_dir / run.run_id)
                    index.delete(run.run_id)
                    shutil.rmtree(gc_dir / run.run_id)
                    deleted.append(run.run_id)
            finally:
                references.close()
        if gc_dir.is_dir() and not any(gc_dir.iterdir()):
            gc_dir.rmdir()
        # ``delete`` swallows a failure, as every index write does (ADR 0003). A row
        # that stayed is put right from the manifests, which no longer list the run.
        stale = [run_id for run_id in (*interrupted, *deleted) if index.get(run_id) is not None]
    finally:
        _close_index(index)
    rebuilt = False
    if stale:
        rebuild_index(output_root)
        rebuilt = True
    return RunGcReport(
        applied=True,
        retention_days=days,
        expired=tuple(expired),
        deleted=tuple(deleted),
        kept=tuple(kept),
        retained=retained,
        interrupted=interrupted,
        index_rebuilt=rebuilt,
    )


__all__ = [
    "CANCELLED_RUN_TTL_ENV",
    "RUN_GC_DIRNAME",
    "RUN_RETENTION_DAYS_ENV",
    "ExpiredRun",
    "KeepReason",
    "KeptRun",
    "PruneCandidate",
    "PruneReport",
    "RunGcReport",
    "collect_expired_runs",
    "find_cancelled_partial_runs",
    "prune_cancelled_runs",
    "tree_bytes",
]

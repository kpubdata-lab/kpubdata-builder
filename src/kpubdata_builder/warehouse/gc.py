"""Reclaim snapshots and staging directories nothing needs any more (#699).

Two kinds of garbage, with different causes.

**Orphaned staging directories** are what a crash leaves behind: files under
``_staging/`` that the catalog never recorded, or recorded and then lost. They are
safe to remove because nothing can point at them — a snapshot becomes reachable
only by being promoted and committed.

**Superseded snapshots** are committed snapshots that are no longer current. They
are *not* automatically safe: a query that resolved the pointer before the newer
commit is still reading one. That is what leases are for, and
:meth:`TableCatalog.assert_deletable` is the gate.

Deletion order matters. Files first, then the catalog row. The other way round
leaves files that nothing knows about, which is how an orphan is created in the
first place.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from .catalog import TableCatalog
from .errors import ImmutableSnapshot, SnapshotInUse
from .layout import SnapshotLayout, thaw


@dataclass
class GCReport:
    """What a collection pass did.

    Attributes:
        staging_removed: Orphaned staging directories deleted.
        snapshots_removed: Superseded snapshots deleted.
        kept_current: Snapshots skipped for being current.
        kept_leased: Snapshots skipped for having a live lease.
        leases_expired: Expired lease rows purged.
    """

    staging_removed: list[str] = field(default_factory=list)
    snapshots_removed: list[str] = field(default_factory=list)
    kept_current: list[str] = field(default_factory=list)
    kept_leased: list[str] = field(default_factory=list)
    leases_expired: int = 0

    @property
    def removed_count(self) -> int:
        """How many directories were deleted in total."""
        return len(self.staging_removed) + len(self.snapshots_removed)


def collect_orphan_staging(catalog: TableCatalog, table_id: str) -> list[str]:
    """Delete staging directories the catalog does not know about.

    Returns the names removed.
    """
    layout = SnapshotLayout(catalog.root, table_id)
    known = catalog.known_snapshot_ids(table_id)
    removed: list[str] = []
    for name in layout.orphan_staging_ids(known):
        shutil.rmtree(layout.staging_root / name, ignore_errors=True)
        removed.append(name)
    return removed


def delete_snapshot(catalog: TableCatalog, snapshot_id: str) -> None:
    """Delete one snapshot's files and then its catalog row.

    The lease check and the state change happen in one transaction inside
    ``begin_retiring``, and no lease is issued for a ``retiring`` snapshot. Checking
    and then deleting in two steps left a gap: a query could resolve the pointer and
    take a lease between them, and lose its data mid-read.

    Raises:
        ImmutableSnapshot: It is the table's current snapshot.
        SnapshotInUse: A query holds a live lease on it.
        SnapshotNotFound: No such snapshot.
    """
    snapshot = catalog.begin_retiring(snapshot_id)
    directory = SnapshotLayout(catalog.root, snapshot.table_id).snapshot_dir(snapshot_id)
    if directory.exists():
        # Committed snapshots are read-only, and a read-only directory will not let
        # its entries be unlinked, so permissions come back before the delete.
        thaw(directory)
        shutil.rmtree(directory, ignore_errors=True)
    # Files first, catalog second. The other order creates the orphan it is meant
    # to clean up.
    catalog.forget_snapshot(snapshot_id)


def abandon_stale(catalog: TableCatalog, table_id: str, *, before: str) -> list[str]:
    """Mark uncommitted snapshots older than ``before`` as abandoned.

    The catalog cannot tell a crashed build from a slow one — both look like a
    ``staging`` row — so the age cut-off is the caller's judgement about how long a
    build may take. Nothing is deleted here; the next collection pass reclaims them.

    Returns the snapshot ids marked.
    """
    marked: list[str] = []
    for snapshot in catalog.stale_uncommitted(table_id, before=before):
        catalog.abandon(snapshot.id)
        marked.append(snapshot.id)
    return marked


def collect(
    catalog: TableCatalog,
    table_id: str,
    *,
    keep: int = 3,
    now: str | None = None,
) -> GCReport:
    """Run a collection pass over one table.

    Removes orphaned staging directories, then superseded snapshots beyond the
    ``keep`` most recent, skipping the current snapshot and anything under a live
    lease.

    Args:
        catalog: The catalog to work against.
        table_id: The table to collect.
        keep: How many committed snapshots to retain, newest first. The current
            snapshot always counts as kept regardless of this number.
        now: Override the moment leases are judged against, for tests.

    Returns:
        A :class:`GCReport` describing what happened, including what was kept and
        why. A caller that cannot tell "nothing to do" from "everything was in use"
        cannot diagnose a warehouse that stops reclaiming space.
    """
    report = GCReport()
    report.leases_expired = catalog.purge_expired_leases(now=now)
    report.staging_removed = collect_orphan_staging(catalog, table_id)

    table = catalog.get_table(table_id)
    snapshots = catalog.list_snapshots(table_id)

    # Snapshots nobody will ever commit: a compare-and-swap loser, or what a crash left
    # behind once someone marked it. Their files are not orphans the layout can spot,
    # because the catalog knows about them, so nothing reclaimed them (#699 N-01).
    for snapshot in [s for s in snapshots if s.state in ("abandoned", "retiring")]:
        try:
            delete_snapshot(catalog, snapshot.id)
        except ImmutableSnapshot:
            report.kept_current.append(snapshot.id)
        except SnapshotInUse:
            report.kept_leased.append(snapshot.id)
        else:
            report.snapshots_removed.append(snapshot.id)

    committed = [s for s in snapshots if s.state == "committed"]
    # list_snapshots is newest first, so everything past `keep` is a candidate.
    for snapshot in committed[keep:]:
        if snapshot.id == table.current_snapshot_id:
            report.kept_current.append(snapshot.id)
            continue
        try:
            delete_snapshot(catalog, snapshot.id)
        except ImmutableSnapshot:
            report.kept_current.append(snapshot.id)
        except SnapshotInUse:
            report.kept_leased.append(snapshot.id)
        else:
            report.snapshots_removed.append(snapshot.id)
    return report


__all__ = [
    "GCReport",
    "abandon_stale",
    "collect",
    "collect_orphan_staging",
    "delete_snapshot",
]

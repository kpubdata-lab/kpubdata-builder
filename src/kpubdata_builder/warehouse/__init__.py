"""Warehouse table catalog — immutable snapshots with a transactional pointer (#699).

What separates a build engine from a warehouse is this one property: a committed
table is immutable, and a refresh writes a **new** snapshot and moves a pointer.
The previous snapshot's files are never touched.

The existing stage path replaces the destination directory instead
(``stages/_atomic.py``), which leaves a window where the final path does not
exist. A reader landing in it gets "no such table" rather than the previous
version, and a failed refresh cannot promise the old state survived, because the
old state is what got replaced.

Three pieces:

- :class:`~kpubdata_builder.warehouse.catalog.TableCatalog` — canonical state.
  Which snapshot is current lives only here, so it uses ``BEGIN IMMEDIATE`` and a
  compare-and-swap on ``revision`` rather than swallowing write failures the way a
  derived index may.
- :class:`~kpubdata_builder.warehouse.layout.SnapshotLayout` — paths. Staging and
  committed snapshots are separate directories, and promotion is a single rename.
- :mod:`~kpubdata_builder.warehouse.gc` — reclaims orphaned staging directories
  and superseded snapshots, refusing anything current or under a lease.
- :mod:`~kpubdata_builder.warehouse.baseline` — picks the committed snapshot drift
  may compare against (#700), scoped by owner, coverage and schema contract, and
  returning a stated reason rather than ``None`` when nothing qualifies.

The order of a commit is the contract::

    begin_snapshot()      catalog knows about it, so GC leaves it alone
    ... write files ...   into _staging/<id>/
    write_manifest()
    mark_validated()      commit accepts nothing else
    promote()             single rename into snapshots/<id>/ — files now final
    commit_snapshot()     one transaction: state -> committed, CAS the pointer

Files reach their final path **before** the pointer moves. A crash at any step
leaves the previous current snapshot readable, because nothing ever wrote over it.
"""

from __future__ import annotations

from .baseline import (
    BaselineFound,
    BaselineOutcome,
    DriftAxis,
    NotEvaluated,
    NotEvaluatedReason,
    select_baseline,
)
from .catalog import (
    CATALOG_FILENAME,
    DEFAULT_LEASE_SECONDS,
    SCHEMA_VERSION,
    PinnedSnapshot,
    SnapshotRow,
    SnapshotState,
    TableCatalog,
    TableRow,
)
from .errors import (
    ImmutableSnapshot,
    SnapshotConflict,
    SnapshotInUse,
    SnapshotNotFound,
    SnapshotStateError,
    TableNotFound,
    WarehouseError,
)
from .layout import MANIFEST_FILENAME, SnapshotLayout, SnapshotManifest

__all__ = [
    "CATALOG_FILENAME",
    "BaselineFound",
    "BaselineOutcome",
    "DEFAULT_LEASE_SECONDS",
    "DriftAxis",
    "MANIFEST_FILENAME",
    "NotEvaluated",
    "NotEvaluatedReason",
    "SCHEMA_VERSION",
    "ImmutableSnapshot",
    "PinnedSnapshot",
    "SnapshotConflict",
    "SnapshotInUse",
    "SnapshotLayout",
    "SnapshotManifest",
    "SnapshotNotFound",
    "SnapshotRow",
    "SnapshotState",
    "SnapshotStateError",
    "TableCatalog",
    "TableNotFound",
    "TableRow",
    "WarehouseError",
    "select_baseline",
]

"""Warehouse catalog exceptions (#699).

The catalog is **canonical**. Unlike the build index (ADR 0003) it does not
swallow write failures: if a pointer update fails silently, nothing knows which
snapshot a table points at. Every exception here propagates to the caller.
"""

from __future__ import annotations


class WarehouseError(Exception):
    """Base class for warehouse catalog failures."""


class TableNotFound(WarehouseError):
    """A table that is not in the catalog was referenced."""


class SnapshotNotFound(WarehouseError):
    """A snapshot that is not in the catalog was referenced."""


class SnapshotConflict(WarehouseError):
    """A concurrent update won the race.

    The compare-and-swap ``revision`` did not match: another commit moved the
    pointer first.

    **This is not something to retry.** The losing commit was built on a state
    that no longer exists, and retrying blindly would overwrite the commit that
    won. The caller has to re-read the current state and decide whether the
    refresh is still needed.
    """


class SnapshotStateError(WarehouseError):
    """The snapshot is in a state that does not allow the requested transition."""


class ImmutableSnapshot(WarehouseError):
    """An attempt was made to overwrite or delete a committed snapshot."""


class SnapshotInUse(WarehouseError):
    """An attempt was made to delete a snapshot that a query is reading.

    A live lease exists. Garbage collection skips the snapshot when it sees this.
    """


__all__ = [
    "ImmutableSnapshot",
    "SnapshotConflict",
    "SnapshotInUse",
    "SnapshotNotFound",
    "SnapshotStateError",
    "TableNotFound",
    "WarehouseError",
]

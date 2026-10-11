"""Room for a build before it is accepted: the disk and each owner's runs (#1099).

A build writes its workspace as it runs, and nothing bounded how much one user's runs
could hold or stopped a build from starting on a full disk. ``POST /builds`` now asks
two things before it queues a job:

- **Is the disk low?** When the filesystem under the output directory has less free
  space than ``KPUBDATA_BUILDER_BUILD_MIN_FREE_MB``, the build is refused with 503
  ``disk_space_low``. The space is measured as ``/readyz`` measures it.
- **Would the owner's runs pass their limit?** In a multi-user deployment an owner's
  finished runs may hold ``KPUBDATA_BUILDER_RUN_STORAGE_MAX_BYTES`` bytes. A build
  cannot know its size before it runs, so each of the owner's queued or running builds
  reserves ``KPUBDATA_BUILDER_RUN_STORAGE_RESERVE_BYTES``; a new build is refused with
  409 ``run_storage_quota_exceeded`` when the finished runs and the reservations,
  its own included, would pass the limit — or when the finished runs already reach it.
  The reservations are counted under the job registry's lock, so two submissions at
  once cannot both take the last room.

Only run workspaces are counted. Uploads have their own limits (#1045) and are not in
a run workspace, so nothing is counted twice; warehouse snapshots are not counted.

**Every value is off by default.** How much a user may keep and how much free space a
build needs are the maintainer's to decide (kpubdata#812); until then nothing here
refuses a build. A malformed or negative value is read as off and warned about at
start-up (``startup_settings``). The synchronous ``POST /build`` is not checked, as it
is not by the per-owner build limit (#1189).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from ..retention import tree_bytes
from ..stages._path_safety import validate_path_segment
from ..store import BuildIndex
from .ownership import multi_user_mode
from .readiness import measure_disk_usage

logger = logging.getLogger(__name__)

RUN_STORAGE_MAX_BYTES_ENV = "KPUBDATA_BUILDER_RUN_STORAGE_MAX_BYTES"
RUN_STORAGE_RESERVE_BYTES_ENV = "KPUBDATA_BUILDER_RUN_STORAGE_RESERVE_BYTES"
BUILD_MIN_FREE_MB_ENV = "KPUBDATA_BUILDER_BUILD_MIN_FREE_MB"


def _non_negative(name: str) -> int:
    """The setting as an integer >= 0; unset, malformed or negative is 0 (off)."""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        return 0
    return max(value, 0)


def resolve_owner_storage_limit() -> int | None:
    """Bytes one owner's finished runs may hold, or None when there is no limit."""
    if not multi_user_mode():
        return None
    return _non_negative(RUN_STORAGE_MAX_BYTES_ENV) or None


def resolve_storage_reserve() -> int:
    """Bytes each queued or running build of the owner reserves."""
    return _non_negative(RUN_STORAGE_RESERVE_BYTES_ENV)


def resolve_build_min_free_bytes() -> int | None:
    """Free bytes the output filesystem must have for a build to start, or None."""
    megabytes = _non_negative(BUILD_MIN_FREE_MB_ENV)
    return megabytes * 1024 * 1024 or None


@dataclass(frozen=True)
class StorageAdmission:
    """What a submission found about the room for its build."""

    disk_low: bool
    #: The owner's limit, or None when there is none.
    limit_bytes: int | None
    #: Bytes the owner's finished runs hold.
    used_bytes: int
    reserve_bytes: int

    def over_quota(self, active: int) -> bool:
        """Whether one more build would pass the limit, with ``active`` builds already
        queued or running for the owner."""
        if self.limit_bytes is None:
            return False
        reserved = (active + 1) * self.reserve_bytes
        return self.used_bytes >= self.limit_bytes or self.used_bytes + reserved > self.limit_bytes


def owner_run_bytes(output_root: Path, index: BuildIndex, owner: str) -> int:
    """Bytes the workspaces of ``owner``'s indexed runs hold.

    The owner is a run's ``owner_id``, else its ``created_by``, as the job registry
    counts it. A run still building is not indexed yet; its reservation stands for it.
    """
    total = 0
    for entry in index.list_builds(limit=None):
        if (entry.owner_id or entry.created_by) != owner:
            continue
        try:
            validate_path_segment(entry.run_id, field_name="run_id")
        except ValueError:
            continue
        run_dir = output_root / entry.run_id
        if run_dir.is_dir() and not run_dir.is_symlink():
            total += tree_bytes(run_dir)
    return total


def storage_admission(
    output_root: Path, index: BuildIndex, owner: str | None
) -> StorageAdmission | None:
    """Measure what the settings ask about; None when every check is off.

    A measurement that fails is logged and does not refuse the build: ``/readyz``
    reports a disk it cannot read, and a build is not turned away on a guess.
    """
    min_free = resolve_build_min_free_bytes()
    limit = resolve_owner_storage_limit() if owner is not None else None
    if min_free is None and limit is None:
        return None
    disk_low = False
    if min_free is not None:
        try:
            disk_low = measure_disk_usage(output_root).free_bytes < min_free
        except OSError:
            logger.warning("could not measure the free space under the output directory")
    used = 0
    if limit is not None and owner is not None:
        try:
            used = owner_run_bytes(output_root, index, owner)
        except Exception:  # noqa: BLE001 - an unreadable index does not refuse a build
            logger.warning("could not measure the owner's run storage", exc_info=True)
            limit = None
    return StorageAdmission(
        disk_low=disk_low,
        limit_bytes=limit,
        used_bytes=used,
        reserve_bytes=resolve_storage_reserve(),
    )


__all__ = [
    "BUILD_MIN_FREE_MB_ENV",
    "RUN_STORAGE_MAX_BYTES_ENV",
    "RUN_STORAGE_RESERVE_BYTES_ENV",
    "StorageAdmission",
    "owner_run_bytes",
    "resolve_build_min_free_bytes",
    "resolve_owner_storage_limit",
    "resolve_storage_reserve",
    "storage_admission",
]

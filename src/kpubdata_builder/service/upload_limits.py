"""Per-user upload limits for a multi-user deployment (#1045, kpubdata#812).

One account must not be able to fill the data volume, and uploads are not kept for
ever. In a multi-user deployment each owner may hold a number of files and a total
size, and an upload is deleted once it is older than the retention period.

The three defaults — 50 files, 1 GiB, 30 days — were chosen without measurement
(kpubdata#812) and are to be revisited after the first deployment. Each is overridable,
and ``0`` turns that one off. A single-user deployment has one owner and applies none.
The per-file limit (``KPUBDATA_BUILDER_MAX_UPLOAD_BYTES``) is separate and applies
everywhere.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .ownership import multi_user_mode

MAX_FILES_ENV = "KPUBDATA_BUILDER_UPLOAD_MAX_FILES"
MAX_TOTAL_BYTES_ENV = "KPUBDATA_BUILDER_UPLOAD_MAX_TOTAL_BYTES"
RETENTION_DAYS_ENV = "KPUBDATA_BUILDER_UPLOAD_RETENTION_DAYS"

DEFAULT_MAX_FILES = 50
DEFAULT_MAX_TOTAL_BYTES = 1024**3
DEFAULT_RETENTION_DAYS = 30


def _non_negative_int(name: str, default: int) -> int:
    """The variable as an integer; the default when it is unset, malformed or negative."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class UploadLimits:
    """What one owner may hold. ``None`` means that limit is off."""

    max_files: int | None
    max_total_bytes: int | None
    retention_days: int | None

    def cutoff(self, now: datetime | None = None) -> datetime | None:
        """Uploads created before this are past retention; None when retention is off."""
        if self.retention_days is None:
            return None
        return (now or datetime.now(timezone.utc)) - timedelta(days=self.retention_days)


def resolve_upload_limits() -> UploadLimits | None:
    """The limits in force, or None in a single-user deployment."""
    if not multi_user_mode():
        return None

    def limit(name: str, default: int) -> int | None:
        value = _non_negative_int(name, default)
        return value or None

    return UploadLimits(
        max_files=limit(MAX_FILES_ENV, DEFAULT_MAX_FILES),
        max_total_bytes=limit(MAX_TOTAL_BYTES_ENV, DEFAULT_MAX_TOTAL_BYTES),
        retention_days=limit(RETENTION_DAYS_ENV, DEFAULT_RETENTION_DAYS),
    )


__all__ = [
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_RETENTION_DAYS",
    "MAX_FILES_ENV",
    "MAX_TOTAL_BYTES_ENV",
    "RETENTION_DAYS_ENV",
    "UploadLimits",
    "resolve_upload_limits",
]

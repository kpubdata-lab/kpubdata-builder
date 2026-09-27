"""Snapshot-based incremental build support (#15).

Store and load previous build snapshot (data checksum + fetch parameters), compare
with current data/parameters to detect changes. Skip rebuild if unchanged
Reduces build time and API calls.

Snapshot storage structure::

    {root}/.kpubdata-builder/snapshots/{dataset_id}/snapshot.json

Key components:
    - BuildSnapshot: Last build snapshot model
    - compute_records_checksum: Reproducible SHA-256 checksum
    - save_snapshot / load_snapshot: Disk save/load
    - has_data_changed: Detect changes vs previous snapshot
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .spec import JsonValue
from .stages._path_safety import validate_path_segment

_SNAPSHOT_DIRNAME = ".kpubdata-builder"


@dataclass(frozen=True)
class BuildSnapshot:
    """Data version snapshot of last build.

    Attributes:
        dataset_id: Dataset identifier.
        built_at: Build time (UTC ISO 8601 string).
        data_checksum: Reproducible data checksum ("sha256:...").
        record_count: Record count.
        source_params: Fetch parameter snapshot.
    """

    dataset_id: str
    built_at: str
    data_checksum: str
    record_count: int = 0
    source_params: dict[str, JsonValue] = field(default_factory=dict)


def compute_records_checksum(records: Sequence[Mapping[str, JsonValue]]) -> str:
    """Calculate reproducible SHA-256 checksum of records.

    Individually serialize each record with sorted key, then sort serialized strings
    to remove differences in record key order and record (row) order. Same
    dataset has same checksum regardless of API return order (#165).

    Args:
        records: Record sequence to calculate checksum from.

    Returns:
        str: Hexadecimal hash with "sha256:" prefix.
    """
    serialized_records = sorted(
        json.dumps(dict(record), ensure_ascii=False, sort_keys=True, default=str)
        for record in records
    )
    payload = "[" + ",".join(serialized_records) + "]"
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _snapshot_path(root: Path, dataset_id: str) -> Path:
    """Calculate snapshot file path for dataset_id (includes path safety validation)."""
    validate_path_segment(dataset_id, field_name="dataset_id")
    return root / _SNAPSHOT_DIRNAME / "snapshots" / dataset_id / "snapshot.json"


def save_snapshot(snapshot: BuildSnapshot, *, root: Path) -> Path:
    """Save snapshot as deterministic JSON under root, return path.

    Args:
        snapshot: Snapshot to save.
        root: Workspace root.

    Returns:
        Path: Recorded snapshot.json path.
    """
    path = _snapshot_path(root, snapshot.dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "dataset_id": snapshot.dataset_id,
        "built_at": snapshot.built_at,
        "data_checksum": snapshot.data_checksum,
        "record_count": snapshot.record_count,
        "source_params": snapshot.source_params,
    }
    _ = path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_snapshot(dataset_id: str, *, root: Path) -> BuildSnapshot | None:
    """Load saved snapshot (None if missing).

    Args:
        dataset_id: Dataset identifier.
        root: Workspace root.

    Returns:
        BuildSnapshot | None: Snapshot object or None.
    """
    path = _snapshot_path(root, dataset_id)
    if not path.exists():
        return None
    # Corrupted/truncated snapshot safely degrades to "no snapshot"
    # instead of breaking incremental build.
    # (None → has_data_changed becomes True → full rebuild) (#194).
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    required = ("dataset_id", "built_at", "data_checksum")
    if any(key not in data for key in required):
        return None
    try:
        record_count = int(data.get("record_count", 0))
    except (TypeError, ValueError):
        return None
    source_params = data.get("source_params", {})
    if not isinstance(source_params, dict):
        return None
    return BuildSnapshot(
        dataset_id=str(data["dataset_id"]),
        built_at=str(data["built_at"]),
        data_checksum=str(data["data_checksum"]),
        record_count=record_count,
        source_params=dict(source_params),
    )


def has_data_changed(
    records: Sequence[Mapping[str, JsonValue]],
    source_params: Mapping[str, JsonValue],
    snapshot: BuildSnapshot | None,
) -> bool:
    """Determine if data or parameters changed vs previous snapshot.

    Args:
        records: Current data records.
        source_params: Current fetch parameters.
        snapshot: Previous snapshot (if missing, first build).

    Returns:
        bool: True if changed or first build.
    """
    if snapshot is None:
        return True
    if dict(source_params) != snapshot.source_params:
        return True
    return compute_records_checksum(records) != snapshot.data_checksum


__all__ = [
    "BuildSnapshot",
    "compute_records_checksum",
    "has_data_changed",
    "load_snapshot",
    "save_snapshot",
]

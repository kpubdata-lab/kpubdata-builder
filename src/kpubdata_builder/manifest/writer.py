"""Build manifest writer (Medallion reorganization: split from old manifest.py).

This module records BuildManifest as deterministic JSON to disk. UTC-based ISO
strings and sorted keys to keep serialization stable.

Key functions:
    - manifest_writer / write_manifest: Disk recording functions
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import timezone
from pathlib import Path
from typing import Any

from ..errors import ManifestError
from .models import BuildManifest
from .provenance import SourceProvenance

#: Provenance fields added by #816. Left out, not null, when a fetch did not record
#: them — a manifest written before them has no such keys, and a reader tells the two
#: cases apart the same way.
_OPTIONAL_PROVENANCE_FIELDS = (
    "fetched_row_count",
    "source_reported_total",
    "coverage",
    # #867: absent in manifests written before it — read as canonical-json-sort-v1.
    "data_checksum_algorithm",
)


def _provenance_entry(entry: SourceProvenance) -> dict[str, Any]:
    payload = asdict(entry)
    for key in _OPTIONAL_PROVENANCE_FIELDS:
        if payload.get(key) is None:
            payload.pop(key, None)
    return payload


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """Record build manifest as deterministic JSON to disk.

    Args:
        manifest: Manifest data to record.
        output_path: Result JSON file path.

    Raises:
        ManifestError: Directory creation or file write failure.
    """
    payload = {
        "schema_version": manifest.schema_version,
        "build_id": manifest.build_id,
        # additive (#481): run's terminal state and partial output flag. Legacy manifests don't have
        # this key; reader derives status from errors presence if absent,
        # as before (manifest.status_from_manifest is the authoritative rule). Cancelled runs
        # Distinct from success/failed; goal is BuildIndex rebuild (store.rebuild_index)
        # can restore cancelled from manifest alone.
        "status": manifest.status,
        "partial": manifest.partial,
        "started_at": manifest.started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": manifest.finished_at.astimezone(timezone.utc).isoformat(),
        "build_environment": (
            asdict(manifest.build_environment) if manifest.build_environment is not None else None
        ),
        "inputs": list(manifest.inputs),
        "inputs_fingerprint": manifest.inputs_fingerprint,
        "created_by": manifest.created_by,
        # canonical stable owner identity (#505, additive) — created_by continues as legacy
        # display label. legacy consumers harmless if unaware of this key.
        "owner_id": manifest.owner_id,
        "outputs": list(manifest.outputs),
        "warnings": list(manifest.warnings),
        "errors": list(manifest.errors),
        "row_counts": manifest.row_counts,
        "schema_summaries": {
            key: asdict(summary) for key, summary in manifest.schema_summaries.items()
        },
        "provenance": [_provenance_entry(entry) for entry in manifest.provenance],
        # additive (#486): per-source_key structured quality/drift results. Empty dict default
        # so legacy consumers harmless if unaware of this key.
        "quality_results": {
            key: [asdict(r) for r in results] for key, results in manifest.quality_results.items()
        },
        "schema_drift": {
            key: [asdict(f) for f in findings] for key, findings in manifest.schema_drift.items()
        },
        # additive (#700): whether each axis was evaluated. This key is what tells an
        # empty schema_drift list apart from "there was nothing to compare against" —
        # they are not the same answer.
        "drift_evaluation": {
            key: [asdict(e) for e in entries] for key, entries in manifest.drift_evaluation.items()
        },
        # additive (#506): provenance of composition (join) execution results. composition-unused
        # run is null — legacy consumers harmless if unaware of this key.
        "composition": asdict(manifest.composition) if manifest.composition is not None else None,
    }
    # additive (#788): only when a table commit failed, so every other manifest keeps
    # its shape. A manifest saying "ok" must also say when the table did not move.
    if manifest.warehouse_failures:
        payload["warehouse_failures"] = {
            key: dict(value) for key, value in manifest.warehouse_failures.items()
        }
    # additive (#659): only when a source declares a Gold selection.
    if manifest.gold_selection:
        payload["gold_selection"] = {
            key: dict(value) for key, value in manifest.gold_selection.items()
        }
    # additive (#689): only when a Gold holds a declared PII column.
    if manifest.pii_masking:
        payload["pii_masking"] = {key: dict(value) for key, value in manifest.pii_masking.items()}
    # additive (#867): which algorithm made inputs_fingerprint; absent before it, which
    # reads as sources-sha256-v1.
    if manifest.inputs_fingerprint_algorithm is not None:
        payload["inputs_fingerprint_algorithm"] = manifest.inputs_fingerprint_algorithm
    if manifest.split_algorithm is not None:
        payload["split_algorithm"] = manifest.split_algorithm
    # additive (#867): each Gold directory's byte digest and the engine that wrote it,
    # apart from the logical data_checksum.
    if manifest.artifacts:
        payload["artifacts"] = {key: dict(value) for key, value in manifest.artifacts.items()}
    # additive (#648): only when a source resumed from a checkpoint.
    if manifest.reproducibility is not None:
        payload["reproducibility"] = dict(manifest.reproducibility)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Write as temp file in same directory, then atomic replace via
        # os.replace. truncate-then-write
        # can leave partial/corrupted manifest on crash/concurrent access (#204).
        fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=".manifest_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                _ = handle.write(f"{serialized}\n")
            os.replace(tmp_name, output_path)
        except BaseException:
            # Clean up temp file if replace fails (ignore if already replaced).
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        raise ManifestError(f"Failed to write manifest to {output_path}: {exc}") from exc


def write_manifest(manifest: BuildManifest, output_path: Path) -> None:
    """Public alias function that records build manifest.

    Args:
        manifest: Manifest object to record.
        output_path: Output path.
    """
    manifest_writer(manifest, output_path)


__all__ = ["manifest_writer", "write_manifest"]

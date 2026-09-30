"""persists Bronze stage artifacts to the execution workspace."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ...spec import JsonValue
from .._path_safety import ensure_within, validate_path_segment
from .models import BronzeArtifact, ProvenanceEvent
from .writer import canonical_line


@dataclass(frozen=True)
class BronzePersistResult:
    """filesystem path recorded for Bronze artifacts."""

    bronze_dir: Path
    records_path: Path
    metadata_path: Path


def _artifact_id(artifact: BronzeArtifact) -> str:
    """generates a short deterministic ID from source_key and fetch_params."""
    key_material = json.dumps(
        {"source_key": artifact.source_key, "fetch_params": artifact.fetch_params},
        sort_keys=True,
    )
    return hashlib.sha256(key_material.encode()).hexdigest()[:12]


def persist_bronze_artifact(
    artifact: BronzeArtifact,
    *,
    output_root: Path,
    run_id: str,
) -> BronzePersistResult:
    """records raw records and metadata to output_root/{run_id}/bronze/{source_key}/{artifact_id}"""
    validate_path_segment(run_id, field_name="run_id")

    # normalizes source_key for filesystem (e.g., "datago.apt_trade" -> "datago.apt_trade").
    source_key_segment = artifact.source_key.replace("/", "_")
    validate_path_segment(source_key_segment, field_name="source_key")

    artifact_id = _artifact_id(artifact)

    bronze_dir = output_root / run_id / "bronze" / source_key_segment / artifact_id
    records_path = bronze_dir / "raw_records.jsonl"
    metadata_path = bronze_dir / "metadata.json"

    ensure_within(output_root, bronze_dir, label="bronze directory")

    # Atomic write: write to temp dir, then rename to final location
    import shutil
    import tempfile

    from .._atomic import atomic_replace_dir

    parent = bronze_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=parent, prefix=f".{artifact_id}_tmp_"))
    try:
        tmp_records = tmp_dir / "raw_records.jsonl"
        tmp_metadata = tmp_dir / "metadata.json"

        # Streamed from the working copy (#622): one record in memory at a time, the
        # same sorted-key bytes as ever.
        with tmp_records.open("w", encoding="utf-8") as f:
            for record in artifact.iter_records():
                # allow_nan=False: NaN/Infinity are non-standard JSON tokens, so fail recording
                # instead raise ValueError (#201).
                f.write(canonical_line(record))
                f.write("\n")

        metadata = _metadata_for_artifact(
            artifact,
            records_path=records_path,
            metadata_path=metadata_path,
            bronze_dir=bronze_dir,
        )
        tmp_metadata.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )

        # Atomic swap: replaces existing directory without data loss (#180).
        atomic_replace_dir(tmp_dir, bronze_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    return BronzePersistResult(
        bronze_dir=bronze_dir,
        records_path=records_path,
        metadata_path=metadata_path,
    )


def _metadata_for_artifact(
    artifact: BronzeArtifact,
    *,
    records_path: Path,
    metadata_path: Path,
    bronze_dir: Path,
) -> dict[str, JsonValue]:
    """constructs metadata payload for BronzeArtifact."""
    provenance = artifact.provenance
    return {
        "source_key": artifact.source_key,
        "fetch_params": artifact.fetch_params,
        "fetched_at": _format_datetime(artifact.fetched_at),
        "provenance": _provenance_to_dict(provenance) if provenance else None,
        "record_count": artifact.record_count,
        "artifact_paths": {
            "records": str(records_path.relative_to(bronze_dir)),
            "metadata": str(metadata_path.relative_to(bronze_dir)),
        },
    }


def _provenance_to_dict(provenance: ProvenanceEvent) -> dict[str, JsonValue]:
    """converts ProvenanceEvent to JSON-serializable dict."""
    return {
        "operation": provenance.operation,
        "source_key": provenance.source_key,
        "fetch_params": provenance.fetch_params,
        "fetched_at": _format_datetime(provenance.fetched_at),
    }


def _format_datetime(value: datetime) -> str:
    """converts datetime values to ISO 8601 strings."""
    return value.isoformat()

"""shared reader for Bronze/Silver/Gold stage artifacts safely (#488).

consumers like Studio must know actual file layouts (e.g., `{run}/bronze/{source_key}/
{artifact_id}/raw_records.jsonl`) to query stage state or preview. builder storage is
tightly coupled. this module encapsulates that knowledge so service layer gets state/summary
from only source_key and stage name.

does not infer success from filesystem existence alone—only when complete sidecar file set
exists considers it "completed"; partial directory presence = "unavailable" (corrupted/legacy),
prior stage incomplete = "not_run", manifest shows failure but complete output missing = "failed".

main components:
    - StageStatus: completed/failed/not_run/unavailable
    - SourceStageSummary: one source's 3-stage status summary
    - compute_run_stage_summary: compute stage status for all sources in run
    - read_bronze_summary / read_silver_summary / read_gold_summary: safe detailed reads
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ..spec import JsonValue
from ._path_safety import ensure_within, validate_path_segment

StageStatus = Literal["completed", "failed", "not_run", "unavailable"]

_SILVER_SIDECAR_FILES = ("schema.json", "stats.json", "preview.json", "validation.json")


def sanitize_source_segment(source_key: str) -> str:
    """same source_key filename normalization rules as Bronze/Silver persist
    (see bronze/persist.py, silver/persist.py). Gold uses source_key as-is for dataset_name."""
    return source_key.replace("/", "_")


def bronze_source_dir(output_root: Path, run_id: str, source_key: str) -> Path:
    """path to {output_root}/{run_id}/bronze/{sanitized source_key}. raises ValueError if unsafe."""
    segment = sanitize_source_segment(source_key)
    validate_path_segment(segment, field_name="source_key")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    stage_dir = run_dir / "bronze" / segment
    ensure_within(output_root, stage_dir, label="bronze source directory")
    return stage_dir


def silver_source_dir(output_root: Path, run_id: str, source_key: str) -> Path:
    """Path to {output_root}/{run_id}/silver/{sanitized source_key}. Raises ValueError if unsafe."""
    segment = sanitize_source_segment(source_key)
    validate_path_segment(segment, field_name="source_key")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    stage_dir = run_dir / "silver" / segment
    ensure_within(output_root, stage_dir, label="silver source directory")
    return stage_dir


def gold_source_dir(output_root: Path, run_id: str, source_key: str) -> Path:
    """Path to {output_root}/{run_id}/gold/{source_key}. Gold persist does not replace slashes,
    using source_key(=dataset_name) as-is (see gold/persist.py), so we do the same.
    Raises ValueError if unsafe."""
    validate_path_segment(source_key, field_name="source_key")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    stage_dir = run_dir / "gold" / source_key
    ensure_within(output_root, stage_dir, label="gold source directory")
    return stage_dir


def _read_json(path: Path) -> JsonValue | None:
    """Safely read JSON file. Returns None if missing or corrupt (unavailable vs crash)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        return None


def _select_latest_bronze_artifact(candidate_dirs: Sequence[Path]) -> Path | None:
    """Deterministically select one artifact when multiple artifact_id candidates exist
    under the same source (#488).

    Reads fetched_at from each candidate's metadata.json and selects the most recent (ISO 8601
    strings sort lexicographically in time order). On tie, break by artifact_id (directory name)
    descending. Candidates missing readable metadata.json are excluded from consideration—no
    arbitrary selection.
    """
    scored: list[tuple[str, str, Path]] = []
    for candidate in candidate_dirs:
        meta = _read_json(candidate / "metadata.json")
        if not isinstance(meta, dict):
            continue
        if not (candidate / "raw_records.jsonl").is_file():
            continue
        fetched_at = meta.get("fetched_at")
        sort_key = fetched_at if isinstance(fetched_at, str) else ""
        scored.append((sort_key, candidate.name, candidate))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return scored[0][2]


def _bronze_artifact_dir(output_root: Path, run_id: str, source_key: str) -> Path | None:
    try:
        src_dir = bronze_source_dir(output_root, run_id, source_key)
    except ValueError:
        return None
    if not src_dir.is_dir():
        return None
    candidates = [child for child in src_dir.iterdir() if child.is_dir()]
    return _select_latest_bronze_artifact(candidates)


def _silver_complete(output_root: Path, run_id: str, source_key: str) -> tuple[bool, bool]:
    """Return (is_complete, directory_exists)."""
    try:
        d = silver_source_dir(output_root, run_id, source_key)
    except ValueError:
        return False, False
    if not d.is_dir():
        return False, False
    complete = (d / "table.parquet").is_file() and all(
        (d / name).is_file() and _read_json(d / name) is not None for name in _SILVER_SIDECAR_FILES
    )
    return complete, True


def _gold_complete(output_root: Path, run_id: str, source_key: str) -> tuple[bool, bool]:
    """Return (is_complete, directory_exists)."""
    try:
        d = gold_source_dir(output_root, run_id, source_key)
    except ValueError:
        return False, False
    if not d.is_dir():
        return False, False
    package_path = d / "package.json"
    complete = (
        (d / "table.parquet").is_file()
        and package_path.is_file()
        and _read_json(package_path) is not None
    )
    return complete, True


def _stage_status(
    *, complete: bool, dir_exists: bool, upstream_completed: bool, source_failed: bool
) -> StageStatus:
    """Determine single stage status.

    Priority: if complete outputs exist → completed. If directory exists but incomplete →
    unavailable (corrupted/legacy format). If upstream stage did not complete → not_run
    (this stage was never attempted). If manifest records this source as failed → failed.
    Otherwise (unknown state) conservatively mark as not_run.
    """
    if complete:
        return "completed"
    if dir_exists:
        return "unavailable"
    if not upstream_completed:
        return "not_run"
    if source_failed:
        return "failed"
    return "not_run"


@dataclass(frozen=True)
class SourceStageSummary:
    """Bronze/Silver/Gold status summary for a single source."""

    source_key: str
    bronze: StageStatus
    silver: StageStatus
    gold: StageStatus


def compute_run_stage_summary(
    output_root: Path,
    run_id: str,
    source_keys: Sequence[str],
    failed_source_keys: frozenset[str],
) -> list[SourceStageSummary]:
    """Compute Bronze/Silver/Gold status for each source known to run."""
    results: list[SourceStageSummary] = []
    for source_key in source_keys:
        source_failed = source_key in failed_source_keys

        bronze_artifact = _bronze_artifact_dir(output_root, run_id, source_key)
        try:
            bronze_dir_exists = bronze_source_dir(output_root, run_id, source_key).is_dir()
        except ValueError:
            bronze_dir_exists = False
        bronze_status = _stage_status(
            complete=bronze_artifact is not None,
            dir_exists=bronze_dir_exists,
            upstream_completed=True,
            source_failed=source_failed,
        )

        silver_complete, silver_dir_exists = _silver_complete(output_root, run_id, source_key)
        silver_status = _stage_status(
            complete=silver_complete,
            dir_exists=silver_dir_exists,
            upstream_completed=(bronze_status == "completed"),
            source_failed=source_failed,
        )

        gold_complete, gold_dir_exists = _gold_complete(output_root, run_id, source_key)
        gold_status = _stage_status(
            complete=gold_complete,
            dir_exists=gold_dir_exists,
            upstream_completed=(silver_status == "completed"),
            source_failed=source_failed,
        )

        results.append(
            SourceStageSummary(
                source_key=source_key, bronze=bronze_status, silver=silver_status, gold=gold_status
            )
        )
    return results


@dataclass(frozen=True)
class BronzeSummary:
    """Safely exposable Bronze summary. Does not include fetch_params or provenance raw text."""

    fetched_at: str | None
    record_count: int | None


def read_bronze_summary(output_root: Path, run_id: str, source_key: str) -> BronzeSummary | None:
    """Read safe summary of selected Bronze artifact only.

    Never returns fetch_params, provenance.fetch_params (secret possible), or artifact_paths
    (internal file layout).
    """
    artifact_dir = _bronze_artifact_dir(output_root, run_id, source_key)
    if artifact_dir is None:
        return None
    meta = _read_json(artifact_dir / "metadata.json")
    if not isinstance(meta, dict):
        return None
    fetched_at = meta.get("fetched_at")
    record_count = meta.get("record_count")
    return BronzeSummary(
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        record_count=record_count if isinstance(record_count, int) else None,
    )


@dataclass(frozen=True)
class SilverSummary:
    """Safely exposable Silver summary. Sample already capped by limit passed by caller."""

    row_count: int | None
    schema: list[JsonValue]
    statistics: JsonValue
    validation: JsonValue
    sample: list[JsonValue]
    sample_total_available: int


def read_silver_summary(
    output_root: Path, run_id: str, source_key: str, *, sample_limit: int
) -> SilverSummary | None:
    """Read safe summary from schema.json/stats.json/validation.json/preview.json.

    Does not read the full Parquet file—sample always comes from preview.json, already
    persisted at persist time within the limit (build_silver_dataset's preview_limit).
    """
    try:
        d = silver_source_dir(output_root, run_id, source_key)
    except ValueError:
        return None
    if not d.is_dir():
        return None
    schema = _read_json(d / "schema.json")
    stats = _read_json(d / "stats.json")
    validation = _read_json(d / "validation.json")
    preview = _read_json(d / "preview.json")
    if schema is None or stats is None or validation is None or preview is None:
        return None

    columns = schema.get("columns") if isinstance(schema, dict) else None
    row_count = stats.get("row_count") if isinstance(stats, dict) else None
    rows = preview.get("rows") if isinstance(preview, dict) else None
    all_rows = rows if isinstance(rows, list) else []
    capped_limit = max(sample_limit, 0)
    sample = all_rows[:capped_limit]

    return SilverSummary(
        row_count=row_count if isinstance(row_count, int) else None,
        schema=columns if isinstance(columns, list) else [],
        statistics=stats,
        validation=validation,
        sample=sample,
        sample_total_available=len(all_rows),
    )


@dataclass(frozen=True)
class GoldSummary:
    """Safely exposable Gold summary. Does not include export options, output_path, or
    credentials."""

    row_count: int | None
    columns: list[str]
    splits: dict[str, int] | None
    export_kinds: list[str]


def read_gold_summary(output_root: Path, run_id: str, source_key: str) -> GoldSummary | None:
    """Read safe summary from package.json only.

    Does not return export_plan.targets[].options (credential possible) or output_path—only
    kind is exposed. No Gold sample sidecar yet, so no sample is generated (caller expresses
    sample_available=false).
    """
    try:
        d = gold_source_dir(output_root, run_id, source_key)
    except ValueError:
        return None
    if not d.is_dir():
        return None
    package = _read_json(d / "package.json")
    if not isinstance(package, dict):
        return None

    row_count = package.get("row_count")
    columns = package.get("columns")
    splits_raw = package.get("splits")
    splits: dict[str, int] | None = None
    if isinstance(splits_raw, dict):
        splits = {str(name): count for name, count in splits_raw.items() if isinstance(count, int)}

    kinds: list[str] = []
    export_plan = package.get("export_plan")
    if isinstance(export_plan, dict):
        targets = export_plan.get("targets")
        if isinstance(targets, list):
            kinds = [
                kind
                for target in targets
                if isinstance(target, dict) and isinstance(kind := target.get("kind"), str)
            ]

    return GoldSummary(
        row_count=row_count if isinstance(row_count, int) else None,
        columns=[c for c in columns if isinstance(c, str)] if isinstance(columns, list) else [],
        splits=splits,
        export_kinds=kinds,
    )


__all__ = [
    "BronzeSummary",
    "GoldSummary",
    "SilverSummary",
    "SourceStageSummary",
    "StageStatus",
    "bronze_source_dir",
    "compute_run_stage_summary",
    "gold_source_dir",
    "read_bronze_summary",
    "read_gold_summary",
    "read_silver_summary",
    "sanitize_source_segment",
    "silver_source_dir",
]

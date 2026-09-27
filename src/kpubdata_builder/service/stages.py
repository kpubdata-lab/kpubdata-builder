"""Bronze/Silver/Gold stage summary/detail service logic (#488).

Contains pure read-only logic used by ``GET /builds/{run_id}/stages`` and
``GET /builds/{run_id}/stages/{stage}``. Run_id format validation, existence check,
and ownership gating are first handled by dispatch/BuilderService in
service/app.py; functions in this module start after that (from trusted run_id).

Knowledge of stage artifact file layout is encapsulated in ``stages._stage_reader``;
this module combines manifest (canonical) with that reader to build per-run
responses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from ..spec import BuildSpec, SourceRef
from ..stages._stage_reader import (
    BronzeSummary,
    GoldSummary,
    SilverSummary,
    SourceStageSummary,
    StageStatus,
    compute_run_stage_summary,
    read_bronze_summary,
    read_gold_summary,
    read_silver_summary,
)
from ..stages.bronze.resolve import source_identity

Stage = Literal["bronze", "silver", "gold"]
STAGE_NAMES: tuple[Stage, ...] = ("bronze", "silver", "gold")

# Defensive upper bound on Silver sample response limit. Actual return size is
# already persisted in preview.json at build time (DEFAULT_PREVIEW_LIMIT, default
# 5 rows) so is always ≤ this, but absurdly large limit requests themselves are
# rejected with clear 400.
MAX_STAGE_PREVIEW_LIMIT = 1000
DEFAULT_STAGE_PREVIEW_LIMIT = 5


def known_source_keys(manifest: dict[str, object]) -> tuple[str, ...]:
    """Retrieve list of source_keys (output-facing keys) known to this run from manifest.inputs.

    Only sources the orchestrator actually attempted are recorded (success/failure
    notwithstanding); checking if source query parameter is in this list validates
    it is known before concatenating into filesystem path. Extremely old legacy
    manifests without inputs field return empty tuple — no guessing.
    """
    inputs = manifest.get("inputs")
    if not isinstance(inputs, list):
        return ()
    return tuple(item for item in inputs if isinstance(item, str))


def failed_source_keys(manifest: dict[str, object]) -> frozenset[str]:
    """Extract set of failed source_keys from manifest.errors
    (``["{source_key}: {message}", ...]``)."""
    errors = manifest.get("errors")
    if not isinstance(errors, list):
        return frozenset()
    keys: set[str] = set()
    for entry in errors:
        if not isinstance(entry, str):
            continue
        prefix, sep, _rest = entry.partition(": ")
        if sep:
            keys.add(prefix)
    return frozenset(keys)


def list_run_stages(
    output_root: Path, run_id: str, manifest: dict[str, object]
) -> list[SourceStageSummary]:
    """Calculate Bronze/Silver/Gold status for all known sources in run."""
    sources = known_source_keys(manifest)
    failed = failed_source_keys(manifest)
    return compute_run_stage_summary(output_root, run_id, sources, failed)


def stage_status_for_source(
    output_root: Path, run_id: str, manifest: dict[str, object], source_key: str
) -> SourceStageSummary | None:
    """Calculate stage status for single source. Returns None if not known source."""
    if source_key not in known_source_keys(manifest):
        return None
    failed = failed_source_keys(manifest)
    results = compute_run_stage_summary(output_root, run_id, (source_key,), failed)
    return results[0] if results else None


def stage_status_of(summary: SourceStageSummary, stage: Stage) -> StageStatus:
    if stage == "bronze":
        return summary.bronze
    if stage == "silver":
        return summary.silver
    return summary.gold


def _output_source_key(source: SourceRef) -> str:
    """Mirror the same rule as pipeline.orchestrator._output_source_key.

    Stage retrieval must find sources using the same output-facing key (alias first,
    else canonical identity per kind) that the pipeline used when writing files.
    file/url kind (#498) have empty provider/dataset, so orchestrator fills identity
    using the same source_identity().
    """
    if source.alias:
        return source.alias
    provider, dataset = source_identity(source)
    return f"{provider}.{dataset}"


def match_source_ref(spec: BuildSpec, source_key: str) -> SourceRef | None:
    """Find source from canonical snapshot matching output-facing key."""
    for source in spec.sources:
        if _output_source_key(source) == source_key:
            return source
    return None


def bronze_detail(output_root: Path, run_id: str, source_key: str) -> BronzeSummary | None:
    return read_bronze_summary(output_root, run_id, source_key)


def silver_detail(
    output_root: Path, run_id: str, source_key: str, *, limit: int
) -> SilverSummary | None:
    return read_silver_summary(output_root, run_id, source_key, sample_limit=limit)


def gold_detail(output_root: Path, run_id: str, source_key: str) -> GoldSummary | None:
    return read_gold_summary(output_root, run_id, source_key)


__all__ = [
    "DEFAULT_STAGE_PREVIEW_LIMIT",
    "MAX_STAGE_PREVIEW_LIMIT",
    "STAGE_NAMES",
    "Stage",
    "bronze_detail",
    "failed_source_keys",
    "gold_detail",
    "known_source_keys",
    "list_run_stages",
    "match_source_ref",
    "silver_detail",
    "stage_status_for_source",
    "stage_status_of",
]

"""Medallion pipeline orchestrator (#48).

Execute each source in BuildSpec in Bronze → Silver → Gold order,
persist each stage's outputs in the execution workspace, and record
the build manifest.

Partial success policy (BUILD_STATE.md): If any source fails,
the overall status is recorded as failed, but successful sources'
outputs and failure information are preserved in the manifest.

If BuildSpec.composition exists (#506), after all sources complete
Bronze/Silver/Gold, composition joins the validated Silver layers
of its two referenced sources to create a separate combined Gold
dataset (gold/{composition.name}/). Existing independent Gold per
source is preserved — composition is additive, not a replacement.

Key components:
    - SourceBuildOutcome: per-source execution result
    - CompositionOutcome: composition (join) execution result (#506)
    - BuildResult: overall execution result
    - run_build: pipeline entry point
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast

import yaml

from ..artifact import ArtifactDataset
from ..errors import DatasetValidationError, SpecLoadError, ValidationError
from ..events import BuildEventRecorder, BuildEventStore
from ..exporters import get_exporter
from ..ingestion import IngestionError
from ..manifest import (
    BuildManifest,
    CompositionProvenance,
    JoinKeyProvenance,
    SchemaSummary,
    SourceProvenance,
    build_schema_summary,
    build_source_provenance,
    capture_build_environment,
    compute_inputs_fingerprint,
    manifest_writer,
    snapshot_coverage,
)
from ..manifest.checksums import FINGERPRINT_ALGORITHM
from ..manifest.reproducibility import not_reproducible
from ..quality import (
    DriftEvaluation,
    QualityCheckResult,
    SchemaDriftFinding,
    evaluate_quality,
)
from ..spec import (
    BUILDSPEC_SNAPSHOT_FILENAME,
    BuildSpec,
    CompositionSpec,
    ExportTarget,
    JsonValue,
    SourceRef,
    parse_spec,
    write_buildspec_snapshot,
)
from ..spec.fingerprints import SourceFingerprints, fingerprint_source
from ..spec.validator import validate_spec
from ..stages.bronze.build import SourceClient
from ..stages.bronze.models import BronzeArtifact, utc_now
from ..stages.bronze.persist import persist_bronze_artifact
from ..stages.bronze.resolve import build_bronze_artifact_for_source, source_identity
from ..stages.gold.build import build_gold_package
from ..stages.gold.card import build_dataset_card, render_dataset_card
from ..stages.gold.compose import CompositionError, build_composed_gold_package
from ..stages.gold.persist import persist_gold_package
from ..stages.gold.select import GoldSelectionError, GoldSelectionResult, apply_gold_selection
from ..stages.silver.build import build_silver_dataset
from ..stages.silver.drift import (
    COVERAGE_MISMATCH,
    COVERAGE_UNKNOWN,
    SCHEMA_CONTRACT_CHANGED,
    VOLUME_FINDING_KINDS,
    DriftFinding,
    SilverBaseline,
    detect_drift,
    find_previous_silver,
)
from ..stages.silver.models import SilverDataset
from ..stages.silver.persist import persist_silver_dataset
from ..stages.silver.pii import scan_pii
from ..stages.silver.summarize import build_schema
from ..tabular import DEFAULT_PREVIEW_LIMIT
from ..tabular.polars_engine import artifact_writer, infer_schema
from ..tabular.wire import encode_rows
from ..uploads import UploadRepository
from ..warehouse import (
    MaterializeResult,
    SnapshotConflict,
    TableCatalog,
    WarehouseError,
    materialize,
)
from ..warehouse import gc as warehouse_gc
from ..warehouse.layout import content_digest
from .cancellation import BuildCancelled, CancellationProbe, raise_if_cancelled
from .context import BuildContext
from .export import export_gold_package

logger = logging.getLogger(__name__)

# Maximum threads for concurrent source execution. Per-source fetch/stage
# are mostly network I/O waits, so sequential execution time grows linearly
# with source count. Cap threads to avoid unlimited spawning (#247).
_MAX_PARALLEL_SOURCES = 4
#: Per-run directory of param_grid checkpoints (#648).
_CHECKPOINT_DIRNAME = "_checkpoints"
#: Per-run directory where each source's Bronze records are written as they arrive,
#: until the source finishes (#622).
_STAGING_DIRNAME = "_bronze_staging"


def _dataset_card_license(spec: BuildSpec) -> str:
    """Prioritize canonical license; use string legacy metadata as fallback."""
    if spec.license is not None:
        return spec.license
    legacy_license = spec.metadata.get("license")
    return legacy_license if isinstance(legacy_license, str) else ""


def _gold_package_metadata(spec: BuildSpec) -> dict[str, str]:
    """Gold package metadata — sole source seen by exporter (#629).

    The exporter sees only ``ArtifactDataset.metadata``. There were two
    places building it, causing divergence; the second overwrote the first.
    This is now the only authoritative source.

    ``license`` is included only when explicitly set. Omitting the key
    allows exporter default (CC-BY-4.0) to apply on Kaggle — a declared
    empty string differs from an omitted license.
    """
    metadata = {
        "title": spec.title,
        "description": spec.description,
        # Kaggle exporter reads the dataset id from here
        # (#550 standardization — spec.dataset_id is the publication destination).
        "dataset_id": spec.dataset_id,
    }
    if spec.attribution:
        # Public Namuh (CCPL) types 1–4 all require attribution. License ID
        # alone cannot satisfy that requirement (needs agency name, type,
        # original URL together). Captured separately and passed to card
        # as-is (ADR 0018).
        metadata["attribution"] = spec.attribution
    declared_license = _dataset_card_license(spec)
    if declared_license:
        # Without this, Kaggle dataset-metadata.json always used CC-BY-4.0
        # regardless of spec.license — the same build published different
        # licenses in dataset card vs. Kaggle metadata.
        metadata["license"] = declared_license
    # `license: other` means nothing without its name and link; the exporters carry
    # both where the platform has a place for them (#764).
    if spec.license_name:
        metadata["license_name"] = spec.license_name
    if spec.license_link:
        metadata["license_link"] = spec.license_link
    return metadata


def _dataset_card_version(spec: BuildSpec) -> str:
    """Use metadata.version only when it is a string.

    metadata accepts arbitrary JSON via ``_parse_json_mapping``, so
    passing null/number/list/dict through ``str()`` yields ``"None"``
    or ``"{...}"`` strings leaked to dataset card. Like license, treat
    non-strings as empty so ``card.version or "unversioned"`` fallback
    works correctly.
    """
    version = spec.metadata.get("version")
    return version if isinstance(version, str) else ""


@dataclass(frozen=True)
class SourceBuildOutcome:
    """Single source pipeline execution result.

    Attributes:
        source_key: source identifier.
        status: "ok", "failed", or "cancelled" (#481 — cancellation
            observed at safe stage boundary; next stage not started).
            "cancelled" is not a failure; error field left empty. Only
            created by async runs with cancellation probe (sync
            POST /build/CLI lacks probe, returns ok/failed only).
        stages_completed: successfully completed stage names in order
            (bronze/silver/gold). Cancelled sources also record actual
            completed stages to cancellation point — incomplete stages
            are not misrepresented as successful.
        error: failure message if status is "failed".
    """

    source_key: str
    status: str
    stages_completed: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CompositionOutcome:
    """Composition (join) execution result (#506).

    Separate type from SourceBuildOutcome — composition has no bronze/
    silver/gold stages (only combines two source Silvers), and must be
    clearly distinguished as "combined result" in manifest.

    Attributes:
        name: combined Gold dataset name (CompositionSpec.name).
        status: "ok" | "failed" (join itself failed) | "skipped"
            (referenced source failed; join not attempted).
        error: failure/skip reason.
    """

    name: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class BuildResult:
    """Overall build execution result.

    Attributes:
        context: execution context.
        status: overall status ("ok", "failed", or "cancelled").
            "cancelled" only appears with cancellation probe (#481) —
            sync POST /build/CLI paths return ok/failed only (traditional
            behavior).
        outcomes: per-source execution results.
        manifest_path: persisted build manifest path.
        composition_outcome: composition execution result. None if
            BuildSpec.composition absent (#506).
        materialized: committed table snapshots, keyed by source key (#703). Empty
            when no warehouse was given — which means "not attempted", never "nothing
            to commit". A caller that reads an empty mapping as success would report a
            build as materialised that never touched a catalog.
    """

    context: BuildContext
    status: str
    outcomes: tuple[SourceBuildOutcome, ...]
    manifest_path: Path
    spec_digest: str
    composition_outcome: CompositionOutcome | None = None
    materialized: dict[str, MaterializeResult] = field(default_factory=dict)
    #: Sources whose table commit failed, with the reason (#788). The build itself
    #: succeeded; the table was not moved to its output.
    warehouse_failures: dict[str, dict[str, str]] = field(default_factory=dict)


def _fetch_source_key(source: SourceRef) -> str:
    """Return Bronze fetch identity (canonical per-kind identity, #498)."""
    provider, dataset = source_identity(source)
    return f"{provider}.{dataset}"


def _output_source_key(source: SourceRef) -> str:
    """Return user-facing output key for workspace/result recording."""
    return source.alias if source.alias else _fetch_source_key(source)


def _recorded_fingerprints(run_dir: Path, output_key: str) -> SourceFingerprints | None:
    """Recompute an earlier run's source fingerprints from its spec snapshot (#700).

    The snapshot is the canonical text the fingerprints are defined over, so nothing
    new has to be stored per run and runs from before this existed are covered too.
    ``None`` when the snapshot is missing, unreadable, or no longer names this source
    — which makes the run's coverage unknown rather than assumed to match.
    """
    try:
        document = yaml.safe_load(
            (run_dir / BUILDSPEC_SNAPSHOT_FILENAME).read_text(encoding="utf-8")
        )
        if not isinstance(document, dict):
            return None
        earlier = parse_spec(document)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, SpecLoadError, ValidationError):
        return None
    for source in earlier.sources:
        if _output_source_key(source) == output_key:
            return fingerprint_source(source)
    return None


def _volume_comparability(source: SourceRef) -> Callable[[Path], str | None]:
    """Accept an earlier run as a volume baseline only if it is the same measurement.

    Same population (coverage) and same rules for reading it (schema contract). A row
    count from Seoul 2025 against one from Busan 2026 is not drift, and neither is a
    row count from before a ``casts`` change against one after it (#700).
    """
    output_key = _output_source_key(source)
    current = fingerprint_source(source)

    def check(run_dir: Path) -> str | None:
        earlier = _recorded_fingerprints(run_dir, output_key)
        if earlier is None:
            return COVERAGE_UNKNOWN
        if earlier.coverage != current.coverage:
            return COVERAGE_MISMATCH
        if earlier.schema_contract != current.schema_contract:
            return SCHEMA_CONTRACT_CHANGED
        return None

    return check


def _artifact_digests(
    context: BuildContext, keys: Sequence[str]
) -> dict[str, dict[str, JsonValue]]:
    """Each written Gold directory's byte digest and writer (#867).

    The digest is ``warehouse.layout.content_digest`` — the same function, and so the
    same meaning, as a warehouse snapshot's ``artifact_digest``.
    """
    writer = cast(JsonValue, artifact_writer())
    digests: dict[str, dict[str, JsonValue]] = {}
    for key in keys:
        gold_dir = context.output_root / context.run_id / "gold" / key
        if gold_dir.is_dir():
            digests[key] = {"artifact_digest": content_digest(gold_dir), "artifact_writer": writer}
    return digests


def _retag_bronze_artifact(artifact: BronzeArtifact, *, output_key: str) -> BronzeArtifact:
    """Keep fetch provenance; replace only source_key for output paths."""
    return replace(artifact, source_key=output_key)


def _record_output_paths(outputs: list[str], *paths: Path) -> None:
    """Record all generated output paths to manifest outputs."""
    outputs.extend(str(path) for path in paths)


def _quality_failure_messages(fail_results: Sequence[QualityCheckResult]) -> list[str]:
    """Convert FAIL-rated QualityCheckResult to DatasetValidationError message (#486)."""
    messages: list[str] = []
    for r in fail_results:
        location = f" @ {r.column}" if r.column else ""
        detail = f", detail={r.detail!r}" if r.detail else ""
        messages.append(
            f"quality check failed: {r.rule}{location} (actual={r.actual!r}, "
            f"threshold={r.threshold!r}{detail})"
        )
    return messages


def _to_schema_drift_findings(findings: Sequence[DriftFinding]) -> tuple[SchemaDriftFinding, ...]:
    """Convert deterministic DriftFinding to API/manifest SchemaDriftFinding (#486)."""
    return tuple(
        SchemaDriftFinding(kind=f.kind, column=f.column, detail=f.detail) for f in findings
    )


def _execute_exports(
    gold_dir: Path,
    artifact: ArtifactDataset,
    exports: tuple[ExportTarget, ...],
) -> list[Path]:
    """Execute exporters and return generated file paths.

    Args:
        gold_dir: Gold package directory.
        artifact: assembled output for exporters to consume.
        exports: list of export targets.

    Returns:
        List of generated file paths.

    """
    output_paths: list[Path] = []
    for export_target in exports:
        exporter = get_exporter(export_target.kind)
        result = exporter.export(artifact, export_target, gold_dir)
        output_paths.append(result.output_path)
        logger.info(
            "exported %s to %s (size: %d bytes)",
            export_target.kind,
            result.output_path,
            result.file_size,
        )
    return output_paths


@dataclass(frozen=True)
class _SourcePipelineResult:
    """Local result of single-source pipeline execution.

    When running multiple sources concurrently via thread pool (#247),
    _run_source_pipeline returns its results locally instead of directly
    touching shared mutable state (outputs/row_counts/schema_summaries/
    provenance). Merging happens in run_build after all threads complete,
    in a single thread.
    """

    outcome: SourceBuildOutcome
    output_paths: tuple[str, ...] = ()
    row_count: int | None = None
    schema_summary: SchemaSummary | None = None
    provenance_entry: SourceProvenance | None = None
    quality_results: tuple[QualityCheckResult, ...] = ()
    quality_evaluated: bool = False
    schema_drift: tuple[SchemaDriftFinding, ...] = ()
    # Whether drift was compared at all, separate from what it found (#700). An
    # empty schema_drift alone cannot tell "nothing changed" from "no baseline".
    drift_evaluation: tuple[DriftEvaluation, ...] = ()
    silver: SilverDataset | None = None
    #: What ``sources[].gold`` did to this source (#659); None when it declares none.
    gold_selection: GoldSelectionResult | None = None
    #: ``(resumed, total)`` param_grid combinations when this source resumed from a
    #: checkpoint (#648); None when every combination was fetched in this run.
    resumed: tuple[int, int] | None = None


def _run_source_pipeline(
    source: SourceRef,
    *,
    client: SourceClient,
    context: BuildContext,
    recorder: BuildEventRecorder,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
    baseline_owner_id: str | None = None,
    capture_silver: bool = False,
    cancellation: CancellationProbe | None = None,
    secret_values: tuple[str, ...] = (),
) -> _SourcePipelineResult:
    """Execute one source Bronze → Silver → Gold and persist outputs.

    Instead of accepting shared mutable containers, collects results
    locally and returns them, so this can be called concurrently (from

    ``upload_repository``/``owner_id`` are only used by ``kind="file"``
    sources (#498) — stable principal id needed for upload ownership

    ``recorder`` emits structured events only at this source's actual
    execution boundaries (fetch/stage/quality) (#496) — incomplete stages
    are not misrepresented as done. Whether each stage completed is still
    tracked via ``completed`` list; stages like export (reflected in
    ``completed``) use ``export_started`` flag for separate tracking —
    preserving existing partial-run outcome contract (``stages_completed``)

    Args:
        capture_silver: if True, include validated SilverDataset in
            result. Only enabled when composition (#506) references this
            source; general builds without composition don't hold Silver
        cancellation: cooperative cancellation probe (#481). If None
            (sync POST /build, CLI), no cancellation check occurs —
            100% identical to existing behavior. If probe present, check
            **only at safe stage boundaries** — don't interrupt running
            stage (no partial objects left), check only after prior
    """
    output_key = _output_source_key(source)
    completed: list[str] = []
    outputs: list[str] = []
    provenance_entry: SourceProvenance | None = None
    evaluated_row_count: int | None = None
    captured_silver: SilverDataset | None = None
    # Structured Quality/Schema results survive exceptions (schema validation
    # failure, quality FAIL, etc.) and outlive the source failure outcome
    # (#486) — quality_results won't be lost due to exceptions below.
    quality_results: tuple[QualityCheckResult, ...] = ()
    quality_evaluated = False
    schema_drift: tuple[SchemaDriftFinding, ...] = ()
    drift_evaluation: tuple[DriftEvaluation, ...] = ()
    # Track export stage progress separately from completed list (#496) —
    # export runs after gold completion but isn't reflected in existing
    # outcome model (stages_completed). fetch_completed separately tracks
    # whether fetch succeeded vs. bronze persist success (persist can fail
    # *after* fetch), distinguishing success boundaries.
    export_started = False
    fetch_completed = False
    gold_selection: GoldSelectionResult | None = None
    resumed_combinations = 0
    total_combinations = 0
    try:
        # Boundary 0 (#481): This source hasn't started yet. Waiting in worker
        # pool (#247, max 4) and about to start; if cancellation was already
        # requested, skip fetch itself — don't start new remote I/O after cancel.
        raise_if_cancelled(cancellation)
        # Resolve raw records using resolver matching kind (public_api/file/url)
        # (#498) — Silver/Gold need not know kind afterward. Bronze stage and
        # source fetch share this call as execution boundary (#496) —
        # public_api/file/url all use identical event vocabulary.
        recorder.stage_started(output_key, "bronze")
        recorder.source_fetch_started(output_key)

        def after_combination(done: int, total: int) -> None:
            # Each finished combination is a safe boundary (#648): report it, then stop
            # if cancellation was asked for. Raising here abandons the fetch before
            # Bronze is written, so a cancelled run keeps no half-fetched source.
            recorder.source_fetch_progress(output_key, done=done, total=total)
            if done < total:
                raise_if_cancelled(cancellation)

        # A param_grid fetch keeps each finished combination here and a rebuild of
        # the same run resumes from it (#648). Removed once Bronze is written.
        run_dir = context.output_root / context.run_id
        checkpoint_path = run_dir / _CHECKPOINT_DIRNAME / output_key
        # The single-file checkpoint of earlier versions cannot be resumed from (#622).
        (run_dir / _CHECKPOINT_DIRNAME / f"{output_key}.jsonl").unlink(missing_ok=True)
        # Records are written here as they arrive (#622), and read from here by Silver.
        # Anything a crashed attempt left is removed first.
        staging_dir = run_dir / _STAGING_DIRNAME / output_key
        shutil.rmtree(staging_dir, ignore_errors=True)
        bronze = build_bronze_artifact_for_source(
            source,
            client=client,
            upload_repository=upload_repository,
            owner_id=owner_id,
            secret_values=secret_values,
            on_combination_done=after_combination,
            checkpoint_path=checkpoint_path,
            staging_dir=staging_dir,
        )
        recorder.source_fetch_completed(output_key, record_count=bronze.record_count)
        fetch_completed = True
        bronze = _retag_bronze_artifact(bronze, output_key=output_key)
        bronze_paths = persist_bronze_artifact(
            bronze, output_root=context.output_root, run_id=context.run_id
        )
        shutil.rmtree(checkpoint_path, ignore_errors=True)
        resumed_combinations = bronze.resumed_combinations
        total_combinations = len(bronze.call_totals)
        completed.append("bronze")
        recorder.stage_completed(
            output_key,
            "bronze",
            message="Bronze written",
            metrics={"records": bronze.record_count},
        )
        _record_output_paths(outputs, bronze_paths.records_path, bronze_paths.metadata_path)
        # Finalize immediately after bronze success: even if later stages fail
        # (partial failure), provenance survives (same as pre-parallelization
        # immediate append to shared list) (#247).
        provenance_provider, provenance_dataset = source_identity(source)
        provenance_entry = build_source_provenance(
            provider=provenance_provider,
            dataset=provenance_dataset,
            fetched_at=bronze.fetched_at,
            # The persisted file, checksummed without loading it (#622).
            records_path=bronze_paths.records_path,
            record_count=bronze.record_count,
            # bronze.fetch_params is already scrubbed of secret/path by
            # kind-specific resolver (#498) — file has upload_id/format/
            # encoding, url has endpoint/method without query string,
            # public_api has original source.params unchanged.
            params=bronze.fetch_params,
            # Reported totals are kept per call, never summed (#816).
            call_totals=bronze.call_totals,
        )

        # Boundary 1 (#481): Bronze outputs written to disk and provenance
        # finalized. Cancellation observed here preserves Bronze, skips Silver.
        raise_if_cancelled(cancellation)

        recorder.stage_started(output_key, "silver")
        required_columns = source.schema.required if source.schema else ()
        column_dtypes = source.schema.dtypes if source.schema else None
        silver = build_silver_dataset(
            bronze,
            required_columns=required_columns,
            casts=source.schema.casts if source.schema else None,
            rename=source.schema.rename if source.schema else None,
            derived=source.schema.derived if source.schema else (),
            read_as=source.schema.read_as if source.schema else None,
            null_tokens=source.schema.null_tokens if source.schema else (),
            column_null_tokens=(source.schema.column_null_tokens if source.schema else None),
            coalesce=source.schema.coalesce if source.schema else None,
            zfill=source.schema.zfill if source.schema else None,
            column_dtypes=column_dtypes,
        )
        evaluated_row_count = silver.statistics.row_count

        # Structured Quality/Schema evaluation (#486). Uses same common
        # evaluator as Preview — if exception occurs below, quality_results
        # already filled, surviving in failure outcome (#486).
        quality_results = evaluate_quality(
            silver,
            context.spec.quality,
            source_key=output_key,
            required_columns=required_columns,
            column_dtypes=column_dtypes,
        )
        quality_evaluated = True
        # Quality checkpoint event (#496) reflects #486 verdict — even if FAIL
        # causes source failure below, evaluation actually occurred and results
        # already recorded (partial-run visibility).
        recorder.quality_evaluated(output_key, quality_results)

        # Fail source if Silver validation failed. Validation is now a gate,
        # not advisory (#189). Existing error message contract preserved for
        # backward compatibility.
        if not silver.validation.ok:
            # Convert ValidationProblem objects to string list expected by
            # DatasetValidationError (#261)
            problem_messages = [problem.message for problem in silver.validation.problems]
            raise DatasetValidationError(problem_messages)

        # PII scan gate (#441, QG-1). Original values not included in
        # results/logs. block: fail on detection, warn: manifest/log warning,
        # allow: pass through.
        if context.spec.pii is not None:
            findings = [
                f for f in scan_pii(silver.table) if f.column not in context.spec.pii.allow_columns
            ]
            if findings:
                if context.spec.pii.mode == "block":
                    raise DatasetValidationError(
                        [f"PII 검출({f.kind}) @ {f.column}: {f.count}건" for f in findings]
                    )
                if context.spec.pii.mode == "warn":
                    logger.warning(
                        "PII 의심 컬럼 (warn): %s",
                        ", ".join(f"{f.kind}@{f.column}({f.count})" for f in findings),
                    )

        # Quality WARN/FAIL gate (#446, #486). WARN logs only, continues;
        # FAIL halts source before Gold entry. quality_results already filled
        # above, survives even if raise here.
        fail_results = [r for r in quality_results if r.status == "fail"]
        if fail_results:
            raise DatasetValidationError(_quality_failure_messages(fail_results))
        for r in quality_results:
            if r.status == "warn":
                logger.warning(
                    "품질 위반(warn): %s%s actual=%s threshold=%s (#486)",
                    r.rule,
                    f" @ {r.column}" if r.column else "",
                    r.actual,
                    r.threshold,
                )

        # Drift detection (#445, DRIFT-1). Compared only against earlier successful
        # runs of the same dataset_id and source_key, so another dataset's silver
        # cannot manufacture drift (#486), and only against the same owner (#700):
        # another user's run as the baseline turns row count and schema changes into
        # a metadata side channel.
        #
        # The two axes do not share a baseline (#700). Columns should not depend on
        # which region was collected, so the schema axis takes the newest owned run.
        # A row count only means something against the same population read under
        # the same contract, so the volume axis takes the newest owned run that
        # matches both — and reports why when none does, instead of comparing.
        baseline = find_previous_silver(
            context.output_root,
            context.run_id,
            dataset_id=context.spec.dataset_id,
            source_key=output_key,
            owner_id=baseline_owner_id,
        )
        volume_baseline = find_previous_silver(
            context.output_root,
            context.run_id,
            dataset_id=context.spec.dataset_id,
            source_key=output_key,
            owner_id=baseline_owner_id,
            comparable=_volume_comparability(source),
        )
        drift_findings: list[DriftFinding] = []
        evaluations: list[DriftEvaluation] = []
        for axis, found in (("schema", baseline), ("volume", volume_baseline)):
            if isinstance(found, SilverBaseline):
                drift_findings.extend(
                    f
                    for f in detect_drift(
                        silver.schema, silver.statistics, found.schema, found.stats
                    )
                    if (f.kind in VOLUME_FINDING_KINDS) == (axis == "volume")
                )
                evaluations.append(
                    DriftEvaluation(
                        axis=axis,
                        evaluated=True,
                        baseline_snapshot_id=found.run_id,
                        detail=f"compared against run {found.run_id}",
                    )
                )
            else:
                # Leaving the findings empty drops the key from the manifest, and then
                # "there was no baseline" and "compared, nothing changed" are the same
                # answer on the wire (#700). Record the failure to evaluate explicitly.
                evaluations.append(
                    DriftEvaluation(
                        axis=axis, evaluated=False, reason=found.reason, detail=found.detail
                    )
                )
                logger.info("드리프트 미평가(%s): %s — %s (#700)", axis, output_key, found.detail)
        schema_drift = _to_schema_drift_findings(drift_findings)
        for f in drift_findings:
            logger.warning("드리프트 감지: %s @ %s — %s (#445)", f.kind, f.column, f.detail)
        drift_evaluation = tuple(evaluations)

        silver_paths = persist_silver_dataset(
            silver, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("silver")
        recorder.stage_completed(
            output_key,
            "silver",
            message="Silver written",
            metrics={"row_count": evaluated_row_count},
        )
        # Capture Silver only after passing all gates (schema/PII/quality) —
        # composition (#506) uses this value directly for join, so validated
        # data only.
        if capture_silver:
            captured_silver = silver
        _record_output_paths(
            outputs,
            silver_paths.table_path,
            silver_paths.schema_path,
            silver_paths.stats_path,
            silver_paths.preview_path,
            silver_paths.validation_path,
        )

        # Boundary 2 (#481): All Silver outputs written. Cancellation observed
        # preserves Bronze+Silver, skips Gold.
        raise_if_cancelled(cancellation)

        recorder.stage_started(output_key, "gold")
        # The published shape is decided here, not in Silver (#659): Silver keeps
        # every column and row, and quality above was measured on it.
        gold_input = silver
        if source.gold is not None:
            selected, gold_selection = apply_gold_selection(silver.table, source.gold)
            gold_input = replace(silver, table=selected)
        gold = build_gold_package(
            gold_input,
            dataset_name=output_key,
            exports=context.spec.exports,
            metadata=_gold_package_metadata(context.spec),
            splits_spec=context.spec.splits,
        )
        gold_paths = persist_gold_package(
            gold, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("gold")
        recorder.stage_completed(
            output_key, "gold", message="Gold written", metrics={"row_count": len(gold.table)}
        )
        _record_output_paths(
            outputs,
            gold_paths.table_path,
            gold_paths.package_path,
            *gold_paths.splits_paths.values(),
        )

        # Boundary 3 (#481): Gold outputs written, before export starts.
        # All medallion stages complete; export remains.
        raise_if_cancelled(cancellation)

        # export stage (#496): SourceBuildOutcome.stages_completed unchanged
        # (existing model per #488 stage API), but actual export execution
        # boundary records started/completed/failed separately.
        export_started = True
        recorder.stage_started(output_key, "export")
        export_paths = export_gold_package(gold, output_dir=gold_paths.gold_dir)
        _record_output_paths(outputs, *export_paths)

        card = build_dataset_card(
            title=context.spec.title,
            description=context.spec.description,
            sources=(output_key,),
            fields=(
                (column.name, column.dtype, column.nullable)
                for column in (
                    silver.schema.columns
                    if gold_selection is None
                    else infer_schema(gold.table).columns
                )
            ),
            # A card describes what is published: with a selection, sample rows come
            # from Gold, so a dropped column or row never appears in it.
            sample_rows=(
                silver.preview.rows
                if gold_selection is None
                else tuple(
                    encode_rows(
                        gold.table.head(len(silver.preview.rows)).to_dicts(),
                        infer_schema(gold.table).columns,
                    )
                )
            ),
            license=_dataset_card_license(context.spec),
            version=_dataset_card_version(context.spec),
        )
        card_path = gold_paths.gold_dir / "README.md"
        _ = card_path.write_text(render_dataset_card(card), encoding="utf-8")
        _record_output_paths(outputs, card_path)

        # BuildSpec.exports fully executed above by export_gold_package —
        # package.export_plan.targets is spec.exports (#629). Previously,
        # same target used twice in same dir; second's ArtifactDataset
        # lacked schema, losing it in published outputs. manifest outputs
        # duplicated paths, file_count doubled.
        recorder.stage_completed(
            output_key,
            "export",
            message="Export written",
            metrics={"file_count": len(export_paths)},
        )

        schema_summary = build_schema_summary(
            (column.name, column.dtype, column.nullable) for column in silver.schema.columns
        )
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key, status="ok", stages_completed=tuple(completed)
            ),
            output_paths=tuple(outputs),
            row_count=evaluated_row_count,
            schema_summary=schema_summary,
            provenance_entry=provenance_entry,
            quality_results=quality_results,
            quality_evaluated=quality_evaluated,
            schema_drift=schema_drift,
            drift_evaluation=drift_evaluation,
            silver=captured_silver,
            gold_selection=gold_selection,
            resumed=((resumed_combinations, total_combinations) if resumed_combinations else None),
        )
    except BuildCancelled:
        # Cooperative cancellation is not failure (#481) — caught before
        # common except below so not interpreted as "this source failed". No
        # failure events (stage_failed/source_fetch_failed), error field empty.
        # Already completed stages (completed) and outputs preserved in partial
        # manifest — unstarted stages never recorded as successful.
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key,
                status="cancelled",
                stages_completed=tuple(completed),
            ),
            output_paths=tuple(outputs),
            row_count=evaluated_row_count,
            provenance_entry=provenance_entry,
            quality_results=quality_results,
            quality_evaluated=quality_evaluated,
            schema_drift=schema_drift,
            drift_evaluation=drift_evaluation,
            silver=captured_silver,
        )
    except Exception as exc:  # Convert stage failure to result for manifest
        # ValidationError/DatasetValidationError include no filesystem paths,
        # so message passed as-is. IngestionError (#498) also designed to
        # carry only safe messages (no raw response body/internal stack) —
        # SSRF block/oversize/corrupt file reasons visible to user immediately.
        # Other BuildError subclasses (ExportError/ManifestError) may include
        # internal info like destination paths, so log detailed message to
        # server warning, return generic message to client (#225).
        if isinstance(
            exc, (ValidationError, DatasetValidationError, IngestionError, GoldSelectionError)
        ):
            error_msg = str(exc)
        else:
            logger.error(
                "source pipeline failed for %r: %s",
                output_key,
                exc,
                exc_info=exc,
            )
            error_msg = f"pipeline failed for source {output_key!r}"
        # Record failure event per last boundary actually reached (#496) —
        # completed list is authoritative for "how far did each stage get",
        # so next stage is the failed one. Unstarted stages don't record as
        # failures (no events at all).
        if "bronze" not in completed:
            if not fetch_completed:
                recorder.source_fetch_failed(output_key, message=error_msg)
            recorder.stage_failed(output_key, "bronze", message=error_msg)
        elif "silver" not in completed:
            recorder.stage_failed(output_key, "silver", message=error_msg)
        elif "gold" not in completed:
            recorder.stage_failed(output_key, "gold", message=error_msg)
        elif export_started:
            recorder.stage_failed(output_key, "export", message=error_msg)
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key,
                status="failed",
                stages_completed=tuple(completed),
                error=error_msg,
            ),
            output_paths=tuple(outputs),
            row_count=evaluated_row_count,
            provenance_entry=provenance_entry,
            quality_results=quality_results,
            quality_evaluated=quality_evaluated,
            schema_drift=schema_drift,
            drift_evaluation=drift_evaluation,
            silver=captured_silver,
        )
    finally:
        # The staged records are needed only while this source runs; its persisted
        # Bronze is in the run's bronze directory (#622).
        staging_root = context.output_root / context.run_id / _STAGING_DIRNAME
        shutil.rmtree(staging_root / output_key, ignore_errors=True)
        # Sources run in parallel; whichever finishes last removes the empty parent.
        with contextlib.suppress(OSError):
            staging_root.rmdir()


@dataclass(frozen=True)
class _CompositionPipelineResult:
    """Local result of composition execution (#506). Separate from
    _SourcePipelineResult for same reason — runs single-threaded after all
    sources complete, but return shape preserved for same manifest merge loop.
    """

    outcome: CompositionOutcome
    output_paths: tuple[str, ...] = ()
    row_count: int | None = None
    schema_summary: SchemaSummary | None = None
    provenance: CompositionProvenance | None = None


def _run_composition(
    composition: CompositionSpec,
    *,
    silver_by_key: Mapping[str, SilverDataset],
    context: BuildContext,
) -> _CompositionPipelineResult:
    """Execute composition (join) and persist combined Gold outputs (#506).

    Join key existence/dtype compatibility validation and duplicate-key
    explosion detection are handled by ``build_composed_gold_package``
    (runtime validation gate in build pipeline) — spec.validator only
    checks structural alias references.

    If any referenced source failed to pass Silver, join is not attempted
    and returns "skipped".
    """
    join = composition.join
    missing = [alias for alias in (join.left, join.right) if alias not in silver_by_key]
    if missing:
        return _CompositionPipelineResult(
            outcome=CompositionOutcome(
                name=composition.name,
                status="skipped",
                error=(
                    f"source(s) {missing} did not complete Silver successfully; "
                    "composition was not attempted"
                ),
            )
        )

    try:
        package, stats = build_composed_gold_package(
            left_silver=silver_by_key[join.left],
            right_silver=silver_by_key[join.right],
            join=join,
            dataset_name=composition.name,
            exports=context.spec.exports,
            metadata=_gold_package_metadata(context.spec),
        )
    except CompositionError as exc:
        return _CompositionPipelineResult(
            outcome=CompositionOutcome(name=composition.name, status="failed", error=str(exc))
        )

    if stats.duplicate_key_warning:
        # If on_duplicate_key="fail" was set, build_composed_gold_package
        # would have raised CompositionError, so reaching here means
        # severity="warn" (default) — log only, continue.
        logger.warning(
            "composition %r: join keys present on both sides repeat on both sides and "
            "multiplied rows (left=%s distinct_keys=%d/%d rows, right=%s distinct_keys=%d/%d "
            "rows, output_rows=%d) (#506, #698)",
            composition.name,
            join.left,
            stats.left_distinct_key_count,
            stats.left_row_count,
            join.right,
            stats.right_distinct_key_count,
            stats.right_row_count,
            stats.output_row_count,
        )
    if stats.left_null_key_rows or stats.right_null_key_rows:
        # on_null_key="fail" would have raised; under "warn" the rows are dropped
        # from matching, and that is said out loud rather than hidden (#698).
        logger.warning(
            "composition %r: rows with a null join key never match "
            "(left=%s null_key_rows=%d, right=%s null_key_rows=%d) (#698)",
            composition.name,
            join.left,
            stats.left_null_key_rows,
            join.right,
            stats.right_null_key_rows,
        )

    outputs: list[str] = []
    gold_paths = persist_gold_package(
        package, output_root=context.output_root, run_id=context.run_id
    )
    _record_output_paths(
        outputs,
        gold_paths.table_path,
        gold_paths.package_path,
        *gold_paths.splits_paths.values(),
    )
    export_paths = export_gold_package(package, output_dir=gold_paths.gold_dir)
    _record_output_paths(outputs, *export_paths)

    combined_schema = build_schema(package.table)
    card = build_dataset_card(
        title=context.spec.title,
        description=context.spec.description,
        sources=(join.left, join.right),
        fields=((col.name, col.dtype, col.nullable) for col in combined_schema.columns),
        sample_rows=package.table.head(DEFAULT_PREVIEW_LIMIT).to_dicts(),
        license=_dataset_card_license(context.spec),
        version=_dataset_card_version(context.spec),
    )
    card_path = gold_paths.gold_dir / "README.md"
    _ = card_path.write_text(render_dataset_card(card), encoding="utf-8")
    _record_output_paths(outputs, card_path)

    # Single-source path doesn't export again (#629).

    schema_summary = build_schema_summary(
        (col.name, col.dtype, col.nullable) for col in combined_schema.columns
    )
    provenance = CompositionProvenance(
        name=composition.name,
        left=join.left,
        right=join.right,
        join_type=join.type,
        left_key=join.left_key,
        right_key=join.right_key,
        left_row_count=stats.left_row_count,
        left_distinct_key_count=stats.left_distinct_key_count,
        right_row_count=stats.right_row_count,
        right_distinct_key_count=stats.right_distinct_key_count,
        output_row_count=stats.output_row_count,
        duplicate_key_warning=stats.duplicate_key_warning,
        keys=tuple(JoinKeyProvenance(left=lk, right=rk) for lk, rk in stats.keys),
        cardinality=stats.cardinality,
        observed_cardinality=stats.observed_cardinality,
        left_unmatched_ratio=stats.left_unmatched_ratio,
        right_unmatched_ratio=stats.right_unmatched_ratio,
        expansion_ratio=stats.expansion_ratio,
        left_null_key_rows=stats.left_null_key_rows,
        right_null_key_rows=stats.right_null_key_rows,
    )
    return _CompositionPipelineResult(
        outcome=CompositionOutcome(name=composition.name, status="ok"),
        output_paths=tuple(outputs),
        row_count=stats.output_row_count,
        schema_summary=schema_summary,
        provenance=provenance,
    )


def _reclaim(catalog: TableCatalog, table_id: str, keep: int | None) -> None:
    """Drop the snapshots of ``table_id`` that this commit just superseded.

    A build that commits and never reclaims grows the warehouse by a whole copy of
    Gold every refresh — a dataset refreshed daily keeps 365 of them in a year
    (#738). The reclamation existed before this call did; nothing ran it.

    It runs here, in the build, for one reason: there is no scheduler. A CLI command
    nobody runs is the same defect one level up. The work is bounded — one table, at
    most ``len(committed) - keep`` directories — and it happens after the pointer has
    already moved, so a build that got as far as committing is already successful.

    Which is why this never raises. Reclamation failing is a warehouse that uses more
    disk than it should; a build failing is a dataset the user does not have. The
    second is worse, and the first is recoverable by running ``warehouse-gc`` later.

    ``keep=None`` turns it off, for a caller that keeps every snapshot on purpose.
    """
    if keep is None:
        return
    try:
        report = warehouse_gc.collect(catalog, table_id, keep=keep)
    except Exception:  # noqa: BLE001 - see docstring: never fail a committed build
        logger.warning("snapshot reclamation failed for table %s", table_id, exc_info=True)
        return
    if report.removed_count:
        logger.info(
            "reclaimed %d snapshot director%s from table %s",
            report.removed_count,
            "y" if report.removed_count == 1 else "ies",
            table_id,
        )
    if report.kept_leased:
        logger.info(
            "kept %d snapshot(s) of table %s: a query still holds a lease",
            len(report.kept_leased),
            table_id,
        )


def run_build(
    spec: BuildSpec,
    *,
    client: SourceClient,
    output_root: Path,
    run_id: str | None = None,
    created_by: str | None = None,
    owner_id: str | None = None,
    manifest_owner_id: str | None = None,
    upload_repository: UploadRepository | None = None,
    event_store: BuildEventStore | None = None,
    cancellation: CancellationProbe | None = None,
    catalog: TableCatalog | None = None,
    workspace_id: str = "ws_personal",
    warehouse_keep: int | None = 3,
    secret_values: tuple[str, ...] = (),
) -> BuildResult:
    """Execute BuildSpec through Medallion pipeline.

    Args:
        spec: BuildSpec to execute.
        client: kpubdata-compatible client for Bronze fetch.
        output_root: execution workspace root.
        run_id: execution identifier. Auto-generated from timestamp if omitted.
        created_by: display/legacy label of principal requesting build (#388).
        owner_id: canonical stable owner identity for ``kind="file"`` source
            resolver's upload ownership verification (#505/#498) — "owner of
            uploads this run references". Sync POST /build always uses
            submitting principal as owner; async /builds omits this for now
            (None), not exposing stable identity to file resolver (#498 async
            limitation, preserved).
        manifest_owner_id: canonical stable owner identity recorded in persisted
            run manifest (and BuildIndex reading that manifest, #505 SSOT) —
            "who submitted this run". Omit (None) to use owner_id (100% same
            as existing sync callers). Async callers can omit file resolver's
            owner_id (None) while filling this separately: "who submitted"
            (persisted ownership) separate from "what can file resolver verify"
            (different values).
        upload_repository: content repository for ``kind="file"`` sources (#498).
            None fails builds with file sources.
        event_store: structured run event timeline repository (#496). None
            (CLI direct call) records no events — existing callers need no changes.
        cancellation: cooperative cancellation probe (#481). None (sync POST /build,
            CLI) skips cancellation checks entirely — 100% same as existing behavior.
            With probe, check at stage boundaries and after all sources complete,
            before composition/manifest finalize (``commit()``). Success of
            ``commit()`` locks run as successful — later cancellation rejected,
            structurally preventing success manifest from later inverting to
            cancelled.

    Returns:
        BuildResult: overall status (ok/failed/cancelled), per-source results,
        manifest path. "cancelled" only with cancellation probe caller.

    Raises:
        ValidationError: spec fails minimum execution requirements.
        ValueError: run_id contains unsafe characters.
    """
    effective_manifest_owner_id = owner_id if manifest_owner_id is None else manifest_owner_id
    # Validate spec first at entry point (fail-fast). Delegating to caller
    # risks malformed spec deeply entering stages with cryptic errors;
    # block before stage entry (#212).
    validate_spec(spec)
    context = BuildContext.create(spec, output_root=output_root, run_id=run_id)
    # Recorder created only after run_id finalized (BuildContext.create
    # generates if omitted) — "started" is true semantic here (#496).
    # validate_spec failure: no run_id/workspace, no event recorded.
    # Existing behavior: validation failure → no workspace created.
    recorder = BuildEventRecorder(event_store, run_id=context.run_id)
    recorder.run_started()
    # Lock validated actual execution inputs before pipeline. Later source
    # failures still preserve run audit info; validation input failures
    # don't snapshot (no workspace created).
    _, spec_digest = write_buildspec_snapshot(
        spec, output_root=context.output_root, run_id=context.run_id
    )
    # The revision each table has *now*, before anything is fetched (#787). Committing
    # against it makes a refresh another build finished meanwhile a conflict: the
    # older data loses instead of replacing the newer snapshot.
    start_revisions: dict[str, int] = (
        {
            key: catalog.table_revision(workspace_id, f"{spec.dataset_id}.{key}")
            for key in (_output_source_key(source) for source in spec.sources)
        }
        if catalog is not None
        else {}
    )

    # composition (#506) referenced aliases' Silver only survives thread results
    # — non-composition builds unchanged, no extra preservation.
    composition_aliases: frozenset[str] = (
        frozenset({spec.composition.join.left, spec.composition.join.right})
        if spec.composition is not None
        else frozenset()
    )

    def _worker(source: SourceRef) -> _SourcePipelineResult:
        return _run_source_pipeline(
            source,
            client=client,
            context=context,
            recorder=recorder,
            upload_repository=upload_repository,
            owner_id=owner_id,
            baseline_owner_id=effective_manifest_owner_id,
            capture_silver=_output_source_key(source) in composition_aliases,
            cancellation=cancellation,
            secret_values=secret_values,
        )

    # Per-source fetch/stage mostly waits on network I/O, so concurrent
    # execution via thread pool reduces total time (#247). executor.map
    # returns results in spec.sources order, not completion order, so
    # downstream merge (manifest) stays deterministic.
    max_workers = min(len(spec.sources), _MAX_PARALLEL_SOURCES)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_worker, spec.sources))

    outcomes = tuple(result.outcome for result in results)

    # After all source execution, merge single-threaded — no shared mutable
    # state across threads, so this stage is as safe as pre-parallelization (#247).
    outputs: list[str] = []
    row_counts: dict[str, int] = {}
    schema_summaries: dict[str, SchemaSummary] = {}
    provenance: list[SourceProvenance] = []
    provenance_by_key: dict[str, SourceProvenance] = {}
    quality_results: dict[str, tuple[QualityCheckResult, ...]] = {}
    schema_drift: dict[str, tuple[SchemaDriftFinding, ...]] = {}
    drift_evaluation: dict[str, tuple[DriftEvaluation, ...]] = {}
    silver_by_key: dict[str, SilverDataset] = {}
    gold_selection: dict[str, GoldSelectionResult] = {}
    resumed_sources: dict[str, dict[str, JsonValue]] = {}
    for result in results:
        outputs.extend(result.output_paths)
        if result.resumed is not None:
            resumed_sources[result.outcome.source_key] = {
                "resumed_combinations": result.resumed[0],
                "total_combinations": result.resumed[1],
            }
        if result.gold_selection is not None:
            gold_selection[result.outcome.source_key] = result.gold_selection
        if result.row_count is not None:
            row_counts[result.outcome.source_key] = result.row_count
        if result.schema_summary is not None:
            schema_summaries[result.outcome.source_key] = result.schema_summary
        if result.provenance_entry is not None:
            provenance.append(result.provenance_entry)
            provenance_by_key[result.outcome.source_key] = result.provenance_entry
        # quality_evaluated tracks whether evaluate_quality was actually called
        # (#486) — even if FAIL fails source, result survives. Bronze/Silver
        # failure never reaches evaluate_quality, so manifest lacks that key
        # (distinguish "0 checks" from "no evaluation").
        if result.quality_evaluated:
            quality_results[result.outcome.source_key] = result.quality_results
        if result.schema_drift:
            schema_drift[result.outcome.source_key] = result.schema_drift
        # Recorded unconditionally, unlike schema_drift: "not evaluated" is the case
        # that has to survive to the manifest, and dropping a falsy value is exactly
        # how it used to disappear (#700).
        if result.drift_evaluation:
            drift_evaluation[result.outcome.source_key] = result.drift_evaluation
        if result.silver is not None:
            silver_by_key[result.outcome.source_key] = result.silver

    # ---- Final safety boundary (#481)-----------------------------------------
    #
    # This run's "point of no return". Cancellation is final in two cases:
    #
    #   (a) Any source already observed cancellation at stage boundary.
    #   (b) All sources complete, but cancellation arrives before entering
    #       composition/manifest finalize — ``commit()`` returns False.
    #
    # ``commit()`` success locks cancellation requests (``CancellationProbe``
    # contract), so normal path below writes success/failure manifest while
    # job never transitions to cancelling — preventing success manifest
    # inversion to cancelled, and preventing cancelling → succeeded transition.
    # (a) skips commit: cancellation already confirmed final.
    cancelled = any(outcome.status == "cancelled" for outcome in outcomes)
    if not cancelled and cancellation is not None and not cancellation.commit():
        cancelled = True

    # composition (#506) runs after all sources complete, single-threaded,
    # using validated Silver from referenced sources. Skipped on cancelled
    # run — no new stages start after cancel (same source stage principle).
    # Manifest doesn't record unstarted composition as success (null).
    composition_outcome: CompositionOutcome | None = None
    composition_provenance: CompositionProvenance | None = None
    if spec.composition is not None and not cancelled:
        composition_result = _run_composition(
            spec.composition, silver_by_key=silver_by_key, context=context
        )
        composition_outcome = composition_result.outcome
        composition_provenance = composition_result.provenance
        outputs.extend(composition_result.output_paths)
        if composition_result.row_count is not None:
            row_counts[composition_result.outcome.name] = composition_result.row_count
        if composition_result.schema_summary is not None:
            schema_summaries[composition_result.outcome.name] = composition_result.schema_summary

    errors = tuple(
        f"{outcome.source_key}: {outcome.error}"
        for outcome in outcomes
        if outcome.status == "failed" and outcome.error is not None
    )
    if composition_outcome is not None and composition_outcome.status != "ok":
        errors = (*errors, f"{composition_outcome.name}: {composition_outcome.error}")
    if cancelled:
        # Cancellation is not failure, so no run_failed; not success either,
        # no run_finished (#481). Terminal event (``run_cancelled``) recorded
        # by service layer once at job transition — recording here too would
        # differ from queued cancel (never executed) and duplicate on same run.
        #
        # If sources already failed, failure reasons survive in errors —
        # cancellation doesn't suppress failure. But run terminal state is
        # "cancelled".
        status = "cancelled"
    elif errors:
        status = "failed"
        recorder.run_failed(failed_source_count=len(errors))
    else:
        status = "ok"
        recorder.run_finished()

    # Materialise after every source is done, in the single-threaded merge. Committing
    # from the worker pool would put four threads through the same compare-and-swap and
    # make three of them lose for no reason (#699).
    #
    # Before the manifest, not after (#788): a commit that fails is part of what this
    # run did, so the manifest records it, the index still gets its row, and the caller
    # gets an answer instead of an exception after a manifest that already said ok.
    materialized: dict[str, MaterializeResult] = {}
    warehouse_failures: dict[str, dict[str, str]] = {}
    if catalog is not None and status == "ok":
        sources_by_key = {_output_source_key(source): source for source in spec.sources}
        for outcome in outcomes:
            if outcome.status != "ok":
                continue
            gold_dir = context.output_root / context.run_id / "gold" / outcome.source_key
            if not gold_dir.is_dir():
                continue
            # Recorded so a later refresh can pick a volume baseline that collected the
            # same population under the same contract (#700). Without them the catalog
            # can only answer "coverage unknown" for every snapshot a build commits.
            source_ref = sources_by_key.get(outcome.source_key)
            fingerprints = fingerprint_source(source_ref) if source_ref is not None else None
            try:
                committed = materialize(
                    catalog,
                    workspace_id=workspace_id,
                    logical_name=f"{spec.dataset_id}.{outcome.source_key}",
                    source_dir=gold_dir,
                    run_id=context.run_id,
                    owner_id=effective_manifest_owner_id,
                    coverage_fingerprint=fingerprints.coverage if fingerprints else None,
                    source_params_fingerprint=(
                        fingerprints.source_params if fingerprints else None
                    ),
                    schema_contract_version=(
                        fingerprints.schema_contract if fingerprints else None
                    ),
                    # The table holds Gold's rows; with a selection that is not Silver's
                    # count (#659).
                    row_count=(
                        gold_selection[outcome.source_key].output_rows
                        if outcome.source_key in gold_selection
                        else row_counts.get(outcome.source_key)
                    ),
                    expected_revision=start_revisions.get(outcome.source_key),
                    # A snapshot of a partial fetch says so (#816), so a reader can show
                    # it rather than present part of the data as the whole.
                    coverage=snapshot_coverage(provenance_by_key.get(outcome.source_key)),
                )
            except SnapshotConflict:
                # Another build committed this table after this one started (#787).
                # Not retried (#699): this run's data is older than what is current.
                warehouse_failures[outcome.source_key] = {
                    "reason": "conflict",
                    "detail": "another build committed this table after this run started; "
                    "the newer snapshot stays current",
                }
                continue
            except (WarehouseError, OSError) as exc:
                logger.exception("warehouse commit failed for %s", outcome.source_key)
                warehouse_failures[outcome.source_key] = {
                    "reason": "commit_failed",
                    "detail": f"the snapshot could not be committed ({type(exc).__name__})",
                }
                continue
            materialized[outcome.source_key] = committed
            logger.info(
                "materialised %s as snapshot %s",
                outcome.source_key,
                committed.snapshot.id,
            )
            _reclaim(catalog, committed.table.id, warehouse_keep)

    manifest = BuildManifest(
        build_id=context.run_id,
        status=status,
        # # partial (#481): "completed before normal finish, outputs are
        # partial artifacts". True only for cancelled runs; partial nature
        # of failed runs expressed via status/errors as before (preserve
        # existing consumer meaning).
        partial=cancelled,
        started_at=context.started_at,
        finished_at=utc_now(),
        inputs=tuple(_output_source_key(source) for source in spec.sources),
        outputs=tuple(outputs),
        # # recorder absorbed event logging failures (#496) here — reusing
        # existing authoritative warnings channel without new API field,
        # so API consumers can see if event timeline actually has gaps.
        warnings=recorder.dropped_events(),
        errors=errors,
        row_counts=row_counts,
        schema_summaries=schema_summaries,
        provenance=tuple(provenance),
        build_environment=capture_build_environment(),
        inputs_fingerprint=compute_inputs_fingerprint(provenance),
        inputs_fingerprint_algorithm=FINGERPRINT_ALGORITHM if provenance else None,
        created_by=created_by,
        owner_id=effective_manifest_owner_id,
        quality_results=quality_results,
        schema_drift=schema_drift,
        drift_evaluation=drift_evaluation,
        composition=composition_provenance,
        warehouse_failures=warehouse_failures,
        gold_selection={key: value.body() for key, value in gold_selection.items()},
        reproducibility=not_reproducible(resumed_sources) if resumed_sources else None,
        artifacts=_artifact_digests(
            context,
            [o.source_key for o in outcomes if o.status == "ok"]
            + (
                [composition_outcome.name]
                if composition_outcome is not None and composition_outcome.status == "ok"
                else []
            ),
        ),
    )
    manifest_path = context.output_root / context.run_id / "manifest.json"
    manifest_writer(manifest, manifest_path)

    return BuildResult(
        context=context,
        status=status,
        outcomes=outcomes,
        manifest_path=manifest_path,
        spec_digest=spec_digest,
        composition_outcome=composition_outcome,
        materialized=materialized,
        warehouse_failures=warehouse_failures,
    )

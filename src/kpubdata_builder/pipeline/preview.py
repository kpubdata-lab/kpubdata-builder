"""Build preview (#3).

Before running the full build, show schema and few sample rows per source. After
Bronze fetch, materialize Silver in memory only — **do not persist any output files**
(no persist call). Miniature version of build.

Quality/Schema evaluation uses the same common evaluator as orchestrator.run_build
(``quality.evaluate_quality``) (#486) — Preview and Build make identical judgments
on same data/rules, no semantic drift. Drift detection not performed (requires
comparing with persisted prior run, but preview writes nothing to workspace).

Source↔Silver diff and sampling (#497): within single execution, compare Bronze raw
sample and Silver transformed sample by same row index, cell by cell. Only with
current structure where Bronze→Silver path (normalize/validate) does not filter/reorder
rows is ``diff_available=true``; when precondition breaks (row count mismatch etc.),
``diff_available=false`` (fail-closed instead of incorrect index diff).

Preview row limit (≤ MAX_PREVIEW_LIMIT, service/app.py) bounds rows, but columns
are unconstrained anywhere; on wide datasets cell-level diff item count can still be
unlimited. diffs list itself is truncated at MAX_PREVIEW_DIFF_ITEMS and that fact
recorded in ``diff_truncated`` — transform_summary aggregation (changed_cells/
changed_rows) remains accurate even after truncation.

Fetch bound (#1185): a public_api source is read up to ``limit`` records or
``PREVIEW_MAX_PAGES`` pages, never the whole source — a preview spends at most that
many requests of the user's provider quota. Statistics, quality checks and a random
sample are then over the records fetched; ``fetch_complete`` says whether those are
the whole source.

Main components:
    - SampleMode: "first" | "random" sampling method
    - PreviewDiffItem: One cell-level change
    - PreviewTransformSummary: Change summary over comparable sample range
    - SourcePreview: Per-source preview result
    - PreviewResult: Full preview result
    - preview_build: Preview entry point
"""

from __future__ import annotations

import contextlib
import random
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ..quality import QualityCheckResult, evaluate_quality
from ..spec import BuildSpec, JsonValue, SourceRef
from ..spec.models import QualityPolicy
from ..spec.validator import validate_spec
from ..stages.bronze.build import FetchBound, SourceClient
from ..stages.bronze.models import CallTotal
from ..stages.bronze.resolve import build_bronze_artifact_for_source, source_identity
from ..stages.bronze.writer import new_staging_dir
from ..stages.silver.build import build_silver_dataset
from ..stages.silver.preview import select_preview_rows
from ..tabular import DEFAULT_PREVIEW_LIMIT, PreviewSlice, SchemaInfo, TableStatistics
from ..tabular.duckdb_runtime import build_connection
from ..uploads import UploadRepository
from .failures import public_failure_message

SampleMode = Literal["first", "random"]
_SAMPLE_MODES: tuple[SampleMode, ...] = ("first", "random")

# Fixed default seed for random sample_mode when seed is omitted — unlike "random"
# name, does not use unreproducible wall-clock seed (#497).
DEFAULT_PREVIEW_SEED = 0

# Defensive upper bound for diffs response (#497). limit (≤ MAX_PREVIEW_LIMIT,
# service/app.py) bounds rows only, columns are unconstrained anywhere, so
# cell-level diff item count can be rows × changed_columns unlimited on wide
# datasets. This bound only truncates actually materialized PreviewDiffItem count
# in response, reusing same value (MAX_PREVIEW_LIMIT) rather than creating new
# magic number — transform_summary.changed_cells/changed_rows remain accurate even
# after truncation, keeping "diffs list truncated but aggregation exact" contract.
MAX_PREVIEW_DIFF_ITEMS = 1000

# Most requests one source's preview makes (#1185), across its param_grid
# combinations too. A data.go.kr key's daily quota is often 1,000 to 10,000 requests;
# the unbounded preview could spend it on one large source.
PREVIEW_MAX_PAGES = 3


@dataclass(frozen=True)
class PreviewDiffItem:
    """One cell-level change between Source and Silver (#497).

    Attributes:
        row: 0-based position within source_sample/sample arrays. Not absolute row
            number in full dataset (both refer to same target only when
            diff_available=true).
        column: Column name.
        before: Value before transformation (Bronze raw).
        after: Value after transformation (Silver).
        transform: When column has declared casting (schema.casts), filled as
            ``"cast:{dtype}"``. Otherwise None; not guessed.
    """

    row: int
    column: str
    before: JsonValue
    after: JsonValue
    transform: str | None = None


@dataclass(frozen=True)
class PreviewTransformSummary:
    """Change summary computed over comparable sample range (#497).

    Based on sample range in this preview response, not full dataset change.

    Attributes:
        changed_cells: Count of cells with different values.
        changed_rows: Count of rows with one or more cells changed.
    """

    changed_cells: int
    changed_rows: int


@dataclass(frozen=True)
class SourcePreview:
    """Single source preview result.

    Attributes:
        source_key: Source identifier.
        status: "ok" or "failed".
        schema: Inferred schema summary (empty SchemaInfo if failed).
        preview: Top N row preview (empty PreviewSlice if failed). Contains top N
            or deterministic random N rows per sample_mode.
        statistics: Full table statistics (row_count/null_counts/duplicate_rate, #440).
            Evidence for schema contract draft (VAL-4) and quality gate (QG-3).
        quality_results: Structured Quality/Schema evaluation result (#486). Same
            evaluator result as Build, contains only actually-evaluated checks including PASS.
        error: Error message if failed.
        source_sample: Bronze raw sample before transformation (#497). Filled best-effort
            even when diff_available=false (empty tuple if failed).
        sample_mode: Actual sampling method applied in this response.
        diff_available: True only when source_sample[i] and preview.rows[i] guaranteed
            to reference same logical row. False on row count mismatch, query failure,
            etc.; diffs/transform_summary then empty.
        diffs: Cell-level change list filled only when diff_available=true. Capped at
            MAX_PREVIEW_DIFF_ITEMS (see diff_truncated).
        transform_summary: Change summary filled only when diff_available=true.
            changed_cells/changed_rows remain exact aggregate even if diffs truncated.
        diff_truncated: True if diffs hit MAX_PREVIEW_DIFF_ITEMS ceiling and did not
            capture all changed cells. Always false if diff_available=false (not "truncated",
            diff was never attempted).
        fetch_complete: False when the fetch stopped at the preview's bound while the
            source had more records (#1185); ``preview.total_rows`` and ``statistics``
            then count the records fetched, not the source.
        source_reported_total: The provider's own count of the source's records, when
            a single call reported one; None otherwise (a ``param_grid`` total is never
            summed, #816).
    """

    source_key: str
    status: str
    schema: SchemaInfo
    preview: PreviewSlice
    statistics: TableStatistics
    quality_results: tuple[QualityCheckResult, ...] = ()
    error: str | None = None
    source_sample: tuple[dict[str, JsonValue], ...] = ()
    sample_mode: SampleMode = "first"
    diff_available: bool = False
    diffs: tuple[PreviewDiffItem, ...] = ()
    transform_summary: PreviewTransformSummary | None = None
    diff_truncated: bool = False
    fetch_complete: bool = True
    source_reported_total: int | None = None


@dataclass(frozen=True)
class PreviewResult:
    """Full preview result.

    Attributes:
        previews: Per-source preview results.
    """

    previews: tuple[SourcePreview, ...]


def _fetch_key(source: SourceRef) -> str:
    """Return Bronze fetch identity (canonical per-kind identity, #498)."""
    provider, dataset = source_identity(source)
    return f"{provider}.{dataset}"


def _output_key(source: SourceRef) -> str:
    """Key exposed on preview result surface — use alias if present."""
    return source.alias if source.alias else _fetch_key(source)


def _select_indices(
    *, total_rows: int, limit: int, sample_mode: SampleMode, seed: int
) -> list[int]:
    """Select up to limit row indices from total_rows, in ascending order.

    "first": Select top count rows as-is (matches existing top-N behavior).
    "random": Non-replacement sampling via dedicated ``random.Random`` instance
        initialized with seed. Does not touch global random state. Direct range
        indexing avoids copying/shuffling entire dataset to list (#497 memory
        bound). Same total_rows/limit/seed always returns same result.

    Source/Silver must share this function's result for diff consistency — if
    sampled independently, may select different rows.
    """
    count = min(limit, total_rows)
    if count <= 0:
        return []
    if sample_mode == "first":
        return list(range(count))
    rng = random.Random(seed)
    return sorted(rng.sample(range(total_rows), count))


def _diff_sample(
    source_rows: Sequence[dict[str, JsonValue]],
    transformed_rows: Sequence[dict[str, JsonValue]],
    *,
    columns: Sequence[str],
    casts: Mapping[str, str] | None,
    max_items: int,
) -> tuple[tuple[PreviewDiffItem, ...], PreviewTransformSummary, bool]:
    """Compare two row sequences guaranteed to be sorted, cell by cell.

    Call only when caller has already verified source_rows[i] and transformed_rows[i]
    reference the same logical row (as determined by diff_available check).

    columns has no count limit (#497 sample/diff memory bound) — actually materialized
    diff items are capped at max_items, but comparison continues even after truncation
    so changed_cells/changed_rows always reflect accurate totals over entire sample
    range — aggregation is never dependent on diffs length.

    Returns: (diffs, transform_summary, truncated). truncated=true if actual changed
    cell count exceeded max_items and diffs could not capture all changes.
    """
    diffs: list[PreviewDiffItem] = []
    changed_rows = 0
    changed_cells = 0
    truncated = False
    for row_index, (before_row, after_row) in enumerate(
        zip(source_rows, transformed_rows, strict=True)
    ):
        row_changed = False
        for column in columns:
            before = before_row.get(column)
            after = after_row.get(column)
            if before == after:
                continue
            row_changed = True
            changed_cells += 1
            if len(diffs) < max_items:
                transform = f"cast:{casts[column]}" if casts and column in casts else None
                diffs.append(
                    PreviewDiffItem(
                        row=row_index,
                        column=column,
                        before=before,
                        after=after,
                        transform=transform,
                    )
                )
            else:
                truncated = True
        if row_changed:
            changed_rows += 1
    summary = PreviewTransformSummary(changed_cells=changed_cells, changed_rows=changed_rows)
    return tuple(diffs), summary, truncated


def _reported_total(call_totals: Sequence[CallTotal]) -> int | None:
    """The provider's total for a single-call source, else None (#816: never summed).

    A ``param_grid`` source is excluded by the caller: a bounded preview may have made
    one of its calls, whose total is that combination's, not the source's.
    """
    if len(call_totals) != 1 or call_totals[0].status != "reported":
        return None
    return call_totals[0].value


def _preview_source(
    source: SourceRef,
    *,
    client: SourceClient,
    limit: int,
    quality_policy: QualityPolicy | None,
    sample_mode: SampleMode,
    seed: int,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> SourcePreview:
    """Fetch one source → construct Silver in-memory, extract schema/sample/diff/quality results.

    upload_repository/owner_id are used only in kind="file" sources (#498).
    """
    out_key = _output_key(source)
    # Preview writes nothing that outlives it: Bronze is staged in a private directory
    # (#622) and removed however the preview ends.
    staging_dir = new_staging_dir()
    resources = contextlib.ExitStack()
    try:
        required_columns = source.schema.required if source.schema else ()
        column_dtypes = source.schema.dtypes if source.schema else None
        casts = source.schema.casts if source.schema else None
        rename = source.schema.rename if source.schema else None
        derived = source.schema.derived if source.schema else ()
        read_as = source.schema.read_as if source.schema else None
        null_tokens = source.schema.null_tokens if source.schema else ()
        column_null_tokens = source.schema.column_null_tokens if source.schema else None
        coalesce = source.schema.coalesce if source.schema else None
        zfill = source.schema.zfill if source.schema else None
        # Fetch raw records by kind(public_api/file/url) resolver (#498) — shared
        # with Build so preview and build always see same data from same source.
        bronze = build_bronze_artifact_for_source(
            source,
            client=client,
            upload_repository=upload_repository,
            owner_id=owner_id,
            secret_values=secret_values,
            staging_dir=staging_dir,
            bound=FetchBound(rows=limit, pages=PREVIEW_MAX_PAGES),
        )
        silver = build_silver_dataset(
            bronze,
            preview_limit=limit,
            required_columns=required_columns,
            casts=casts,
            rename=rename,
            derived=derived,
            read_as=read_as,
            null_tokens=null_tokens,
            column_null_tokens=column_null_tokens,
            coalesce=coalesce,
            zfill=zfill,
            column_dtypes=column_dtypes,
            connection=resources.enter_context(build_connection(staging_dir, "preview")),
            workdir=staging_dir,
        )
        resources.callback(silver.table.close)
        # Same shared evaluator as Build (#486) — no file persist.
        quality_results = evaluate_quality(
            silver,
            quality_policy,
            source_key=out_key,
            required_columns=required_columns,
            column_dtypes=column_dtypes,
        )

        total_rows = silver.statistics.row_count
        # Diff alignment basis is NOT count but Bronze→Silver row-preserving invariant:
        # normalize_table() loads the records in their original order (duckdb_load, the
        # row ordinal) then only column-level operations. null_tokens/coalesce/
        # rename/zfill/cast_columns/derived all preserve row count, changing only values/
        # column structure (#620: coalesce removes candidate *columns*, not rows);
        # validate_table() never touches table — no step filters/dedups/reorders rows
        # (test_silver.py::TestRowPreservingInvariant #497 regression-locks invariant).
        # While invariant holds, Bronze record i always matches silver.table row i,
        # so same index list safely reused for both.
        #
        # Below count check is cheap runtime guard only confirming invariant held "this
        # execution", NOT the alignment basis itself — if Silver adds dedup/filter later,
        # this code path itself must change; guard catches count change only (hypothetical
        # future reorder-only change with same count escapes guard, so such change
        # requires re-examining this logic).
        aligned = bronze.record_count == total_rows
        indices = _select_indices(
            total_rows=total_rows, limit=limit, sample_mode=sample_mode, seed=seed
        )

        source_sample = bronze.records_at(indices) if aligned else ()
        transformed_rows = select_preview_rows(silver.table, indices)
        sample_slice = PreviewSlice(rows=transformed_rows, total_rows=total_rows)

        diff_available = aligned and len(source_sample) == len(transformed_rows)
        if diff_available:
            columns = tuple(column.name for column in silver.schema.columns)
            diffs, transform_summary, diff_truncated = _diff_sample(
                source_sample,
                transformed_rows,
                columns=columns,
                casts=casts,
                max_items=MAX_PREVIEW_DIFF_ITEMS,
            )
        else:
            diffs = ()
            transform_summary = None
            diff_truncated = False

        return SourcePreview(
            source_key=out_key,
            status="ok",
            schema=silver.schema,
            preview=sample_slice,
            statistics=silver.statistics,
            quality_results=quality_results,
            source_sample=source_sample,
            sample_mode=sample_mode,
            diff_available=diff_available,
            diffs=diffs,
            transform_summary=transform_summary,
            diff_truncated=diff_truncated,
            fetch_complete=not bronze.stopped_early,
            source_reported_total=(
                None if source.param_grid else _reported_total(bronze.call_totals)
            ),
        )
    except Exception as exc:  # Convert preview failure to result
        return SourcePreview(
            source_key=out_key,
            status="failed",
            schema=SchemaInfo(),
            preview=PreviewSlice(rows=(), total_rows=0),
            statistics=TableStatistics(row_count=0, null_counts={}, duplicate_rate=0.0),
            # Same public-message rule as build (#954): raw engine/internal text is
            # logged server-side, never returned in the /preview response.
            error=public_failure_message(exc, out_key),
            fetch_complete=False,
            source_sample=(),
            sample_mode=sample_mode,
            diff_available=False,
            diffs=(),
            transform_summary=None,
            diff_truncated=False,
        )
    finally:
        resources.close()
        shutil.rmtree(staging_dir, ignore_errors=True)


def preview_build(
    spec: BuildSpec,
    *,
    client: SourceClient,
    limit: int = DEFAULT_PREVIEW_LIMIT,
    sample_mode: SampleMode = "first",
    seed: int = DEFAULT_PREVIEW_SEED,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> PreviewResult:
    """Produce schema and sample rows per source, Source↔Silver diff (no file write).

    Args:
        spec: BuildSpec to preview.
        client: kpubdata-compatible client for Bronze fetch.
        limit: Max rows per source.
        sample_mode: "first" (top N rows, default) or "random" (deterministic random N).
        seed: Seed when sample_mode="random". Same input+seed always returns same
            sample. Unused when sample_mode="first".
        upload_repository: Repository to fetch kind="file" source upload content
            (#498). None causes preview to fail if file source present.
        owner_id: Stable principal ID for upload ownership check (#498).

    Returns:
        PreviewResult: Per-source schema/sample/diff.

    Raises:
        ValueError: If limit < 1 or sample_mode not "first"/"random".
        TypeError: If seed not int (e.g., bool).
        ValidationError: If spec fails minimum requirements. Invalid spec fails fast
            without partial execution or empty result, matching service layer (#193).
    """
    if limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if sample_mode not in _SAMPLE_MODES:
        raise ValueError(f"sample_mode must be one of {_SAMPLE_MODES}, got {sample_mode!r}")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise TypeError(f"seed must be an int, got {type(seed).__name__}")
    validate_spec(spec)
    previews = tuple(
        _preview_source(
            source,
            client=client,
            limit=limit,
            quality_policy=spec.quality,
            sample_mode=sample_mode,
            seed=seed,
            upload_repository=upload_repository,
            owner_id=owner_id,
            secret_values=secret_values,
        )
        for source in spec.sources
    )
    return PreviewResult(previews=previews)

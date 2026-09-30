"""Builder spec validation routines (Medallion refactor: moved from legacy validator.py).

This module checks BuildSpec meets minimum execution requirements. Depends on exporter
registry, so not re-exported from spec package __init__ — import directly via
``kpubdata_builder.spec.validator`` path (avoids circular import).

Main functions:
    - validate_spec: Validate required conditions like dataset_id, sources, exports

BL3 (#417): Structures problems as {code, path, message, hint} object array.
For legacy string problems consumers, message always included.
"""

from __future__ import annotations

import codecs
import math
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..errors import ValidationError
from ..exporters import EXPORTER_REGISTRY
from ..tabular.polars_helpers import _FORMATTED_CASTS, _NAMED_DTYPES, TEXT_CASTS
from .models import (
    DERIVED_KINDS,
    ON_ABSENT_POLICIES,
    READ_AS_TYPES,
    SOURCE_FILE_FORMATS,
    SOURCE_KINDS,
    SOURCE_URL_FORMATS,
    SOURCE_URL_METHODS,
    UPLOAD_ID_PATTERN,
    BuildSpec,
    ColumnNullTokens,
    DerivedColumn,
    SourceRef,
)


@dataclass(frozen=True)
class ValidationProblem:
    """Structured validation problem (#417).

    Attributes:
        code: Machine-readable cause code.
        path: Field path (e.g., sources[0].params.base_date).
        message: Human-readable description (always included — backward compatible).
        hint: Suggested fix (optional).
    """

    code: str
    path: str
    message: str
    hint: str | None = None

    def __str__(self) -> str:
        return self.message


def _p(code: str, path: str, message: str, hint: str | None = None) -> ValidationProblem:
    return ValidationProblem(code=code, path=path, message=message, hint=hint)


def validate_spec(spec: BuildSpec) -> None:
    """Validate BuildSpec minimum executability.

    Raises:
        ValidationError: When one or more validation rules fail.
    """
    problems: list[ValidationProblem] = []
    if not spec.dataset_id.strip():
        problems.append(_p("empty_field", "dataset_id", "dataset_id must be a non-empty string"))
    if not spec.title.strip():
        problems.append(_p("empty_field", "title", "title must be a non-empty string"))
    if not spec.description.strip():
        problems.append(_p("empty_field", "description", "description must be a non-empty string"))
    if not spec.sources:
        problems.append(_p("missing_sources", "sources", "at least one source is required"))
    for i, source in enumerate(spec.sources):
        # provider/dataset only meaningful for kind="public_api" (default) — other kinds'
        # required/forbidden fields validated by _source_kind_problems (#498).
        if source.kind == "public_api":
            if not source.provider.strip():
                problems.append(
                    _p(
                        "empty_field",
                        f"sources[{i}].provider",
                        f"sources[{i}].provider must be a non-empty string",
                    )
                )
            if not source.dataset.strip():
                problems.append(
                    _p(
                        "empty_field",
                        f"sources[{i}].dataset",
                        f"sources[{i}].dataset must be a non-empty string",
                    )
                )
        if source.alias and not source.alias.strip():
            problems.append(
                _p(
                    "blank_alias",
                    f"sources[{i}].alias",
                    f"sources[{i}].alias must not be blank when provided",
                )
            )
    # exports may be empty (#703). A build with none ends at a committed table,
    # which is a complete job — publishing is an explicit follow-up, not the only
    # way to finish.
    for i, export in enumerate(spec.exports):
        if not export.output_path.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"exports[{i}].output_path",
                    f"exports[{i}].output_path must be a non-empty string",
                )
            )
        if export.kind not in EXPORTER_REGISTRY:
            supported = sorted(EXPORTER_REGISTRY)
            msg = f"exports[{i}].kind {export.kind!r} is not supported"
            problems.append(
                _p(
                    "unsupported_export_kind",
                    f"exports[{i}].kind",
                    msg,
                    hint=f"Use one of: {', '.join(supported)}",
                )
            )
    for key in spec.metadata:
        if not key.strip():
            problems.append(
                _p("empty_metadata_key", "metadata", "metadata keys must be non-empty strings")
            )
            break
    problems.extend(_split_problems(spec))
    problems.extend(_schema_problems(spec))
    problems.extend(_source_kind_problems(spec))
    problems.extend(_source_key_problems(spec))
    problems.extend(_param_grid_problems(spec))
    problems.extend(_pii_problems(spec))
    problems.extend(_license_problems(spec))
    problems.extend(_quality_problems(spec))
    problems.extend(_composition_problems(spec))
    problems.extend(_gold_selection_problems(spec))
    if problems:
        raise ValidationError([str(p) for p in problems], structured=problems)


def _split_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Collect validation problems in splits definition."""
    split = spec.splits
    if split is None:
        return []
    problems: list[ValidationProblem] = []
    if split.mode == "ratio":
        if not split.ratios:
            problems.append(
                _p(
                    "missing_ratios",
                    "splits.ratios",
                    "splits.ratios must define at least one split",
                )
            )
        if any(not name.strip() for name in split.ratios):
            problems.append(
                _p(
                    "empty_ratio_name",
                    "splits.ratios",
                    "splits.ratios names must be non-empty strings",
                )
            )
        non_finite = [name for name, f in split.ratios.items() if not math.isfinite(f)]
        if non_finite:
            problems.append(
                _p("non_finite_ratio", "splits.ratios", f"non-finite values: {sorted(non_finite)}")
            )
        elif any(fraction <= 0 for fraction in split.ratios.values()):
            problems.append(
                _p("non_positive_ratio", "splits.ratios", "splits.ratios values must be positive")
            )
        else:
            total = sum(split.ratios.values())
            if split.ratios and abs(total - 1.0) > 1e-6:
                problems.append(
                    _p(
                        "ratio_sum_mismatch",
                        "splits.ratios",
                        f"splits.ratios must sum to 1.0 (got {total})",
                    )
                )
    elif split.mode == "key":
        if not split.key.strip():
            problems.append(
                _p(
                    "empty_split_key",
                    "splits.key",
                    "splits.key must be a non-empty column name for key mode",
                )
            )
    else:
        problems.append(
            _p(
                "unsupported_split_mode",
                "splits.mode",
                f"splits.mode {split.mode!r} is not supported; use 'ratio' or 'key'",
                hint="Use 'ratio' or 'key'",
            )
        )
        # Time series data leakage check (#444). In ratio mode, if key is time
        # column, random shuffle exposes future information to train.
    _TIME_INDICATORS = (
        "date",
        "time",
        "dt",
        "year",
        "month",
        "day",
        "hour",
        "timestamp",
        "at",
        "ts",
    )
    if split.mode == "ratio" and split.key:
        key_lower = split.key.lower()
        if any(ind in key_lower for ind in _TIME_INDICATORS):
            problems.append(
                _p(
                    "time_column_random_split",
                    "splits",
                    f"splits.key {split.key!r} looks like a time column but mode is 'ratio' "
                    "— random split causes time-series data leakage (#444)",
                    hint="Use mode='key' for temporal splitting",
                )
            )
    return problems


def _schema_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate sources[].schema contract itself (#437).

    Check dtypes/casts strings are interpretable as ``_NAMED_DTYPES`` keys.
    Loader (loader._parse_schema) checks structure only; here checks semantics —
    prevents unknown dtype strings from failing only at runtime (normalize/validate).
    """
    problems: list[ValidationProblem] = []
    supported = sorted(_NAMED_DTYPES)
    supported_casts = sorted(set(_NAMED_DTYPES) | set(_FORMATTED_CASTS) | set(TEXT_CASTS))
    for i, source in enumerate(spec.sources):
        if source.schema is None:
            continue
        for col, dtype in source.schema.dtypes.items():
            if dtype.lower() not in _NAMED_DTYPES:
                problems.append(
                    _p(
                        "unknown_dtype",
                        f"sources[{i}].schema.dtypes.{col}",
                        f"unknown dtype {dtype!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(supported)}",
                    )
                )
        for col, cast in source.schema.casts.items():
            if cast.lower() not in supported_casts:
                problems.append(
                    _p(
                        "unknown_cast_dtype",
                        f"sources[{i}].schema.casts.{col}",
                        f"unknown cast dtype {cast!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(supported_casts)}",
                    )
                )
        for col, dtype in source.schema.read_as.items():
            if dtype not in READ_AS_TYPES:
                problems.append(
                    _p(
                        "unknown_read_as_type",
                        f"sources[{i}].schema.read_as.{col}",
                        f"unknown read_as type {dtype!r} for column {col!r}",
                        hint=f"Use one of: {', '.join(READ_AS_TYPES)}",
                    )
                )
        problems.extend(
            _column_null_token_problems(source.schema.column_null_tokens, prefix=f"sources[{i}]")
        )
        problems.extend(_coalesce_problems(source.schema.coalesce, prefix=f"sources[{i}]"))
        problems.extend(_zfill_problems(source.schema.zfill, prefix=f"sources[{i}]"))
        problems.extend(_rename_problems(source.schema.rename, prefix=f"sources[{i}]"))
        problems.extend(
            _derived_problems(
                source.schema.derived,
                prefix=f"sources[{i}]",
                # Declarations before derived collect only named columns. dtypes only
                # declare expected type without creating columns, so excluded — declaring
                # dtype for derived column is normal usage.
                reserved_names=set(source.schema.rename.values())
                | set(source.schema.casts)
                | set(source.schema.zfill)
                | set(source.schema.coalesce),
            )
        )
    return problems


def _column_null_token_problems(
    column_null_tokens: dict[str, ColumnNullTokens], *, prefix: str
) -> list[ValidationProblem]:
    """Validate schema.column_null_tokens declaration itself (#623).

    If tokens empty, nothing happens to that column. Declaring but non-functioning
    is worst — missing remains as value and quality metrics do not count it.
    """
    problems: list[ValidationProblem] = []
    for column, rule in column_null_tokens.items():
        field = f"{prefix}.schema.column_null_tokens.{column}"
        if not rule.tokens:
            problems.append(
                _p(
                    "empty_column_null_tokens",
                    field,
                    f"column_null_tokens for column {column!r} declares no tokens",
                    hint="List the source spellings that mean 'missing' in this column",
                )
            )
        if rule.on_absent not in ON_ABSENT_POLICIES:
            problems.append(
                _p(
                    "unknown_on_absent_policy",
                    f"{field}.on_absent",
                    f"unknown on_absent policy {rule.on_absent!r} for column {column!r}",
                    hint=f"Use one of: {', '.join(ON_ABSENT_POLICIES)}",
                )
            )
    return problems


def _coalesce_problems(
    coalesce: dict[str, tuple[str, ...]], *, prefix: str
) -> list[ValidationProblem]:
    """Validate schema.coalesce declaration itself (#620).

    If candidates empty, normalize fails at runtime with "no candidates". Preventing
    at declaration time tells where to fix.

    Also blocks overlapping groups here. Each rule removes resolved candidate columns,
    so if one rule's target is another's candidate, result depends on mapping traverse
    order. But ``canonical_spec_mapping()`` sorts keys for snapshot, so same digest
    declaration can behave differently from original build — recipe reproducibility breaks.
    """
    problems: list[ValidationProblem] = []
    targets = set(coalesce)
    owners: dict[str, str] = {}
    for target, candidates in coalesce.items():
        field = f"{prefix}.schema.coalesce.{target}"
        for candidate in candidates:
            if candidate in targets and candidate != target:
                problems.append(
                    _p(
                        "overlapping_coalesce_groups",
                        field,
                        f"coalesce target {candidate!r} is also a candidate of {target!r}",
                        hint=(
                            "Overlapping coalesce groups make the result depend on "
                            "declaration order; declare independent alias groups"
                        ),
                    )
                )
            owner = owners.setdefault(candidate, target)
            if owner != target:
                problems.append(
                    _p(
                        "overlapping_coalesce_groups",
                        field,
                        f"coalesce candidate {candidate!r} is claimed by both {owner!r} "
                        f"and {target!r}",
                        hint=(
                            "Overlapping coalesce groups make the result depend on "
                            "declaration order; declare independent alias groups"
                        ),
                    )
                )
    for target, candidates in coalesce.items():
        field = f"{prefix}.schema.coalesce.{target}"
        if not candidates:
            problems.append(
                _p(
                    "empty_coalesce_candidates",
                    field,
                    f"coalesce target {target!r} declares no candidate columns",
                    hint="List the source column names this canonical column is built from",
                )
            )
        if len(set(candidates)) != len(candidates):
            problems.append(
                _p(
                    "duplicate_coalesce_candidates",
                    field,
                    f"coalesce target {target!r} repeats a candidate column",
                )
            )
    return problems


def _zfill_problems(zfill: dict[str, int], *, prefix: str) -> list[ValidationProblem]:
    """Validate schema.zfill width makes sense (#620)."""
    problems: list[ValidationProblem] = []
    for column, width in zfill.items():
        if width < 1:
            problems.append(
                _p(
                    "invalid_zfill_width",
                    f"{prefix}.schema.zfill.{column}",
                    f"zfill width for column {column!r} must be >= 1, got {width}",
                )
            )
    return problems


#: Per-kind required input column count (#611). None means 1 or more.
_DERIVED_ARITY: dict[str, int | None] = {"date_parts": 3, "join_key": None}


def _rename_problems(rename: dict[str, str], *, prefix: str) -> list[ValidationProblem]:
    """Validate schema.rename target names don't overlap (#611 follow-up).

    When two source fields coalesce to same canonical name, normalize_table crashes
    with Polars DuplicateError — prevent at declaration time with spec terminology.
    (Target name overlapping existing non-renamed *source* column requires checking source
    so runtime guard handles it.)
    """
    problems: list[ValidationProblem] = []
    seen: dict[str, str] = {}
    for source_name, target in rename.items():
        if target in seen:
            problems.append(
                _p(
                    "duplicate_rename_target",
                    f"{prefix}.schema.rename.{source_name}",
                    f"rename target {target!r} is also the target of {seen[target]!r}",
                )
            )
        else:
            seen[target] = source_name
    return problems


def _derived_problems(
    derived: tuple[DerivedColumn, ...],
    *,
    prefix: str,
    reserved_names: set[str] | None = None,
) -> list[ValidationProblem]:
    """Validate schema.derived rule kind vocabulary, column count, name collisions (#611).

    normalize_table unpacks date_parts to (year, month, day), so mismatched count
    crashes at runtime with ValueError. Prevent at declaration time.

    ``reserved_names`` are column names already declared by earlier steps (rename targets,
    casts/zfill/coalesce keys). If derived column uses that name, with_columns silently
    overwrites existing column — overlaps between declarations caught here, overlaps with
    source column caught by runtime guard.
    """
    problems: list[ValidationProblem] = []
    reserved = set(reserved_names or ())
    seen_names: set[str] = set()
    for index, rule in enumerate(derived):
        field = f"{prefix}.schema.derived[{index}]"
        if rule.name in reserved or rule.name in seen_names:
            problems.append(
                _p(
                    "derived_name_collision",
                    f"{field}.name",
                    f"derived column {rule.name!r} collides with a column already declared "
                    "in this schema (rename target, casts/zfill/coalesce key, or another "
                    "derived rule)",
                )
            )
        seen_names.add(rule.name)
        if rule.kind not in DERIVED_KINDS:
            problems.append(
                _p(
                    "unknown_derived_kind",
                    f"{field}.kind",
                    f"unknown derived kind {rule.kind!r} for column {rule.name!r}",
                    hint=f"Use one of: {', '.join(DERIVED_KINDS)}",
                )
            )
            continue
        arity = _DERIVED_ARITY[rule.kind]
        if arity is not None and len(rule.columns) != arity:
            problems.append(
                _p(
                    "derived_column_arity",
                    f"{field}.columns",
                    f"{rule.kind} requires exactly {arity} columns, got {len(rule.columns)}",
                )
            )
        elif arity is None and not rule.columns:
            problems.append(
                _p(
                    "derived_column_arity",
                    f"{field}.columns",
                    f"{rule.kind} requires at least one column",
                )
            )
    return problems


def _source_kind_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate kind vocabulary and kind='file'/'url' source value/SSRF-related shapes (#498).

    If kind itself outside ``SOURCE_KINDS`` (public_api/file/url), reject here — loader
    (YAML path) already rejects, but ``SourceRef(kind="ftp", ...)`` constructed directly
    without loader makes this check the only defense (fail-closed, #538 review). Canonical
    source kind contract is only three: public_api | file | url; unknown kind never
    implicitly treated as public_api.

    Rest handles semantic rules loader doesn't check (allowed format/encoding/method
    vocabulary, URL scheme/userinfo, upload_id shape). Actual network connectivity
    verified only at runtime. SSRF defense (DNS resolve/redirect re-validation) is
    responsibility of fetch time (ingestion.url_fetch) — here quickly rejects clearly
    unsafe declarations (scheme=http, contains userinfo, etc.) before build execution.
    """
    problems: list[ValidationProblem] = []
    for i, source in enumerate(spec.sources):
        prefix = f"sources[{i}]"
        if source.kind not in SOURCE_KINDS:
            problems.append(
                _p(
                    "unsupported_source_kind",
                    f"{prefix}.kind",
                    f"{prefix}.kind {source.kind!r} is not supported; "
                    f"use one of {SOURCE_KINDS} (#498)",
                )
            )
        elif source.kind == "file":
            problems.extend(_file_source_problems(source, prefix))
        elif source.kind == "url":
            problems.extend(_url_source_problems(source, prefix))
    return problems


def _file_source_problems(source: SourceRef, prefix: str) -> list[ValidationProblem]:
    problems: list[ValidationProblem] = []
    if not source.upload_id.strip():
        problems.append(
            _p(
                "empty_field",
                f"{prefix}.upload_id",
                f"{prefix}.upload_id must be a non-empty string",
            )
        )
    elif not UPLOAD_ID_PATTERN.match(source.upload_id):
        problems.append(
            _p(
                "invalid_upload_id",
                f"{prefix}.upload_id",
                f"{prefix}.upload_id {source.upload_id!r} is not a valid upload identifier",
                hint="upload_id must come from POST /uploads",
            )
        )
    if source.format not in SOURCE_FILE_FORMATS:
        problems.append(
            _p(
                "unsupported_source_format",
                f"{prefix}.format",
                f"{prefix}.format {source.format!r} is not supported for kind='file'",
                hint=f"Use one of: {', '.join(SOURCE_FILE_FORMATS)}",
            )
        )
    try:
        codecs.lookup(source.encoding)
    except LookupError:
        problems.append(
            _p(
                "unknown_encoding",
                f"{prefix}.encoding",
                f"{prefix}.encoding {source.encoding!r} is not a known encoding",
            )
        )
    return problems


def _url_source_problems(source: SourceRef, prefix: str) -> list[ValidationProblem]:
    problems: list[ValidationProblem] = []
    if source.method not in SOURCE_URL_METHODS:
        problems.append(
            _p(
                "unsupported_source_method",
                f"{prefix}.method",
                f"{prefix}.method {source.method!r} is not supported (P0 supports GET only, #498)",
                hint=f"Use one of: {', '.join(SOURCE_URL_METHODS)}",
            )
        )
    if source.format and source.format not in SOURCE_URL_FORMATS:
        problems.append(
            _p(
                "unsupported_source_format",
                f"{prefix}.format",
                f"{prefix}.format {source.format!r} is not supported for kind='url'",
                hint=f"Use one of: {', '.join(SOURCE_URL_FORMATS)}",
            )
        )
    problems.extend(_endpoint_problems(source.endpoint, f"{prefix}.endpoint"))
    return problems


def _endpoint_problems(endpoint: str, path: str) -> list[ValidationProblem]:
    if not endpoint.strip():
        return [_p("empty_field", path, f"{path} must be a non-empty string")]
    parsed = urlsplit(endpoint)
    problems: list[ValidationProblem] = []
    if parsed.scheme != "https":
        problems.append(
            _p(
                "unsafe_url_scheme",
                path,
                f"{path} must use https (got {parsed.scheme or 'none'!r}) — SSRF policy, #498",
            )
        )
    if parsed.username or parsed.password:
        problems.append(
            _p(
                "url_userinfo_forbidden",
                path,
                f"{path} must not contain userinfo (SSRF policy, #498)",
            )
        )
    if not parsed.hostname:
        problems.append(_p("missing_url_host", path, f"{path} must include a host"))
    return problems


def _source_key_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Catch source declarations with overlapping output keys or unsafe paths (#630).

    Output directories (``bronze/<key>/``, ``silver/<key>/``, ``gold/<key>/``) and
    ``row_counts``/``schema_summaries`` keys all use this value. Same
    ``(provider, dataset)`` declared twice with different params and no alias means
    two sources get same key; sources run concurrently in thread pool and persist
    replaces directories wholesale, so one output vanishes. Only one dict key survives.
    Both outcomes are "ok" so **run reports success but half the data silently disappears**.

    alias becomes path segment. persist's ``validate_path_segment`` eventually catches
    it but only after fetch completes. Stop at declaration stage.
    """
    # spec -> stages top-level import is circular (stages.bronze.resolve reads spec).
    # Duplicating key calculation here risks diverging definitions (#629 was exactly
    # that bug) so keep definition singular, defer import only.
    from ..stages._path_safety import validate_path_segment
    from ..stages.bronze.resolve import source_identity

    problems: list[ValidationProblem] = []
    first_index: dict[str, int] = {}
    for i, source in enumerate(spec.sources):
        if source.alias:
            try:
                validate_path_segment(source.alias, field_name=f"sources[{i}].alias")
            except ValueError as error:
                problems.append(
                    _p(
                        "unsafe_alias",
                        f"sources[{i}].alias",
                        str(error),
                        hint="alias becomes a directory name under the run workspace",
                    )
                )
                continue
            key = source.alias
        else:
            try:
                provider, dataset = source_identity(source)
            except (AttributeError, TypeError):
                # When kind-specific required field empty. _source_kind_problems already
                # reports that; don't repeat here.
                continue
            key = f"{provider}.{dataset}"

        if key in first_index:
            problems.append(
                _p(
                    "duplicate_source_key",
                    f"sources[{i}]",
                    f"sources[{i}] resolves to the same output key {key!r} as "
                    f"sources[{first_index[key]}]; outputs would overwrite each other",
                    hint="give each source a distinct alias",
                )
            )
        else:
            first_index[key] = i
    return problems


def _param_grid_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate ``param_grid`` declaration itself (#613).

    Expansion is Cartesian product so one declaration multiplies call count. Bad
    declaration failing at runtime means hundreds of calls already executed; prevent
    at declaration stage.
    """
    problems: list[ValidationProblem] = []
    for i, source in enumerate(spec.sources):
        if not source.param_grid:
            continue
        prefix = f"sources[{i}].param_grid"
        if source.kind != "public_api":
            problems.append(
                _p(
                    "param_grid_not_supported",
                    prefix,
                    f"param_grid is only valid for kind='public_api', not {source.kind!r}",
                )
            )
            continue
        for key, values in source.param_grid.items():
            field = f"{prefix}.{key}"
            if not values:
                # One empty axis makes entire Cartesian product zero — no calls
                # execute and empty Bronze records as success.
                problems.append(
                    _p(
                        "empty_param_grid_axis",
                        field,
                        f"param_grid axis {key!r} has no values; the expansion would be empty",
                        hint="Remove the axis, or list the values it should iterate over",
                    )
                )
            if key in source.params:
                # Same key on both sides — can't tell which wins from declaration alone.
                problems.append(
                    _p(
                        "param_grid_shadows_params",
                        field,
                        f"{key!r} is declared in both params and param_grid",
                        hint="params carries values shared by every combination; "
                        "keep the axis in only one place",
                    )
                )
            for index, value in enumerate(values):
                if isinstance(value, (dict, list)):
                    # Request parameters are scalar. Nested values can't go to URL.
                    problems.append(
                        _p(
                            "invalid_param_grid_value",
                            f"{field}[{index}]",
                            f"param_grid values must be scalars, got {type(value).__name__}",
                        )
                    )
    return problems


def _pii_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate explicit PII policy violations upfront (#441)."""
    problems: list[ValidationProblem] = []
    if spec.pii is None:
        return problems
    # Public publish (publish=true) spec forbids allow due to PII detection bypass risk.
    if spec.publish and spec.pii.mode == "allow":
        problems.append(
            _p(
                "pii_allow_with_publish",
                "pii.mode",
                "pii.mode='allow' is forbidden when publish=true (공개 배포 PII 노출 위험, #441)",
            )
        )
    return problems


def _quality_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate semantic consistency of quality policy (#486).

    Structure/type/severity vocabulary/operator vocabulary are already rejected by
    loader at parse time. Here we handle only relational constraints loader doesn't
    check (empty ranges, min>max, severity overrides for non-existent columns).
    """
    problems: list[ValidationProblem] = []
    quality = spec.quality
    if quality is None:
        return problems
    for i, rule in enumerate(quality.range):
        path = f"quality.range[{i}]"
        if rule.min is None and rule.max is None:
            problems.append(
                _p(
                    "empty_range_rule",
                    path,
                    f"{path} must declare at least one of min/max",
                )
            )
        elif rule.min is not None and rule.max is not None and rule.min > rule.max:
            problems.append(
                _p(
                    "inverted_range_rule",
                    path,
                    f"{path}.min ({rule.min}) must be <= max ({rule.max})",
                )
            )
    unknown_severity_columns = sorted(
        set(quality.max_null_ratio_severity) - set(quality.max_null_ratio)
    )
    if unknown_severity_columns:
        problems.append(
            _p(
                "unknown_severity_column",
                "quality.max_null_ratio_severity",
                "quality.max_null_ratio_severity references columns not declared in "
                f"quality.max_null_ratio: {unknown_severity_columns}",
            )
        )
    return problems


def _license_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate license requirement for builds with publish=true upfront (#443).

    kpubdata does not provide license metadata, so explicit user-declared
    ``license`` field is the only source of redistributability for public
    distribution.
    """
    problems: list[ValidationProblem] = []
    if spec.publish and not spec.license:
        problems.append(
            _p(
                "missing_license_for_publish",
                "license",
                "license is required when publish=true (재배포 가능성 명시, #443)",
            )
        )
    # `other` is how a licence outside the standard list is recorded, and it carries no
    # terms by itself: a card that says only "other" tells a reader nothing (#764).
    if spec.license == "other":
        for field_name in ("license_name", "license_link"):
            if not getattr(spec, field_name):
                problems.append(
                    _p(
                        "license_other_needs_terms",
                        field_name,
                        f"license is 'other', so {field_name} is required — 'other' alone "
                        "does not say what the terms are",
                    )
                )
    elif spec.license_name or spec.license_link:
        problems.append(
            _p(
                "license_terms_without_other",
                "license",
                "license_name and license_link describe a licence outside the standard "
                "list, so license must be 'other'",
            )
        )
    return problems


def _gold_selection_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """``sources[].gold`` shapes a source's own Gold (#659), which a composition replaces.

    With ``composition`` there is one composed Gold, not one per source, so a per-source
    selection would silently do nothing. It is refused rather than ignored.
    """
    if spec.composition is None:
        return []
    return [
        _p(
            "gold_selection_with_composition",
            f"sources[{i}].gold",
            f"sources[{i}].gold selects a source's own Gold, and composition builds one "
            "composed Gold instead; remove it (#659)",
        )
        for i, source in enumerate(spec.sources)
        if source.gold is not None
    ]


def _composition_problems(spec: BuildSpec) -> list[ValidationProblem]:
    """Validate composition alias references and structural consistency (#506).

    If composition is None, check nothing — existing multi-source BuildSpecs
    without composition are unaffected. Join key existence and dtype compatibility
    require Silver schema (unknown at parse/structure validation time) — handled by
    orchestrator's build pipeline validation gate at runtime.
    """
    problems: list[ValidationProblem] = []
    composition = spec.composition
    if composition is None:
        return problems

    if not composition.name.strip():
        problems.append(
            _p("empty_field", "composition.name", "composition.name must be a non-empty string")
        )
    else:
        existing_output_keys = {
            source.alias if source.alias else f"{source.provider}.{source.dataset}"
            for source in spec.sources
        }
        if composition.name in existing_output_keys:
            problems.append(
                _p(
                    "composition_name_collision",
                    "composition.name",
                    f"composition.name {composition.name!r} collides with an existing source "
                    "output key (alias or provider.dataset)",
                )
            )

    join = composition.join
    aliases = [source.alias for source in spec.sources if source.alias]
    duplicate_aliases = sorted({a for a in aliases if aliases.count(a) > 1})
    if duplicate_aliases:
        problems.append(
            _p(
                "duplicate_source_alias",
                "sources",
                "sources[].alias must be unique when composition is used; "
                f"duplicates: {duplicate_aliases}",
            )
        )

    if join.left and join.left == join.right:
        problems.append(
            _p(
                "self_join",
                "composition.join",
                "composition.join.left and right must reference different sources, "
                f"both are {join.left!r}",
            )
        )

    for side_name, alias in (("left", join.left), ("right", join.right)):
        if not alias.strip():
            problems.append(
                _p(
                    "empty_field",
                    f"composition.join.{side_name}",
                    f"composition.join.{side_name} must be a non-empty string",
                )
            )
            continue
        if not any(source.alias == alias for source in spec.sources):
            problems.append(
                _p(
                    "unknown_composition_source",
                    f"composition.join.{side_name}",
                    f"composition.join.{side_name} {alias!r} does not match any sources[].alias "
                    "(a source referenced by composition must declare a non-empty alias)",
                )
            )

    # A single pair reports under the left_key/right_key shorthand; a composite key
    # reports each pair by index (#698).
    single = len(join.keys) == 1
    for index, (left_column, right_column) in enumerate(join.keys):
        for side_name, column in (("left", left_column), ("right", right_column)):
            if column.strip():
                continue
            path = (
                f"composition.join.{side_name}_key"
                if single
                else f"composition.join.keys[{index}].{side_name}"
            )
            problems.append(_p("empty_field", path, f"{path} must be a non-empty string"))
    for side_index, side_name in ((0, "left"), (1, "right")):
        columns = [pair[side_index] for pair in join.keys]
        repeated = sorted({c for c in columns if columns.count(c) > 1})
        if repeated:
            problems.append(
                _p(
                    "duplicate_join_key_column",
                    "composition.join.keys",
                    f"composition.join.keys names {side_name} column(s) {repeated} more than once",
                )
            )

    return problems


__all__ = ["ValidationProblem", "validate_spec"]

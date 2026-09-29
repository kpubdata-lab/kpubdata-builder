"""BuildSpec data model (Medallion refactor: separated from legacy spec.py)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeAlias

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


#: What to do when a column is absent in column_null_tokens (#623).
ON_ABSENT_POLICIES: tuple[str, ...] = ("error", "ignore")


@dataclass(frozen=True)
class ColumnNullTokens:
    """Column null token declaration and policy (#623)."""

    tokens: tuple[str, ...] = ()
    on_absent: str = "error"


@dataclass(frozen=True)
class SchemaContract:
    """Source schema contract — Silver validation/normalization rules (#437).

    BuildSpec's ``sources[].schema`` declaration is parsed into this model and
    passed by orchestrator/preview as an argument to build_silver_dataset.
    Previously a gate existed but had no passing condition (always OK).

    Attributes:
        required: List of required columns. Passed as required_columns to
            validate_table.
        dtypes: Expected dtype per column (string, ``_NAMED_DTYPES`` keys).
            Passed as column_dtypes to validate_table.
        casts: Per-column casting applied during normalization (string). Passed
            as casts to normalize_table. Null loss from casting is detected with
            audit=True and surfaced as TabularError (#188).
        rename: Source field name → canonical column name mapping (#611).
            Applied before casting, so dtypes/casts/derived all refer to names
            after rename.
        derived: Rules to create new columns from existing ones (#611). Applied
            after casting. If date_parts cannot merge rows with all fragments
            into a date, raises TabularError by the same criteria as casts (#188).
        read_as: Type declaration for reading source columns (``{column: "str"}``).
            Handles source columns with type varying per record as declarations.
            Keys are original field names before rename.
        null_tokens: Source notation for missing values. Collected as null before
            casting. Applies to all string columns.
        column_null_tokens: Column-specific missing value notation (#623).
            Applied **in addition to** global ``null_tokens``. Some sources mark
            the same missing value differently per column; global declarations
            alone cannot express this without changing meaning in other columns.
            Keys are original field names *before* rename; values are
            :class:`ColumnNullTokens`.
        coalesce: Rules to coalesce generation-aliased columns into one canonical
            column (#620). ``{canonical: (candidate1, candidate2, ...)}``. Unlike
            ``rename``, multiple sources merge under one name. Fails if two or
            more candidates are non-null and differ in a single row — silent
            first-wins masks the only signal of misaligned generation boundaries.
            Keys are original field names *before* rename. Coalesced candidate
            columns are absorbed into the canonical column and disappear, but the
            scope is limited to the declared alias group and rows are preserved.
        zfill: Left-pad canonical identifiers to declared width with zeros (#620).
            ``{column: width}``. Keys are names *after* rename.

    Application order is ``read_as -> null_tokens -> coalesce -> rename -> zfill
    -> casts -> derived``. null_tokens comes before coalesce because missing
    notation is still string; coalesce would treat it as a value and conflict.
    zfill comes after rename because the declaration refers to canonical names.
    """

    required: tuple[str, ...] = ()
    dtypes: dict[str, str] = field(default_factory=dict)
    casts: dict[str, str] = field(default_factory=dict)
    rename: dict[str, str] = field(default_factory=dict)
    derived: tuple[DerivedColumn, ...] = ()
    read_as: dict[str, str] = field(default_factory=dict)
    null_tokens: tuple[str, ...] = ()
    column_null_tokens: dict[str, ColumnNullTokens] = field(default_factory=dict)
    coalesce: dict[str, tuple[str, ...]] = field(default_factory=dict)
    zfill: dict[str, int] = field(default_factory=dict)


#: Supported DerivedColumn.kind values (#611). Expressed as typed rules instead
#: of free-form expressions — follows the convention of RangeRule/CompareColumnsRule.
DERIVED_KINDS: tuple[str, ...] = ("date_parts", "join_key")

#: Types allowed by schema.read_as. Accepts only strings for resolving mixed types.
READ_AS_TYPES: tuple[str, ...] = ("str",)


@dataclass(frozen=True)
class DerivedColumn:
    """Rules to create new columns from existing ones (#611).

    Attributes:
        name: Name of the column to be created.
        kind: Derivation method. ``"date_parts"`` merges year/month/day three
            columns into a Date; ``"join_key"`` merges multiple columns into one
            composite string key.
        columns: Input column names. date_parts requires exactly 3 in (year,
            month, day) order; join_key requires 1 or more.
    """

    name: str
    kind: str
    columns: tuple[str, ...]


#: Supported SourceRef.kind values (#498). Loader immediately rejects unknown kinds.
SOURCE_KINDS: tuple[str, ...] = ("public_api", "file", "url")

#: HTTP methods allowed in url kind (#498 P0 — only GET, Auth=None supported).
SOURCE_URL_METHODS: tuple[str, ...] = ("GET",)

#: Upload formats allowed in file kind (#498 P0). Excel/ZIP are out of scope.
SOURCE_FILE_FORMATS: tuple[str, ...] = ("csv", "json", "jsonl", "parquet")

#: Response formats allowed in url kind (#498 P0). Inferred from Content-Type if unspecified.
SOURCE_URL_FORMATS: tuple[str, ...] = ("json", "jsonl", "csv")

#: Upload ID format issued by server (#498). Only ``POST /uploads`` creates IDs
#: of this form — loader/validator pin shape to this pattern to prevent users
#: from injecting arbitrary strings as upload_id and manipulating filesystem/queries.
UPLOAD_ID_PATTERN: re.Pattern[str] = re.compile(r"^upl_[a-f0-9]{32}$")


@dataclass(frozen=True)
class SourceRef:
    """Canonical source reference — represents three kinds: Public API/File/URL (#498)."""

    provider: str = ""
    dataset: str = ""
    params: dict[str, JsonValue] = field(default_factory=dict)
    param_grid: dict[str, tuple[JsonValue, ...]] = field(default_factory=dict)
    alias: str = ""
    schema: SchemaContract | None = None
    kind: str = "public_api"
    upload_id: str = ""
    format: str = ""
    encoding: str = "utf-8"
    endpoint: str = ""
    method: str = "GET"


@dataclass(frozen=True)
class ExportTarget:
    """Concrete export target definition for build.

    Attributes:
        kind: Registry key for exporter.
        output_path: Relative output path from output_dir.
        options: Exporter-specific optional settings.
    """

    kind: str
    output_path: str
    options: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class SplitSpec:
    """Definition of how to divide dataset into named splits (#38).

    Attributes:
        mode: Split method. "ratio" is ratio-based (train/val/test); "key" is
            column value-based (year/region/category).
        ratios: Mapping of split name → ratio for ratio mode (sum must be 1.0).
        key: Column name used as split criterion in key mode.
        seed: Deterministic shuffle seed for ratio mode.
    """

    mode: str
    ratios: dict[str, float] = field(default_factory=dict)
    key: str = ""
    seed: int = 0


@dataclass(frozen=True)
class PiiPolicy:
    """PII detection policy (#441, QG-1)."""

    mode: str = "block"
    allow_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class RangeRule:
    """Min/max range rule for numeric columns (#486).

    Expressed as typed rule instead of free-form Python/eval. min/max are
    inclusive boundaries. Null values do not count as range violations — missing
    value handling is managed separately by max_null_ratio (separation of concerns).

    Attributes:
        column: Target column name.
        min: Allowed minimum value (inclusive). None skips lower bound check.
        max: Allowed maximum value (inclusive). None skips upper bound check.
        severity: Severity on violation — ``"warn"`` (default) | ``"fail"``.
    """

    column: str
    min: float | None = None
    max: float | None = None
    severity: str = "warn"


@dataclass(frozen=True)
class CompareColumnsRule:
    """Comparison rule between two columns (#486).

    Prohibits free-form expression/eval; allows only a restricted operator set.
    Only rows where both columns are non-null are evaluated (rows that cannot be
    compared do not auto-pass).

    Attributes:
        left: Left column name.
        operator: One of ``eq``/``ne``/``gt``/``gte``/``lt``/``lte``.
        right: Right column name.
        severity: Severity on violation — ``"warn"`` (default) | ``"fail"``.
    """

    left: str
    operator: str
    right: str
    severity: str = "warn"


@dataclass(frozen=True)
class QualityPolicy:
    """Data quality threshold policy (#446, QG-3; range/compare_columns/severity
    are #486).

    Thresholds on Silver statistics (row_count/null_counts/duplicate_rate) and
    the table itself (range/compare_columns). On excess, violations are reported
    as structured QualityCheckResult (quality.evaluator) and gated by severity:
    WARN (continue) or FAIL (source failure before Gold entry).

    Type and default severity (``"warn"``) of existing 3 fields (max_duplicate_rate
    /max_null_ratio/min_rows) are preserved from #446 — backward compat. If
    explicit FAIL is needed, declare the corresponding ``*_severity`` field
    together.

    Attributes:
        max_duplicate_rate: Max duplicate row ratio allowed (0.0~1.0). Excess is
            violation.
        max_duplicate_rate_severity: Violation severity. Default ``"warn"``.
        max_null_ratio: Max null ratio per column allowed (``{column: ratio}``).
        max_null_ratio_severity: Per-column violation severity override
            (``{column: severity}``). Undeclared columns default ``"warn"``.
        min_rows: Minimum row count. Below is violation.
        min_rows_severity: Violation severity. Default ``"warn"``.
        range: Per-column min/max range rule list (#486).
        compare_columns: Column comparison rule list (#486).
    """

    max_duplicate_rate: float | None = None
    max_duplicate_rate_severity: str = "warn"
    max_null_ratio: dict[str, float] = field(default_factory=dict)
    max_null_ratio_severity: dict[str, str] = field(default_factory=dict)
    min_rows: int | None = None
    min_rows_severity: str = "warn"
    range: tuple[RangeRule, ...] = ()
    compare_columns: tuple[CompareColumnsRule, ...] = ()


@dataclass(frozen=True)
class JoinSpec:
    """Equi-join contract to combine two sources (#506, #698).

    Structure (reference alias existence, type/severity vocabulary) is validated
    by spec.validator after parsing. Actual existence of join keys and dtype
    compatibility cannot be known at parse time (requires Silver schema) — that
    is verified by orchestrator's build pipeline gate (runtime), not spec.validator.
    "Validate during the validate phase" in completion conditions should read as
    encompassing both this structure validation and pipeline gate.

    The join key is either the single-pair shorthand ``left_key``/``right_key`` or
    a composite ``keys`` tuple of ``(left_column, right_column)`` pairs (#698).
    Both forms are normalised on construction: ``keys`` always holds every pair,
    and ``left_key``/``right_key`` always name the first pair, so callers that
    only understand a single key keep working.

    Attributes:
        left: Alias of left source (see BuildSpec.sources[].alias).
        right: Alias of right source.
        left_key: Join key column name in left table (first pair of ``keys``).
        right_key: Join key column name in right table (first pair of ``keys``).
        type: Join kind. "inner" | "left" (initial scope, #506).
        on_duplicate_key: Action when a key that appears on both sides repeats on
            both sides, so matching rows multiply many-to-many. "warn" (default,
            only log to manifest) | "fail" (build failure). Follows QualityPolicy
            severity convention. Keys that do not intersect never trigger it (#698).
        keys: Composite join key as ``(left_column, right_column)`` pairs (#698).
        cardinality: Declared cardinality verified against the intersecting keys.
            "one_to_one" | "one_to_many" | "many_to_one" | "many_to_many", or None
            when not declared (#698).
        on_null_key: Action when a row has a null in any key column and so can
            never match. "warn" (default, counted in the manifest) | "fail" (#698).
    """

    left: str
    right: str
    left_key: str = ""
    right_key: str = ""
    type: str = "inner"
    on_duplicate_key: str = "warn"
    keys: tuple[tuple[str, str], ...] = ()
    cardinality: str | None = None
    on_null_key: str = "warn"

    def __post_init__(self) -> None:
        if not self.keys:
            object.__setattr__(self, "keys", ((self.left_key, self.right_key),))
            return
        keys = tuple((str(pair[0]), str(pair[1])) for pair in self.keys)
        object.__setattr__(self, "keys", keys)
        first_left, first_right = keys[0]
        if (self.left_key or self.right_key) and (self.left_key, self.right_key) != (
            first_left,
            first_right,
        ):
            raise ValueError(
                "JoinSpec takes either keys or left_key/right_key, not both "
                f"(keys={list(keys)!r}, left_key={self.left_key!r}, "
                f"right_key={self.right_key!r})"
            )
        object.__setattr__(self, "left_key", first_left)
        object.__setattr__(self, "right_key", first_right)


@dataclass(frozen=True)
class CompositionSpec:
    """Contract to assemble multiple sources into one Gold dataset (#506).

    Initial scope is limited to two-source single equi-join (3+ join graphs are
    out of scope) — thus receives single JoinSpec, not a list.

    Attributes:
        name: Name of combined Gold dataset. Also used as gold/{name}/ output
            directory segment; must not overlap with other source output keys
            (alias or provider.dataset).
        join: Single JoinSpec used for combination.
    """

    name: str
    join: JoinSpec


@dataclass(frozen=True)
class BuildSpec:
    """Declarative build specification for dataset artifacts.

    Attributes:
        dataset_id: Global identifier for the dataset.
        title: Human-readable title.
        description: Build purpose and data description.
        sources: List of input sources.
        exports: List of output targets.
        metadata: Arbitrary metadata to include in artifacts.
        publish: Whether to publish after build.
        splits: Dataset split definition. None means no split.
        pii: PII scan policy. None skips scan (backward compat, #441).
        license: Dataset license/usage terms (SPDX identifier or free text).
            Must be declared when ``publish=True`` (#443). kpubdata does not
            provide license metadata, so user explicit declaration is the only
            source.
        attribution: Attribution text. KOGL requires attribution for
            types 1-4, but license alone cannot convey it — requires both type
            and institution and original URL. When declared, appears in the
            dataset card's "Source" section. Maps to legacy publish config's
            ``card.attribution`` (ADR 0018).
        quality: Data quality threshold policy. None skips check (backward compat,
            #446).
        composition: Contract to join two sources into one Gold dataset. None
            generates independent Gold per source (backward compat, #506).

    Example:
        >>> BuildSpec.from_yaml("specs/sample.yaml")
    """

    dataset_id: str
    title: str
    description: str
    sources: tuple[SourceRef, ...]
    exports: tuple[ExportTarget, ...]
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    publish: bool = False
    splits: SplitSpec | None = None
    pii: PiiPolicy | None = None
    license: str | None = None
    attribution: str | None = None
    quality: QualityPolicy | None = None
    composition: CompositionSpec | None = None

    @classmethod
    def from_yaml(cls, path: str | Path) -> BuildSpec:
        """Load BuildSpec from YAML file."""
        import warnings

        warnings.warn(
            "BuildSpec.from_yaml() is deprecated, use load_spec() instead",
            DeprecationWarning,
            stacklevel=2,
        )
        # Delayed import to avoid circular import between models <-> loader.
        from .loader import load_spec

        return load_spec(Path(path))


__all__ = [
    "BuildSpec",
    "DERIVED_KINDS",
    "DerivedColumn",
    "CompositionSpec",
    "ExportTarget",
    "JoinSpec",
    "JsonPrimitive",
    "JsonValue",
    "SOURCE_FILE_FORMATS",
    "SOURCE_KINDS",
    "SOURCE_URL_FORMATS",
    "SOURCE_URL_METHODS",
    "SourceRef",
    "SplitSpec",
    "UPLOAD_ID_PATTERN",
]

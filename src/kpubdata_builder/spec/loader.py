"""BuildSpec YAML loading/parsing (Medallion refactor: separated from legacy spec.py).

This module reads YAML text and structures it as an in-memory mapping,
then converts it to immutable dataclasses in models.py. Type and required key
validation failures are converted to SpecLoadError.

Main functions:
    - load_spec: Convert YAML file path to BuildSpec.
    - parse_spec: Parse already-loaded mapping into BuildSpec.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import cast

import yaml

from ..errors import SpecLoadError
from .models import (
    SOURCE_KINDS,
    BuildSpec,
    ColumnNullTokens,
    CompareColumnsRule,
    CompositionSpec,
    DerivedColumn,
    ExportTarget,
    JoinSpec,
    JsonValue,
    PiiPolicy,
    QualityPolicy,
    RangeRule,
    SchemaContract,
    SourceRef,
    SplitSpec,
)

# JoinSpec.type permitted vocabulary (#506). Initial scope limited to equi-join inner/left.
_JOIN_TYPES = ("inner", "left")

# JoinSpec.on_duplicate_key permitted vocabulary (#506). Reuses quality severity convention.
_JOIN_DUPLICATE_KEY_SEVERITIES = ("warn", "fail")

# JoinSpec.on_null_key permitted vocabulary (#698). Same severity convention.
_JOIN_NULL_KEY_SEVERITIES = ("warn", "fail")

# JoinSpec.cardinality permitted vocabulary (#698). Read left-to-right: "one_to_many"
# means a key is unique on the left and may repeat on the right.
_JOIN_CARDINALITIES = ("one_to_one", "one_to_many", "many_to_one", "many_to_many")

# quality.*_severity allowed vocabulary (#486). Default "warn" for existing threshold
# violations; must explicitly declare "fail" for Gold entry before source failure.
_QUALITY_SEVERITIES = ("warn", "fail")

# quality.compare_columns[].operator permitted vocabulary (#486). Freeform
# expression/eval is forbidden; only this set is allowed — invalid operators are
# rejected immediately at parse time.
_COMPARE_COLUMNS_OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte")


def parse_spec(data: dict[str, object]) -> BuildSpec:
    """Parse in-memory mapping data into BuildSpec.

    Args:
        data: Top-level mapping returned by YAML loader.

    Returns:
        BuildSpec: Validated build specification object.

    Raises:
        SpecLoadError: When field types mismatch or required keys are missing.
    """
    try:
        dataset_id = _require_string(data, "dataset_id")
        title = _require_string(data, "title")
        description = _require_string(data, "description")
        # transforms field removed (#438). Replaced by sources[].schema.casts in VAL-1.
        # Do not silently ignore; raise explicit error to inform users.
        if "transforms" in data:
            raise ValueError("'transforms' is removed; use sources[].schema.casts instead (#438)")
        if "normalization_mode" in data:
            raise ValueError("'normalization_mode' is removed; use sources[].schema instead (#438)")
        metadata = _parse_json_mapping(data.get("metadata", {}), field_name="metadata")
        publish = _parse_bool(data.get("publish", False), field_name="publish")
        sources = _parse_sources(_require_present(data, "sources"))
        # exports is optional (#703). A warehouse build that only materialises a
        # table is a complete job, and requiring an export target made the common
        # case pay for the rare one — a local analysis had to declare where to
        # publish before it could finish.
        exports = _parse_exports(data.get("exports", []))
        splits = _parse_splits(data.get("splits"))
        pii = _parse_pii(data.get("pii"))
        license_obj = data.get("license")
        if license_obj is not None and not isinstance(license_obj, str):
            raise TypeError("license must be a string")
        attribution_obj = data.get("attribution")
        if attribution_obj is not None and not isinstance(attribution_obj, str):
            raise TypeError("attribution must be a string")
        quality = _parse_quality(data.get("quality"))
        composition = _parse_composition(data.get("composition"))
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecLoadError(f"Failed to parse build spec: {exc}") from exc

    return BuildSpec(
        dataset_id=dataset_id,
        title=title,
        description=description,
        sources=sources,
        exports=exports,
        metadata=metadata,
        publish=publish,
        splits=splits,
        pii=pii,
        license=license_obj,
        attribution=attribution_obj,
        quality=quality,
        composition=composition,
    )


def load_spec(path: Path) -> BuildSpec:
    """Read YAML file and convert to BuildSpec."""
    try:
        raw_data = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, yaml.YAMLError) as exc:
        raise SpecLoadError(f"Failed to load build spec from {path}: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise SpecLoadError(
            f"Failed to parse build spec from {path}: top-level YAML must be a mapping"
        )

    return parse_spec(cast(dict[str, object], raw_data))


def _require_string(data: dict[str, object], key: str, *, prefix: str = "") -> str:
    """Extract required string field."""
    label = f"{prefix}.{key}" if prefix else key
    if key not in data:
        raise KeyError(f"{label} is required")
    value = data[key]
    if not isinstance(value, str):
        raise TypeError(f"{label} must be a string")
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def _require_present(data: dict[str, object], key: str) -> object:
    if key not in data:
        raise KeyError(f"{key} is required")
    return data[key]


def _parse_bool(value: object, *, field_name: str) -> bool:
    """Validate boolean field type."""
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _parse_string_list(value: object, *, field_name: str) -> tuple[str, ...]:
    """Convert string list field to immutable tuple."""
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list")

    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise TypeError(f"{field_name} entries must be strings")
    return tuple(cast(str, item) for item in items)


def _parse_string_dict(value: object, *, field_name: str) -> dict[str, JsonValue]:
    """Validate string key/value mapping and copy to new dict."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, JsonValue] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise TypeError(f"{field_name} entries must be string pairs")
        parsed[key] = item
    return parsed


def _validate_json_value(
    value: object, *, field_name: str, _ancestors: frozenset[int] = frozenset()
) -> JsonValue:
    """Recursively validate that value is JSON primitive/container.

    Circular structures created by YAML anchor/alias (e.g. ``a: &x {self: *x}``)
    can cause infinite recursion and RecursionError crash. Track container ``id()``
    in the current recursion path to detect cycles and fail explicitly with ValueError
    (wrapped by load_spec into SpecLoadError), rather than crash (#169).
    """
    if isinstance(value, float) and not math.isfinite(value):
        # NaN/Infinity serialize as non-standard tokens by json.dumps, so reject
        # them globally to preserve standard JSON contract (#201).
        raise ValueError(f"{field_name} must be a finite number, got {value!r}")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, dict)):
        marker = id(value)
        if marker in _ancestors:
            raise ValueError(f"{field_name} contains a circular reference")
        child_ancestors = _ancestors | {marker}
        if isinstance(value, list):
            return [
                _validate_json_value(
                    item, field_name=f"{field_name}[{i}]", _ancestors=child_ancestors
                )
                for i, item in enumerate(value)
            ]
        result: dict[str, JsonValue] = {}
        for k, v in cast(dict[object, object], value).items():
            if not isinstance(k, str):
                raise TypeError(f"{field_name} keys must be strings, got {type(k).__name__}")
            result[k] = _validate_json_value(
                v, field_name=f"{field_name}.{k}", _ancestors=child_ancestors
            )
        return result
    raise TypeError(
        f"{field_name} contains non-JSON value of type {type(value).__name__}: {value!r}"
    )


def _parse_json_mapping(value: object, *, field_name: str) -> dict[str, JsonValue]:
    """Validate mapping fields containing only JSON-compatible values."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, JsonValue] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = _validate_json_value(item, field_name=f"{field_name}.{key}")
    return parsed


def _parse_param_grid(value: object, *, field_name: str) -> dict[str, tuple[JsonValue, ...]]:
    """Parse ``param_grid`` to key-value tuples (#613)."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, tuple[JsonValue, ...]] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        if not isinstance(item, list):
            raise TypeError(
                f"{field_name}.{key} must be a list of values; "
                "a single shared value belongs in params"
            )
        values = cast(list[object], item)
        parsed[key] = tuple(
            _validate_json_value(entry, field_name=f"{field_name}.{key}[{i}]")
            for i, entry in enumerate(values)
        )
    return parsed


# Fields valid only per kind (#498). Different kind fields in one source is
# an explicit contract violation, so loader rejects immediately — do not silently
# interpret ambiguous specs like "kind=file but provider is present".
_PUBLIC_API_ONLY_FIELDS: tuple[str, ...] = ("upload_id", "format", "encoding", "endpoint", "method")
_FILE_ONLY_FIELDS: tuple[str, ...] = (
    "provider",
    "dataset",
    "params",
    "param_grid",
    "endpoint",
    "method",
)
_URL_ONLY_FIELDS: tuple[str, ...] = (
    "provider",
    "dataset",
    "params",
    "param_grid",
    "upload_id",
    "encoding",
)


def _reject_foreign_fields(
    mapping: dict[str, object], forbidden: tuple[str, ...], *, prefix: str, kind: str
) -> None:
    present = sorted(name for name in forbidden if name in mapping)
    if present:
        raise TypeError(f"{prefix}: fields {present} are not valid for kind={kind!r} (#498)")


def _parse_sources(value: object) -> tuple[SourceRef, ...]:
    """Convert sources array to SourceRef tuple (#498).

    Distinguish public_api (default, backward compatible)/file/url by ``kind``
    and parse different field combinations. Omitted ``kind`` on existing source
    is always interpreted as ``public_api`` — existing Public API BuildSpec
    works unchanged.
    """
    if not isinstance(value, list):
        raise TypeError("sources must be a list")
    if not value:
        raise ValueError("sources must not be empty")

    items = cast(list[object], value)
    parsed_sources: list[SourceRef] = []
    for index, item in enumerate(items):
        prefix = f"sources[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        # normalization_mode field removed (#438). Replaced by sources[].schema.
        # Do not silently ignore; raise explicit error.
        if "normalization_mode" in mapping:
            raise TypeError(
                f"sources[{index}].normalization_mode is removed; "
                "use sources[].schema instead (#438)"
            )
        kind_obj = mapping.get("kind", "public_api")
        if not isinstance(kind_obj, str):
            raise TypeError(f"{prefix}.kind must be a string")
        if kind_obj not in SOURCE_KINDS:
            raise ValueError(
                f"{prefix}.kind {kind_obj!r} is not supported; use one of {SOURCE_KINDS} (#498)"
            )
        alias_obj = mapping.get("alias", "")
        if not isinstance(alias_obj, str):
            raise TypeError(f"sources[{index}].alias must be a string")
        schema_obj = mapping.get("schema")
        schema = _parse_schema(schema_obj, prefix=prefix) if schema_obj is not None else None

        if kind_obj == "file":
            parsed_sources.append(
                _parse_file_source(mapping, index=index, alias=alias_obj, schema=schema)
            )
        elif kind_obj == "url":
            parsed_sources.append(
                _parse_url_source(mapping, index=index, alias=alias_obj, schema=schema)
            )
        else:
            parsed_sources.append(
                _parse_public_api_source(mapping, index=index, alias=alias_obj, schema=schema)
            )
    return tuple(parsed_sources)


def _parse_public_api_source(
    mapping: dict[str, object], *, index: int, alias: str, schema: SchemaContract | None
) -> SourceRef:
    prefix = f"sources[{index}]"
    _reject_foreign_fields(mapping, _PUBLIC_API_ONLY_FIELDS, prefix=prefix, kind="public_api")
    provider = _require_string(mapping, "provider", prefix=prefix)
    dataset = _require_string(mapping, "dataset", prefix=prefix)
    params = _parse_json_mapping(mapping.get("params", {}), field_name=f"{prefix}.params")
    param_grid = _parse_param_grid(mapping.get("param_grid", {}), field_name=f"{prefix}.param_grid")
    return SourceRef(
        provider=provider,
        dataset=dataset,
        params=params,
        param_grid=param_grid,
        alias=alias,
        schema=schema,
        kind="public_api",
    )


def _parse_file_source(
    mapping: dict[str, object], *, index: int, alias: str, schema: SchemaContract | None
) -> SourceRef:
    """Parse kind='file' source (#498). Semantic validation of value vocabulary
    (allowed format/encoding existence, upload_id shape) is handled by validator.py."""
    prefix = f"sources[{index}]"
    _reject_foreign_fields(mapping, _FILE_ONLY_FIELDS, prefix=prefix, kind="file")
    upload_id = _require_string(mapping, "upload_id", prefix=prefix)
    format_value = _require_string(mapping, "format", prefix=prefix)
    encoding_obj = mapping.get("encoding", "utf-8")
    if not isinstance(encoding_obj, str) or not encoding_obj.strip():
        raise TypeError(f"{prefix}.encoding must be a non-empty string")
    return SourceRef(
        alias=alias,
        schema=schema,
        kind="file",
        upload_id=upload_id,
        format=format_value,
        encoding=encoding_obj,
    )


def _parse_url_source(
    mapping: dict[str, object], *, index: int, alias: str, schema: SchemaContract | None
) -> SourceRef:
    """Parse kind='url' source (#498). SSRF-related semantic validation like
    scheme/userinfo/method vocabulary is handled by validator.py — here only structure."""
    prefix = f"sources[{index}]"
    _reject_foreign_fields(mapping, _URL_ONLY_FIELDS, prefix=prefix, kind="url")
    endpoint = _require_string(mapping, "endpoint", prefix=prefix)
    method_obj = mapping.get("method", "GET")
    if not isinstance(method_obj, str) or not method_obj.strip():
        raise TypeError(f"{prefix}.method must be a non-empty string")
    format_obj = mapping.get("format", "")
    if not isinstance(format_obj, str):
        raise TypeError(f"{prefix}.format must be a string")
    return SourceRef(
        alias=alias,
        schema=schema,
        kind="url",
        endpoint=endpoint,
        method=method_obj,
        format=format_obj,
    )


def _parse_schema(value: object, *, prefix: str) -> SchemaContract:
    """Convert sources[].schema mapping to SchemaContract (#437).

    Parse three fields: required/dtypes/casts. dtype/cast values must be strings;
    actual interpretation as polars dtype is validated by validator.py (loader
    checks structure only).
    """
    mapping = _ensure_mapping(value, field_name=f"{prefix}.schema")
    required = _parse_string_list(
        mapping.get("required", []), field_name=f"{prefix}.schema.required"
    )
    dtypes = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("dtypes", {}), field_name=f"{prefix}.schema.dtypes"),
    )
    casts = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("casts", {}), field_name=f"{prefix}.schema.casts"),
    )
    rename = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("rename", {}), field_name=f"{prefix}.schema.rename"),
    )
    derived = _parse_derived(mapping.get("derived", []), prefix=f"{prefix}.schema.derived")
    read_as = cast(
        dict[str, str],
        _parse_string_dict(mapping.get("read_as", {}), field_name=f"{prefix}.schema.read_as"),
    )
    null_tokens = _parse_string_list(
        mapping.get("null_tokens", []), field_name=f"{prefix}.schema.null_tokens"
    )
    column_null_tokens = _parse_column_null_tokens(
        mapping.get("column_null_tokens", {}), prefix=f"{prefix}.schema.column_null_tokens"
    )
    coalesce = _parse_coalesce(mapping.get("coalesce", {}), prefix=f"{prefix}.schema.coalesce")
    zfill = _parse_zfill(mapping.get("zfill", {}), prefix=f"{prefix}.schema.zfill")
    return SchemaContract(
        required=required,
        dtypes=dtypes,
        casts=casts,
        rename=rename,
        derived=derived,
        read_as=read_as,
        null_tokens=null_tokens,
        column_null_tokens=column_null_tokens,
        coalesce=coalesce,
        zfill=zfill,
    )


def _parse_column_null_tokens(value: object, *, prefix: str) -> dict[str, ColumnNullTokens]:
    """Parse schema.column_null_tokens (#623).

    Accepts two notations. When using only list, ``on_absent`` defaults to ``"error"``.

        column_null_tokens:
          foo: ["", "NA"]
          bar:
            tokens: [""]
            on_absent: ignore

    Semantic validation of ``on_absent`` vocabulary is handled by validator.py —
    loader checks structure only.
    """
    mapping = _ensure_mapping(value, field_name=prefix)
    parsed: dict[str, ColumnNullTokens] = {}
    for column, declaration in mapping.items():
        field = f"{prefix}.{column}"
        if isinstance(declaration, dict):
            unknown = set(declaration) - {"tokens", "on_absent"}
            if unknown:
                raise TypeError(f"{field} has unknown keys: {sorted(unknown)}")
            tokens = _parse_string_list(declaration.get("tokens", []), field_name=f"{field}.tokens")
            on_absent = declaration.get("on_absent", "error")
            if not isinstance(on_absent, str):
                raise TypeError(f"{field}.on_absent must be a string")
        else:
            tokens = _parse_string_list(declaration, field_name=field)
            on_absent = "error"
        parsed[column] = ColumnNullTokens(tokens=tokens, on_absent=on_absent)
    return parsed


def _parse_coalesce(value: object, *, prefix: str) -> dict[str, tuple[str, ...]]:
    """Parse ``{name: (string, ...)}`` form declarations (#620, #623).

    schema.coalesce and schema.column_null_tokens share shape, used together.

    Check structure only — semantic validation like empty candidates is handled
    by validator.py.
    """
    mapping = _ensure_mapping(value, field_name=prefix)
    parsed: dict[str, tuple[str, ...]] = {}
    for target, candidates in mapping.items():
        parsed[target] = _parse_string_list(candidates, field_name=f"{prefix}.{target}")
    return parsed


def _parse_zfill(value: object, *, prefix: str) -> dict[str, int]:
    """Convert schema.zfill to ``{column: width}`` (#620)."""
    mapping = _ensure_mapping(value, field_name=prefix)
    parsed: dict[str, int] = {}
    for column, width in mapping.items():
        if not isinstance(width, int) or isinstance(width, bool):
            raise TypeError(f"{prefix}.{column} must be an integer width")
        parsed[column] = width
    return parsed


def _parse_derived(value: object, *, prefix: str) -> tuple[DerivedColumn, ...]:
    """Convert schema.derived array to DerivedColumn tuple (#611).

    Check structure only — semantic validation of kind vocabulary and column
    count is handled by validator.py.
    """
    if not isinstance(value, list):
        raise TypeError(f"{prefix} must be a list")
    rules: list[DerivedColumn] = []
    for index, item in enumerate(cast(list[object], value)):
        mapping = _ensure_mapping(item, field_name=f"{prefix}[{index}]")
        name = mapping.get("name")
        kind = mapping.get("kind")
        if not isinstance(name, str) or not name:
            raise TypeError(f"{prefix}[{index}].name must be a non-empty string")
        if not isinstance(kind, str) or not kind:
            raise TypeError(f"{prefix}[{index}].kind must be a non-empty string")
        columns = _parse_string_list(
            mapping.get("columns", []), field_name=f"{prefix}[{index}].columns"
        )
        rules.append(DerivedColumn(name=name, kind=kind, columns=columns))
    return tuple(rules)


def _parse_exports(value: object) -> tuple[ExportTarget, ...]:
    """Convert the exports array into a tuple of ExportTarget.

    An empty list is valid (#703): the build then ends at a materialised table
    instead of at an exported artifact.
    """
    if not isinstance(value, list):
        raise TypeError("exports must be a list")

    items = cast(list[object], value)
    parsed_exports: list[ExportTarget] = []
    for index, item in enumerate(items):
        prefix = f"exports[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        kind = _require_string(mapping, "kind", prefix=prefix)
        output_path = _require_string(mapping, "output_path", prefix=prefix)
        options = _parse_json_mapping(
            mapping.get("options", {}), field_name=f"exports[{index}].options"
        )
        parsed_exports.append(ExportTarget(kind=kind, output_path=output_path, options=options))
    return tuple(parsed_exports)


def _parse_splits(value: object) -> SplitSpec | None:
    """Convert the splits mapping into a SplitSpec (None when absent)."""
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="splits")
    mode = _require_string(mapping, "mode", prefix="splits")

    seed_obj = mapping.get("seed", 0)
    if not isinstance(seed_obj, int) or isinstance(seed_obj, bool):
        raise TypeError("splits.seed must be an integer")

    ratios: dict[str, float] = {}
    ratios_obj = mapping.get("ratios", {})
    if not isinstance(ratios_obj, dict):
        raise TypeError("splits.ratios must be a mapping")
    for name, fraction in cast(dict[object, object], ratios_obj).items():
        if not isinstance(name, str):
            raise TypeError("splits.ratios keys must be strings")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise TypeError("splits.ratios values must be numbers")
        ratios[name] = float(fraction)

    key_obj = mapping.get("key", "")
    if not isinstance(key_obj, str):
        raise TypeError("splits.key must be a string")

    return SplitSpec(mode=mode, ratios=ratios, key=key_obj, seed=seed_obj)


def _ensure_mapping(value: object, *, field_name: str) -> dict[str, object]:
    """Verify mapping with string keys and return copy."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")

    raw_mapping = cast(dict[object, object], value)
    parsed: dict[str, object] = {}
    for key, item in raw_mapping.items():
        if not isinstance(key, str):
            raise TypeError(f"{field_name} keys must be strings")
        parsed[key] = item
    return parsed


def _parse_pii(value: object) -> PiiPolicy | None:
    """Convert pii mapping to PiiPolicy (None if absent, #441).

    mode is one of block (default)/warn/allow. allow_columns is list of columns
    to exclude false positives.
    """
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="pii")
    mode_obj = mapping.get("mode", "block")
    if not isinstance(mode_obj, str):
        raise TypeError("pii.mode must be a string")
    if mode_obj not in ("block", "warn", "allow"):
        raise ValueError(f"pii.mode must be one of block/warn/allow, got {mode_obj!r}")
    allow_columns = _parse_string_list(
        mapping.get("allow_columns", []), field_name="pii.allow_columns"
    )
    return PiiPolicy(mode=mode_obj, allow_columns=allow_columns)


def _parse_severity(value: object, *, field_name: str) -> str:
    """Validate quality severity value ("warn"/"fail") (#486)."""
    if not isinstance(value, str) or value not in _QUALITY_SEVERITIES:
        raise ValueError(f"{field_name} must be one of {_QUALITY_SEVERITIES}, got {value!r}")
    return value


def _parse_severity_map(value: object, *, field_name: str) -> dict[str, str]:
    """Validate per-column severity override mapping (#486)."""
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for k, v in cast(dict[object, object], value).items():
        if not isinstance(k, str):
            raise TypeError(f"{field_name} keys must be strings")
        result[k] = _parse_severity(v, field_name=f"{field_name}.{k}")
    return result


def _parse_optional_number(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{field_name} must be a number")
    return float(value)


def _parse_range_rules(value: object) -> tuple[RangeRule, ...]:
    """Convert quality.range array to RangeRule tuple (#486)."""
    if not isinstance(value, list):
        raise TypeError("quality.range must be a list")
    rules: list[RangeRule] = []
    for index, item in enumerate(cast(list[object], value)):
        prefix = f"quality.range[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        column = _require_string(mapping, "column", prefix=prefix)
        min_value = _parse_optional_number(mapping.get("min"), field_name=f"{prefix}.min")
        max_value = _parse_optional_number(mapping.get("max"), field_name=f"{prefix}.max")
        severity = _parse_severity(mapping.get("severity", "warn"), field_name=f"{prefix}.severity")
        rules.append(RangeRule(column=column, min=min_value, max=max_value, severity=severity))
    return tuple(rules)


def _parse_compare_columns_rules(value: object) -> tuple[CompareColumnsRule, ...]:
    """Convert quality.compare_columns array to CompareColumnsRule tuple (#486).

    Freeform expression/eval is forbidden; only ``_COMPARE_COLUMNS_OPERATORS``
    are allowed — invalid operators are rejected immediately here.
    """
    if not isinstance(value, list):
        raise TypeError("quality.compare_columns must be a list")
    rules: list[CompareColumnsRule] = []
    for index, item in enumerate(cast(list[object], value)):
        prefix = f"quality.compare_columns[{index}]"
        mapping = _ensure_mapping(item, field_name=prefix)
        left = _require_string(mapping, "left", prefix=prefix)
        right = _require_string(mapping, "right", prefix=prefix)
        operator = _require_string(mapping, "operator", prefix=prefix)
        if operator not in _COMPARE_COLUMNS_OPERATORS:
            raise ValueError(
                f"{prefix}.operator must be one of {_COMPARE_COLUMNS_OPERATORS}, got {operator!r}"
            )
        severity = _parse_severity(mapping.get("severity", "warn"), field_name=f"{prefix}.severity")
        rules.append(
            CompareColumnsRule(left=left, operator=operator, right=right, severity=severity)
        )
    return tuple(rules)


def _parse_quality(value: object) -> QualityPolicy | None:
    """Convert quality mapping to QualityPolicy (None if absent, #446/#486).

    max_duplicate_rate/min_rows are scalars, max_null_ratio is {column: ratio}
    mapping — as-is from existing #446 syntax. ``*_severity`` fields (#486) are
    optional, default "warn". ``range``/``compare_columns`` are typed extension
    rules added in #486.
    """
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="quality")
    max_dup = mapping.get("max_duplicate_rate")
    if max_dup is not None and (not isinstance(max_dup, (int, float)) or isinstance(max_dup, bool)):
        raise TypeError("quality.max_duplicate_rate must be a number")
    max_dup_severity = _parse_severity(
        mapping.get("max_duplicate_rate_severity", "warn"),
        field_name="quality.max_duplicate_rate_severity",
    )
    min_rows = mapping.get("min_rows")
    if min_rows is not None and (not isinstance(min_rows, int) or isinstance(min_rows, bool)):
        raise TypeError("quality.min_rows must be an integer")
    min_rows_severity = _parse_severity(
        mapping.get("min_rows_severity", "warn"), field_name="quality.min_rows_severity"
    )
    null_ratio_raw = mapping.get("max_null_ratio", {})
    if not isinstance(null_ratio_raw, dict):
        raise TypeError("quality.max_null_ratio must be a mapping")
    max_null_ratio: dict[str, float] = {}
    for k, v in cast(dict[object, object], null_ratio_raw).items():
        if not isinstance(k, str) or not isinstance(v, (int, float)) or isinstance(v, bool):
            raise TypeError("quality.max_null_ratio entries must be string→number pairs")
        max_null_ratio[k] = float(v)
    max_null_ratio_severity = _parse_severity_map(
        mapping.get("max_null_ratio_severity", {}), field_name="quality.max_null_ratio_severity"
    )
    range_rules = _parse_range_rules(mapping.get("range", []))
    compare_columns_rules = _parse_compare_columns_rules(mapping.get("compare_columns", []))
    return QualityPolicy(
        max_duplicate_rate=float(max_dup) if max_dup is not None else None,
        max_duplicate_rate_severity=max_dup_severity,
        max_null_ratio=max_null_ratio,
        max_null_ratio_severity=max_null_ratio_severity,
        min_rows=min_rows,
        min_rows_severity=min_rows_severity,
        range=range_rules,
        compare_columns=compare_columns_rules,
    )


def _parse_join_keys(mapping: dict[str, object]) -> tuple[tuple[str, str], ...]:
    """Read the join key from either ``keys`` or the ``left_key``/``right_key`` shorthand.

    Exactly one form must be given (#698). Giving both would leave it unclear which
    one the author meant, so it is refused rather than merged.
    """
    has_keys = "keys" in mapping
    has_shorthand = "left_key" in mapping or "right_key" in mapping
    if has_keys and has_shorthand:
        raise ValueError("composition.join takes either keys or left_key/right_key, not both")
    if not has_keys and not has_shorthand:
        raise ValueError(
            "composition.join requires keys (a list of {left, right} pairs) or left_key/right_key"
        )
    if has_shorthand:
        left_key = _require_string(mapping, "left_key", prefix="composition.join")
        right_key = _require_string(mapping, "right_key", prefix="composition.join")
        return ((left_key, right_key),)

    raw_keys = mapping["keys"]
    if not isinstance(raw_keys, list) or not raw_keys:
        raise ValueError("composition.join.keys must be a non-empty list of {left, right} pairs")
    pairs: list[tuple[str, str]] = []
    for index, raw_pair in enumerate(cast(list[object], raw_keys)):
        prefix = f"composition.join.keys[{index}]"
        pair = _ensure_mapping(raw_pair, field_name=prefix)
        unknown = sorted(set(pair) - {"left", "right"})
        if unknown:
            raise ValueError(f"{prefix} has unknown fields: {unknown}")
        pairs.append(
            (
                _require_string(pair, "left", prefix=prefix),
                _require_string(pair, "right", prefix=prefix),
            )
        )
    return tuple(pairs)


def _parse_join(value: object) -> JoinSpec:
    """Convert composition.join mapping to JoinSpec (#506, #698).

    left/right are required strings. The join key is either ``keys`` (a list of
    ``{left, right}`` column pairs) or the single-pair ``left_key``/``right_key``
    shorthand — exactly one of the two. type/on_duplicate_key/on_null_key/
    cardinality are fixed-vocabulary fields validated here immediately like
    pii.mode — whether left/right match actual sources[].alias and whether join
    keys actually exist/are compatible is semantic validation (not structural),
    handled by validator/orchestrator respectively.
    """
    mapping = _ensure_mapping(value, field_name="composition.join")
    left = _require_string(mapping, "left", prefix="composition.join")
    right = _require_string(mapping, "right", prefix="composition.join")
    keys = _parse_join_keys(mapping)

    join_type = mapping.get("type", "inner")
    if not isinstance(join_type, str) or join_type not in _JOIN_TYPES:
        raise ValueError(f"composition.join.type must be one of {_JOIN_TYPES}, got {join_type!r}")

    on_duplicate_key = mapping.get("on_duplicate_key", "warn")
    if (
        not isinstance(on_duplicate_key, str)
        or on_duplicate_key not in _JOIN_DUPLICATE_KEY_SEVERITIES
    ):
        raise ValueError(
            "composition.join.on_duplicate_key must be one of "
            f"{_JOIN_DUPLICATE_KEY_SEVERITIES}, got {on_duplicate_key!r}"
        )

    on_null_key = mapping.get("on_null_key", "warn")
    if not isinstance(on_null_key, str) or on_null_key not in _JOIN_NULL_KEY_SEVERITIES:
        raise ValueError(
            "composition.join.on_null_key must be one of "
            f"{_JOIN_NULL_KEY_SEVERITIES}, got {on_null_key!r}"
        )

    cardinality = mapping.get("cardinality")
    if cardinality is not None and (
        not isinstance(cardinality, str) or cardinality not in _JOIN_CARDINALITIES
    ):
        raise ValueError(
            "composition.join.cardinality must be one of "
            f"{_JOIN_CARDINALITIES}, got {cardinality!r}"
        )

    return JoinSpec(
        left=left,
        right=right,
        type=join_type,
        on_duplicate_key=on_duplicate_key,
        keys=keys,
        cardinality=cardinality,
        on_null_key=on_null_key,
    )


def _parse_composition(value: object) -> CompositionSpec | None:
    """Convert composition mapping to CompositionSpec (None if absent, #506)."""
    if value is None:
        return None
    mapping = _ensure_mapping(value, field_name="composition")
    name = _require_string(mapping, "name", prefix="composition")
    join = _parse_join(_require_present(mapping, "join"))
    return CompositionSpec(name=name, join=join)


__all__ = [
    "load_spec",
    "parse_spec",
]

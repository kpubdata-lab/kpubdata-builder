"""Canonical BuildSpec serialization and snapshot saving for per-run reproducibility audit."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from pathlib import Path
from typing import cast

import yaml

from ..stages._path_safety import ensure_within, validate_path_segment
from .models import BuildSpec, JsonValue, SourceRef

BUILDSPEC_SNAPSHOT_FILENAME = "buildspec.yaml"
REDACTED_VALUE = "<redacted>"

# BuildSpec has no separate credential model — only free-form JSON mapping.
# Therefore, redact only explicit keys actually used in credential values,
# not substring guessing.
_SECRET_FIELD_NAMES = frozenset(
    {
        "access_token",
        "accesstoken",
        "api_key",
        "apikey",
        "authorization",
        "bearer_token",
        "bearertoken",
        "client_secret",
        "clientsecret",
        "password",
        "refresh_token",
        "refreshtoken",
        "secret",
        "service_key",
        "servicekey",
        "token",
        # Keys actually used as credential environment variables/header names in
        # this repository.
        "hf_token",
        "kaggle_key",
        "kpubdata_builder_api_key",
        "kpubdata_datago_api_key",
        "x_api_key",
    }
)


def _normalized_key(key: str) -> str:
    return key.casefold().replace("-", "_")


def _canonical_json(value: JsonValue) -> JsonValue:
    """Copy free-form JSON value with fixed key order and secret redaction.

    Use only for free-form mappings like ``params``/``auth`` where keys may be
    credential names. For structural mappings where keys are column names, use
    :func:`_canonical_structure`.
    """
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key in sorted(value):
            item = value[key]
            result[key] = (
                REDACTED_VALUE
                if _normalized_key(key) in _SECRET_FIELD_NAMES
                else _canonical_json(item)
            )
        return result
    if isinstance(value, list):
        return [_canonical_json(item) for item in value]
    return value


def _canonical_structure(value: JsonValue) -> JsonValue:
    """Copy structural mapping with fixed key order only — no redaction (#623).

    Fields like ``schema.rename``/``read_as``/``column_null_tokens`` have keys
    that are source column names. Applying credential-key redaction would replace
    columns named ``token``/``api_key``/``password`` with ``"<redacted>"``, causing
    snapshots to hold strings instead of list/dict types expected by loaders,
    preventing re-parsing. Specs declaring different null tokens would also get the
    same digest, breaking recipe identity. Column names are not credentials—copy
    values only, preserving structure.
    """
    if isinstance(value, dict):
        return {key: _canonical_structure(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_structure(item) for item in value]
    return value


def canonical_source_mapping(source: SourceRef) -> dict[str, JsonValue]:
    """One source as it appears in the canonical spec, secrets redacted.

    Split out of :func:`canonical_spec_mapping` so a source can be fingerprinted on its
    own (#700) from exactly the text a run snapshot records — a fingerprint computed
    any other way would not match the one recomputed from a past run's snapshot.
    """
    schema: JsonValue = None
    if source.schema is not None:
        schema = {
            "required": list(source.schema.required),
            "dtypes": _canonical_structure(cast(JsonValue, source.schema.dtypes)),
            "casts": _canonical_structure(cast(JsonValue, source.schema.casts)),
        }
        # Remaining Silver transformation declarations (post-#611) included
        # only when declared.
        #
        # They are part of the recipe too — if omitted, changing transformation
        # rules leaves digest unchanged, breaking R1's "same recipe means same
        # output" claim by leaving the rules that created Silver outside the
        # recipe. So declared values must always be included.
        #
        # However, including keys even when empty changes digests of all
        # existing specs that don't use these fields. Digest is recipe identity
        # exposed in manifest/BuildIndex/GET /datasets, so upgrade-time
        # comparisons of "is this the same recipe?" break. Identity must not
        # change because of unused features.
        optional: dict[str, JsonValue] = {
            "rename": _canonical_structure(cast(JsonValue, source.schema.rename)),
            "read_as": _canonical_structure(cast(JsonValue, source.schema.read_as)),
            "null_tokens": list(source.schema.null_tokens),
            "column_null_tokens": _canonical_structure(
                cast(
                    JsonValue,
                    {
                        column: {
                            "tokens": list(rule.tokens),
                            "on_absent": rule.on_absent,
                        }
                        for column, rule in source.schema.column_null_tokens.items()
                    },
                )
            ),
            "coalesce": _canonical_structure(
                cast(
                    JsonValue,
                    {
                        target: list(candidates)
                        for target, candidates in source.schema.coalesce.items()
                    },
                )
            ),
            "zfill": _canonical_structure(cast(JsonValue, source.schema.zfill)),
            "derived": [
                {
                    "name": rule.name,
                    "kind": rule.kind,
                    "columns": list(rule.columns),
                }
                for rule in source.schema.derived
            ],
        }
        schema.update({key: value for key, value in optional.items() if value})
    # Include only fields valid for each kind (#498). Loader's
    # _reject_foreign_fields rejects on mere presence of kind-foreign fields,
    # so always including all kinds' fields makes canonical snapshot itself
    # round-trip-unsafe — follow schema's (#437) existing pattern of "omit if
    # unrelated". For existing public_api-only specs, only the "kind":
    # "public_api" field grows additively.
    entry: dict[str, JsonValue] = {"kind": source.kind, "alias": source.alias, "schema": schema}
    if source.gold is not None:
        # Part of the recipe (#659): which columns and rows are published. Omitted when
        # absent, so existing specs keep their digest.
        entry["gold"] = {
            "select": list(source.gold.select),
            "filters": [
                {
                    "column": f.column,
                    "op": f.op,
                    **({} if f.op == "not_null" else {"value": f.value}),
                }
                for f in source.gold.filters
            ],
        }
    if source.kind == "file":
        entry["upload_id"] = source.upload_id
        entry["format"] = source.format
        entry["encoding"] = source.encoding
    elif source.kind == "url":
        entry["endpoint"] = source.endpoint
        entry["method"] = source.method
        entry["format"] = source.format
    else:
        entry["provider"] = source.provider
        entry["dataset"] = source.dataset
        entry["params"] = _canonical_json(source.params)
        if source.param_grid:
            # Expanded combinations determine which data was fetched — part of
            # the recipe. If omitted, changing grid leaves digest unchanged,
            # breaking "same recipe means same output" (#613).
            #
            # Omit when empty. Existing specs' digests must not change because
            # of unused features (same reason as #640).
            entry["param_grid"] = _canonical_structure(
                cast(
                    JsonValue,
                    {key: list(values) for key, values in source.param_grid.items()},
                )
            )
    return entry


def canonical_spec_mapping(spec: BuildSpec) -> dict[str, JsonValue]:
    """Convert BuildSpec to JSON-compatible mapping with fixed field order and optional defaults."""
    sources: list[JsonValue] = []
    for source in spec.sources:
        sources.append(canonical_source_mapping(source))

    exports: list[JsonValue] = [
        {
            "kind": target.kind,
            "output_path": target.output_path,
            "options": _canonical_json(target.options),
        }
        for target in spec.exports
    ]

    splits: JsonValue = None
    if spec.splits is not None:
        splits = {
            "mode": spec.splits.mode,
            "ratios": _canonical_structure(cast(JsonValue, spec.splits.ratios)),
            "key": spec.splits.key,
            "seed": spec.splits.seed,
        }

    pii: JsonValue = None
    if spec.pii is not None:
        pii = {"mode": spec.pii.mode, "allow_columns": list(spec.pii.allow_columns)}

    quality: JsonValue = None
    if spec.quality is not None:
        quality = {
            "max_duplicate_rate": spec.quality.max_duplicate_rate,
            "max_duplicate_rate_severity": spec.quality.max_duplicate_rate_severity,
            "max_null_ratio": _canonical_structure(cast(JsonValue, spec.quality.max_null_ratio)),
            "max_null_ratio_severity": _canonical_structure(
                cast(JsonValue, spec.quality.max_null_ratio_severity)
            ),
            "min_rows": spec.quality.min_rows,
            "min_rows_severity": spec.quality.min_rows_severity,
            "range": [
                {"column": r.column, "min": r.min, "max": r.max, "severity": r.severity}
                for r in spec.quality.range
            ],
            "compare_columns": [
                {"left": r.left, "operator": r.operator, "right": r.right, "severity": r.severity}
                for r in spec.quality.compare_columns
            ],
        }

    composition: JsonValue = None
    if spec.composition is not None:
        join = spec.composition.join
        join_mapping: dict[str, JsonValue] = {"left": join.left, "right": join.right}
        # A single-pair key keeps the left_key/right_key shorthand and the #698
        # fields appear only when declared, so a spec written before #698 keeps its
        # spec_digest (same reason as #640).
        if len(join.keys) == 1:
            join_mapping["left_key"] = join.left_key
            join_mapping["right_key"] = join.right_key
        else:
            join_mapping["keys"] = [{"left": lk, "right": rk} for lk, rk in join.keys]
        join_mapping["type"] = join.type
        join_mapping["on_duplicate_key"] = join.on_duplicate_key
        if join.cardinality is not None:
            join_mapping["cardinality"] = join.cardinality
        if join.on_null_key != "warn":
            join_mapping["on_null_key"] = join.on_null_key
        composition = {"name": spec.composition.name, "join": join_mapping}

    mapping: dict[str, JsonValue] = {
        "dataset_id": spec.dataset_id,
        "title": spec.title,
        "description": spec.description,
        "sources": sources,
        "exports": exports,
        "metadata": _canonical_json(spec.metadata),
        "publish": spec.publish,
        "splits": splits,
        "pii": pii,
        "license": spec.license,
        "quality": quality,
        "composition": composition,
    }
    # Include only when declared. Always including changes spec_digest of all
    # existing specs not using attribution — recipe identity diverges
    # unmotivatedly (same reason as #640).
    if spec.attribution is not None:
        mapping["attribution"] = spec.attribution
    # Same rule for the licence's name and link (#764): a spec that does not use
    # `license: other` keeps its digest.
    if spec.license_name is not None:
        mapping["license_name"] = spec.license_name
    if spec.license_link is not None:
        mapping["license_link"] = spec.license_link
    # Omitted when absent (#781), so specs without a cadence keep their digest.
    if spec.refresh_cadence is not None:
        mapping["refresh_cadence"] = spec.refresh_cadence
    return mapping


def serialize_spec(spec: BuildSpec) -> str:
    """Serialize BuildSpec to deterministic canonical UTF-8 YAML string."""
    return yaml.safe_dump(
        canonical_spec_mapping(spec),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
        width=4096,
    )


def serialize_spec_bytes(spec: BuildSpec) -> bytes:
    """Return canonical bytes to record in actual snapshot."""
    return serialize_spec(spec).encode("utf-8")


def compute_spec_digest(payload: bytes) -> str:
    """Return SHA-256 digest of canonical snapshot bytes."""
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def write_buildspec_snapshot(
    spec: BuildSpec, *, output_root: Path, run_id: str
) -> tuple[Path, str]:
    """Atomically record canonical BuildSpec to run workspace and return digest."""
    validate_path_segment(run_id, field_name="run_id")
    run_dir = output_root / run_id
    ensure_within(output_root, run_dir, label="run directory")
    snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
    ensure_within(run_dir, snapshot_path, label="BuildSpec snapshot")
    payload = serialize_spec_bytes(spec)

    run_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=run_dir, prefix=".buildspec_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, snapshot_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    return snapshot_path, compute_spec_digest(payload)


__all__ = [
    "BUILDSPEC_SNAPSHOT_FILENAME",
    "REDACTED_VALUE",
    "canonical_spec_mapping",
    "compute_spec_digest",
    "serialize_spec",
    "serialize_spec_bytes",
    "write_buildspec_snapshot",
]

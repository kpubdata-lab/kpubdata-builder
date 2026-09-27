"""logic to split records into named partitions (#38).

splits record sequence by SplitSpec into ratio splits (train/val/test) or column-value
splits (year/region/category). ratio splits are deterministic based on seed; reproducible
regardless of record order.

main functions:
    - apply_splits: records + SplitSpec -> {split name: record tuple}
"""

from __future__ import annotations

import random
from collections.abc import Sequence

import polars as pl

from ...spec import JsonValue, SplitSpec

Record = dict[str, JsonValue]


def _allocate_counts(total: int, ratios: dict[str, float], names: list[str]) -> dict[str, int]:
    """distributes ratios as integer counts (sum = total); remainder distributed by
    largest fractional part."""
    ratio_sum = sum(ratios.values())
    exact = {name: total * ratios[name] / ratio_sum for name in names}
    counts = {name: int(exact[name]) for name in names}
    remainder = total - sum(counts.values())
    # distributes remainder by largest fractional part (ties broken by name order) for determinism.
    by_fraction = sorted(
        names,
        key=lambda name: (-(exact[name] - counts[name]), name),
    )
    for name in by_fraction[:remainder]:
        counts[name] += 1
    return counts


def _ratio_split(
    records: Sequence[Record], ratios: dict[str, float], seed: int
) -> dict[str, tuple[Record, ...]]:
    """deterministically splits records by ratio."""
    names = sorted(ratios)
    counts = _allocate_counts(len(records), ratios, names)
    order = list(range(len(records)))
    random.Random(seed).shuffle(order)

    result: dict[str, tuple[Record, ...]] = {}
    position = 0
    for name in names:
        chosen = order[position : position + counts[name]]
        position += counts[name]
        # preserves original order for stable results.
        result[name] = tuple(records[index] for index in sorted(chosen))
    return result


# uses single non-string object as sentinel to avoid collision with actual record values.
# object() is str()-able, but identical object() instances are distinguished by(identity)only
# distinguished. using this unique identity as internal bucket key, "__missing__" or
# "__null__" literal string values don't collide into wrong bucket
# prevents (#225).
_MISSING_KEY_SENTINEL: object = object()
_NULL_VALUE_SENTINEL: object = object()

_SENTINEL_NAMES: dict[object, str] = {
    _MISSING_KEY_SENTINEL: "__missing__",
    _NULL_VALUE_SENTINEL: "__null__",
}


def _key_split(records: Sequence[Record], key: str) -> dict[str, tuple[Record, ...]]:
    """splits records into groups by column values (value -> partition name).

    records without key go to "__missing__" bucket, None values to "__null__" bucket,
    empty strings to "" bucket.

    uses sentinel objects for internal bucket keys so records without/with None key
    and records with literal "__missing__"/"__null__" strings do not merge during
    collection. output dict converts sentinels to string names on merge (extends on
    name collision #225).
    """
    grouped: dict[object, list[Record]] = {}
    for record in records:
        if key not in record:
            bucket: object = _MISSING_KEY_SENTINEL
        elif record[key] is None:
            bucket = _NULL_VALUE_SENTINEL
        else:
            bucket = str(record[key])
        grouped.setdefault(bucket, []).append(record)
    # converts sentinel to output name; merges on name collision to prevent record loss.
    result: dict[str, list[Record]] = {}
    for k, rows in grouped.items():
        name: str = k if isinstance(k, str) else _SENTINEL_NAMES[k]
        result.setdefault(name, []).extend(rows)
    return {name: tuple(rows) for name, rows in result.items()}


def apply_splits(records: Sequence[Record], spec: SplitSpec) -> dict[str, tuple[Record, ...]]:
    """splits records into named partitions by SplitSpec.

    arguments:
        records: sequence of records to split.
        spec: split definition.

    returns:
        dict[str, tuple[Record, ...]]: split name -> record tuple.

    raises:
        ValueError: if unsupported split mode.
    """
    if spec.mode == "ratio":
        return _ratio_split(records, spec.ratios, spec.seed)
    if spec.mode == "key":
        return _key_split(records, spec.key)
    raise ValueError(f"Unsupported split mode: {spec.mode!r}")


def _ratio_split_frame(
    frame: pl.DataFrame, ratios: dict[str, float], seed: int
) -> dict[str, pl.DataFrame]:
    """deterministically splits DataFrame by ratio."""
    names = sorted(ratios)
    counts = _allocate_counts(frame.height, ratios, names)
    order = list(range(frame.height))
    random.Random(seed).shuffle(order)

    result: dict[str, pl.DataFrame] = {}
    position = 0
    for name in names:
        chosen = order[position : position + counts[name]]
        position += counts[name]
        # preserves original order for stable results.
        result[name] = frame[sorted(chosen)]
    return result


def _key_split_frame(frame: pl.DataFrame, key: str) -> dict[str, pl.DataFrame]:
    """splits DataFrame into groups by column values.

    records without key go to "__missing__" bucket, None values to "__null__" bucket.
    uses sentinel objects for internal bucket keys so literal "__missing__"/"__null__"
    string values do not collide. output dict converts sentinels to string names on
    merge (extends on name collision #225).
    """
    # if key missing, return entire as __missing__ bucket
    if key not in frame.columns:
        return {"__missing__": frame}

    # replace None with "__null__" string for partition handling
    # use fill_null to convert None to explicit string
    key_col = frame[key]
    # convert to string then replace None with __null__
    key_str = key_col.cast(pl.Utf8, strict=False).fill_null("__null__")
    frame_with_key = frame.with_columns(key_str.alias("__split_key__"))

    # partition groups by
    partitions = frame_with_key.partition_by("__split_key__", maintain_order=True)

    # construct result dict: remove __split_key__ column and merge duplicate names
    grouped: dict[str, list[pl.DataFrame]] = {}
    for partition in partitions:
        split_name = partition["__split_key__"][0]  # use key value of first row as partition name
        partition_clean = partition.drop("__split_key__")
        grouped.setdefault(split_name, []).append(partition_clean)

    # merge partitions with same name (sentinel collision handling)
    return {name: pl.concat(dfs) for name, dfs in grouped.items()}


def apply_splits_to_frame(frame: pl.DataFrame, spec: SplitSpec) -> dict[str, pl.DataFrame]:
    """splits DataFrame into named partitions by SplitSpec (Polars native).

    arguments:
        frame: DataFrame to split.
        spec: split definition.

    returns:
        dict[str, pl.DataFrame]: split name -> DataFrame.

    raises:
        ValueError: if unsupported split mode.
    """
    if spec.mode == "ratio":
        return _ratio_split_frame(frame, spec.ratios, spec.seed)
    if spec.mode == "key":
        return _key_split_frame(frame, spec.key)
    raise ValueError(f"Unsupported split mode: {spec.mode!r}")


__all__ = ["apply_splits", "apply_splits_to_frame"]

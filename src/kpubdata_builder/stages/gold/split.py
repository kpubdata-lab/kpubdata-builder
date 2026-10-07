"""logic to split records into named partitions (#38).

splits a Gold table by SplitSpec into ratio splits (train/val/test) or column-value
splits (year/region/category).

**Ratio splits — ``hash-sort-v2`` (#871).** Each row gets the sort key
``md5("<seed>:<row ordinal>")`` (the ordinal is ``_kpubdata_row_seq``, ADR 0021 D8);
rows are ordered by that key, then by the ordinal, and the order is cut into exact
counts — the same counts as before (:func:`_allocate_counts`), so ``N`` rows always give
exactly the train/val/test sizes the ratios ask for. DuckDB sorts, spilling to disk if
it must; no Python list of ``N`` indices exists any more. The same seed gives the same
membership, a different seed a different one, and duplicate rows are told apart by
their ordinals. Each split keeps the table's row order.

Membership differs from the earlier ``shuffle-v1`` (``random.Random(seed).shuffle``):
the manifest records which algorithm made a run's splits (``split_algorithm``), and a
manifest without it predates this and used ``shuffle-v1`` (:func:`split_algorithm_of`).

**Key splits** group by the key column in DuckDB. A partition is named after the key's
value as text — formatted exactly as before (:func:`key_text`), for the distinct values
only —
and a row without a value goes to ``"__null__"``; a table without the key column is all
``"__missing__"``. A value that is literally ``"__null__"`` shares that partition with
the nulls, as it always did (#225).

main functions:
    - apply_splits: records + SplitSpec -> {split name: record tuple}
    - apply_splits_to_table: Gold table + SplitSpec -> {split name: table}
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast

from ...spec import JsonValue, SplitSpec
from ...tabular.duckdb_load import TableHandle
from ...tabular.duckdb_runtime import ROW_SEQ_COLUMN, TabularRelation
from ...tabular.sql import quote_identifier

Record = dict[str, JsonValue]

#: The ratio split algorithm runs use now.
SPLIT_ALGORITHM = "hash-sort-v2"
#: What a manifest without ``split_algorithm`` used.
LEGACY_SPLIT_ALGORITHM = "shuffle-v1"

_MISSING = "__missing__"
_NULL = "__null__"

_names = itertools.count()


def split_algorithm_of(manifest: Mapping[str, object]) -> str:
    """The algorithm a manifest's ratio splits were made with (#871).

    A manifest without ``split_algorithm`` predates it and used ``shuffle-v1``.
    """
    value = manifest.get("split_algorithm")
    return value if isinstance(value, str) and value else LEGACY_SPLIT_ALGORITHM


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


def _ranges(total: int, ratios: dict[str, float]) -> dict[str, tuple[int, int]]:
    """Each split's ``[start, end)`` positions in the hash order, names sorted."""
    names = sorted(ratios)
    counts = _allocate_counts(total, ratios, names)
    out: dict[str, tuple[int, int]] = {}
    position = 0
    for name in names:
        out[name] = (position, position + counts[name])
        position += counts[name]
    return out


def _sort_key(seed: int, ordinal: int) -> str:
    """``hash-sort-v2``'s key for one row — the same text DuckDB hashes in SQL."""
    return hashlib.md5(f"{seed}:{ordinal}".encode()).hexdigest()


def _ratio_split(
    records: Sequence[Record], ratios: dict[str, float], seed: int
) -> dict[str, tuple[Record, ...]]:
    """deterministically splits records by ratio (``hash-sort-v2``)."""
    order = sorted(range(len(records)), key=lambda i: (_sort_key(seed, i), i))
    result: dict[str, tuple[Record, ...]] = {}
    for name, (start, end) in _ranges(len(records), ratios).items():
        # preserves original order for stable results.
        result[name] = tuple(records[index] for index in sorted(order[start:end]))
    return result


def _key_split(records: Sequence[Record], key: str) -> dict[str, tuple[Record, ...]]:
    """splits records into groups by column values (value -> partition name).

    records without key go to "__missing__", None values to "__null__", empty strings
    to "". A literal "__missing__"/"__null__" value shares that partition (#225).
    """
    result: dict[str, list[Record]] = {}
    for record in records:
        if key not in record:
            name = _MISSING
        elif record[key] is None:
            name = _NULL
        else:
            name = str(record[key])
        result.setdefault(name, []).append(record)
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


def _with_column(
    table: TableHandle, expression: str, params: Sequence[object], *, source: str = ""
) -> TableHandle:
    """``table`` with one more column, ``expression`` (an integer), last.

    ``source`` replaces the table in ``FROM`` when the expression needs a subquery.
    """
    loaded = table.table
    seq = quote_identifier(ROW_SEQ_COLUMN)
    columns = [
        f"{quote_identifier(p)} AS {quote_identifier(f'c{i}')}"
        for i, p in enumerate(loaded.physical)
    ]
    columns.append(f"{expression} AS {quote_identifier(f'c{len(loaded.physical)}')}")
    return table.derive(
        f"SELECT {seq}, {', '.join(columns)} FROM {source or loaded.relation.sql}",
        params,
        into=TabularRelation(f"{loaded.relation.name}_keyed_{next(_names)}"),
        columns=[*zip(loaded.names, loaded.nodes, strict=True), ("_split", ("int",))],
    )


def _subset(keyed: TableHandle, where: str) -> TableHandle:
    """The rows of ``keyed`` whose last column matches ``where`` (``{}`` stands for it),
    without that column, in the table's order, as a new table."""
    loaded = keyed.table
    seq = quote_identifier(ROW_SEQ_COLUMN)
    kept = loaded.physical[:-1]
    columns = ", ".join(
        f"{quote_identifier(p)} AS {quote_identifier(f'c{i}')}" for i, p in enumerate(kept)
    )
    return keyed.derive(
        f"SELECT row_number() OVER (ORDER BY {seq}) - 1 AS {seq}"
        f"{', ' + columns if columns else ''} FROM {loaded.relation.sql} "
        f"WHERE {where.format(quote_identifier(loaded.physical[-1]))}",
        into=TabularRelation(f"{loaded.relation.name}_split_{next(_names)}"),
        columns=list(zip(loaded.names[:-1], loaded.nodes[:-1], strict=True)),
    )


def _ratio_split_table(
    table: TableHandle, ratios: dict[str, float], seed: int
) -> dict[str, TableHandle]:
    seq = quote_identifier(ROW_SEQ_COLUMN)
    # Each row's position in hash order; DuckDB sorts out of core if it must.
    keyed = _with_column(
        table,
        f"row_number() OVER (ORDER BY md5(CAST(? AS VARCHAR) || ':' || "
        f"CAST({seq} AS VARCHAR)), {seq}) - 1",
        [seed],
    )
    return {
        name: _subset(keyed, f"{{0}} >= {start} AND {{0}} < {end}")
        for name, (start, end) in _ranges(table.height, ratios).items()
    }


def _float_text(value: float) -> str:
    """A float as the splits have always named it: the shortest round-trip digits, in
    exponent form from 1e16 up and below 1e-5, the exponent unpadded (``1.5e-7``)."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    shortest = Decimal(repr(value))
    sign, digits, exponent = shortest.as_tuple()
    assert isinstance(exponent, int)
    magnitude = len(digits) + exponent - 1
    if (magnitude >= 16 or magnitude < -5) and value != 0:
        text = "".join(str(d) for d in digits).rstrip("0") or "0"
        mantissa = text[0] + (f".{text[1:]}" if len(text) > 1 else "")
        return f"{'-' if sign else ''}{mantissa}e{'+' if magnitude >= 0 else '-'}{abs(magnitude)}"
    fixed = format(shortest, "f")
    return fixed if "." in fixed else f"{fixed}.0"


def _offset_text(offset: dt.timedelta) -> str:
    """``+09:00``: the offset to the nearest minute, as Polars printed it. A historic zone
    offset with seconds (Seoul's +08:27:52 in 1900) was ``+08:2752`` with ``%z`` (#876
    review)."""
    minutes = round(offset.total_seconds() / 60)
    sign = "-" if minutes < 0 else "+"
    hours, rest = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{rest:02d}"


def key_text(value: object, node: tuple[object, ...]) -> str | None:
    """A key value as the name of its partition — the text the splits have always used
    (Polars' cast to text before #876), or None for a null.

    Raises:
        ValueError: The key column is not a scalar a partition can be named after.
    """
    kind = node[0]
    if kind in ("list", "struct", "binary", "duration"):
        raise ValueError(f"a split key must be a scalar column, not a {kind} column")
    if value is None:
        return None
    if kind == "bool":
        return "true" if value else "false"
    if kind == "float":
        return _float_text(float(cast(float, value)))
    if kind == "decimal":
        scale = int(cast(int, node[1]))
        return format(cast(Decimal, value).quantize(Decimal(1).scaleb(-scale)), "f")
    if kind == "datetime":
        moment = cast(dt.datetime, value)
        text = moment.strftime("%Y-%m-%d %H:%M:%S.%f")
        if moment.tzinfo is None:
            return text
        return f"{text}{_offset_text(cast(dt.timedelta, moment.utcoffset()))}"
    if kind == "time":
        return cast(dt.time, value).strftime("%H:%M:%S")
    if kind == "date":
        return cast(dt.date, value).isoformat()
    return str(value)


def _key_names(table: TableHandle, column: str, groups: list[tuple[int, object]]) -> dict[int, str]:
    """Each group's partition name: its key value as text, as the splits always named
    it (:func:`key_text`), read for the distinct values only."""
    node = table.table.nodes[table.table.names.index(column)]
    names: dict[int, str] = {}
    for group, value in groups:
        text = key_text(table.decode_row([column], [value])[column], node)
        names[group] = _NULL if text is None else text
    return names


def _key_split_table(table: TableHandle, key: str) -> dict[str, TableHandle]:
    loaded = table.table
    if key not in loaded.names:
        return {_MISSING: table}
    seq = quote_identifier(ROW_SEQ_COLUMN)
    column = loaded.column(key)
    # One group per distinct value (null one of them), numbered by first appearance.
    keyed = _with_column(
        table,
        "dense_rank() OVER (ORDER BY first_seen)",
        [],
        source=(
            f"(SELECT *, min({seq}) OVER (PARTITION BY {column}) AS first_seen "
            f"FROM {loaded.relation.sql})"
        ),
    )
    group = quote_identifier(keyed.table.physical[-1])
    rows = keyed.fetch(
        f"SELECT DISTINCT {group}, {keyed.table.column(key)} FROM {keyed.table.relation.sql} "
        f"ORDER BY {group}"
    )
    groups = [(int(g), value) for g, value in rows]
    named: dict[str, list[int]] = {}
    for g, name in _key_names(table, key, [(g, v) for g, v in groups if v is not None]).items():
        named.setdefault(name, []).append(g)
    null_groups = [g for g, v in groups if v is None]
    if null_groups:
        named.setdefault(_NULL, []).extend(null_groups)
    # Partitions in order of first appearance, as before.
    ordered = sorted(named.items(), key=lambda item: min(item[1]))
    return {
        name: _subset(keyed, f"{{0}} IN ({', '.join(str(g) for g in sorted(ids))})")
        for name, ids in ordered
    }


def apply_splits_to_table(table: TableHandle, spec: SplitSpec) -> dict[str, TableHandle]:
    """splits a Gold table into named partitions by SplitSpec, in DuckDB (#871).

    Each split is a new table in ``table``'s connection.

    raises:
        ValueError: if unsupported split mode.
    """
    if spec.mode == "ratio":
        return _ratio_split_table(table, spec.ratios, spec.seed)
    if spec.mode == "key":
        return _key_split_table(table, spec.key)
    raise ValueError(f"Unsupported split mode: {spec.mode!r}")


__all__ = [
    "LEGACY_SPLIT_ALGORITHM",
    "SPLIT_ALGORITHM",
    "apply_splits",
    "apply_splits_to_table",
    "split_algorithm_of",
]

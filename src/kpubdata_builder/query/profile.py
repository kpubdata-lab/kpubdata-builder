"""Bounded column profiles of one table snapshot (#817).

A table screen wants to say what each column holds — its type, how much of it is
missing, the range of its values — without the client reading the rows. This is the
first, bounded step of that:

- **Exact, over every row.** Row count, null count and ratio, NaN and infinite counts
  for float columns, and min/max for numeric and temporal columns. Nothing is sampled,
  so ``scope`` says ``full`` and ``accuracy`` says ``exact``. Quantiles, histograms,
  distinct counts and top values are left out until their cost and their disclosure
  risk have been weighed — a top value or a distinct count of a name column *is* data.
- **NaN and infinity are not values of a range.** They are excluded from min/max and
  counted, so ``range.excluded_count`` says how many were left out.
- **Small groups are not described.** A min or max over fewer than
  ``MIN_RANGE_VALUES`` values can point at one record, so the range is withheld.
- **Suspected personal data is not profiled.** A column whose values match a PII
  pattern, or whose name suggests one, gets its type and nothing else, unless the
  BuildSpec's ``pii`` policy accepts it (``mode: allow``, or the column in
  ``allow_columns``). No policy is not acceptance.
- **Which values are pattern-checked (#897).** ``String``, ``Categorical`` and
  ``Enum`` columns (the last two read as their text), and ``List``/``Array`` columns of
  those, where a row matches when any element does. Columns that cannot hold text —
  numeric, boolean, temporal, decimal, duration, null — have no values to check.
  Every other type that can hold text — ``Struct``, ``Object``, ``Binary``, lists of
  lists or of structs, ``Unknown`` — is not pattern-checked, so it is treated as
  suspected with the kind ``unchecked_values`` rather than reported ``not_detected``:
  a value the profile did not look at is not evidence of absence. The BuildSpec's
  ``pii`` policy accepts such a column like any other.
- **Same limits as a query.** The worker runs through ``QueryEngine`` — child process,
  timeout, memory cap — and takes a slot from the same concurrency limit.

The result is tied to the snapshot id, the snapshot's content digest and
``PROFILE_ALGORITHM_VERSION``; a profile computed for other bytes or by another
algorithm is never reused.
"""

from __future__ import annotations

import json
import time
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import cast

import polars as pl
from polars.datatypes import DataTypeClass

from ..spec import JsonValue
from ..tabular.wire import JS_SAFE_INTEGER, encode_value, logical_type

#: Raised whenever what is computed, or how, changes; cached profiles of another
#: version are recomputed.
PROFILE_ALGORITHM_VERSION = 2
#: Fewer finite values than this and a column's min/max is withheld.
MIN_RANGE_VALUES = 10
#: The sensitivity kind of a column that can hold text the value patterns did not read.
UNCHECKED_VALUES_KIND = "unchecked_values"


@dataclass(frozen=True)
class ProfilePlan:
    """What the worker needs besides the table: the source's PII acceptance."""

    allow_all_pii: bool
    allow_columns: tuple[str, ...]

    def to_json(self) -> str:
        return json.dumps(
            {"allow_all_pii": self.allow_all_pii, "allow_columns": list(self.allow_columns)}
        )

    @classmethod
    def from_json(cls, raw: str) -> ProfilePlan:
        data = json.loads(raw)
        return cls(bool(data["allow_all_pii"]), tuple(str(c) for c in data["allow_columns"]))


#: A dtype as a schema gives it, or as a nested type's ``inner`` or field may.
_DType = pl.DataType | DataTypeClass


def _is_text(dtype: _DType) -> bool:
    """A scalar type whose values the patterns read as text."""
    return dtype == pl.String or isinstance(dtype, (pl.Categorical, pl.Enum))


def _text_element(dtype: _DType) -> bool:
    """A ``List`` or ``Array`` whose elements are scalar text."""
    return isinstance(dtype, (pl.List, pl.Array)) and _is_text(dtype.inner)


def _can_hold_text(dtype: _DType) -> bool:
    """Whether some value of ``dtype`` could be, or contain, text."""
    if _is_text(dtype):
        return True
    if isinstance(dtype, (pl.List, pl.Array)):
        return _can_hold_text(dtype.inner)
    if isinstance(dtype, pl.Struct):
        return any(_can_hold_text(field.dtype) for field in dtype.fields)
    return not (
        dtype.is_numeric() or dtype.is_temporal() or dtype == pl.Boolean or dtype == pl.Null
    )


def _pattern_hits(col: pl.Expr, dtype: pl.DataType, pattern: str) -> pl.Expr | None:
    """Rows of ``col`` whose text matches ``pattern``; None when ``dtype`` is not checked."""
    if _is_text(dtype):
        return col.cast(pl.String).str.contains(pattern).sum()
    if _text_element(dtype):
        as_list = col.cast(pl.List(pl.String))
        return as_list.list.eval(pl.element().str.contains(pattern)).list.any().sum()
    return None


def _has_range(dtype: pl.DataType) -> bool:
    return dtype.is_numeric() or dtype.is_temporal()


def _range_encoding(dtype: pl.DataType, low: object, high: object) -> str:
    if dtype.is_decimal():
        return "decimal_string"
    if dtype.is_integer():
        beyond = any(isinstance(v, int) and abs(v) > JS_SAFE_INTEGER for v in (low, high))
        return "decimal_string" if beyond else "number"
    if dtype.is_float():
        return "number"
    return "string"


def _time_zone(dtype: pl.DataType) -> JsonValue:
    """A datetime column's time zone; None when naive or not a datetime."""
    zone = getattr(dtype, "time_zone", None)
    return zone if isinstance(zone, str) else None


def profile_table(table_path: str, plan: ProfilePlan) -> dict[str, JsonValue]:
    """Compute the profile body in one lazy pass over the table."""
    from ..stages.silver.pii import VALUE_PATTERNS, suspect_column_kind

    frame = pl.scan_parquet(table_path)
    schema = frame.collect_schema()
    exprs: list[pl.Expr] = [pl.len().alias("__rows")]
    for index, (name, dtype) in enumerate(schema.items()):
        col = pl.col(name)
        exprs.append(col.null_count().alias(f"n{index}"))
        if dtype.is_float():
            exprs.append(col.is_nan().sum().alias(f"nan{index}"))
            exprs.append(col.is_infinite().sum().alias(f"inf{index}"))
            finite = col.filter(col.is_finite())
        else:
            finite = col.drop_nulls()
        if _has_range(dtype):
            exprs.append(finite.min().alias(f"min{index}"))
            exprs.append(finite.max().alias(f"max{index}"))
            exprs.append(finite.count().alias(f"cnt{index}"))
        for kind, pattern in VALUE_PATTERNS.items():
            hits = _pattern_hits(col, dtype, pattern.pattern)
            if hits is not None:
                exprs.append(hits.alias(f"pii{index}_{kind}"))
    stats = frame.select(exprs).collect().row(0, named=True)

    row_count = int(stats["__rows"])
    allowed = set(plan.allow_columns)
    columns: list[JsonValue] = []
    for index, (name, dtype) in enumerate(schema.items()):
        kinds = [kind for kind in VALUE_PATTERNS if int(stats.get(f"pii{index}_{kind}") or 0) > 0]
        checked = _is_text(dtype) or _text_element(dtype)
        if not checked and _can_hold_text(dtype):
            kinds.append(UNCHECKED_VALUES_KIND)
        name_kind = suspect_column_kind(name)
        if name_kind is not None and name_kind not in kinds:
            kinds.append(name_kind)
        if not kinds:
            sensitivity = "not_detected"
        elif plan.allow_all_pii or name in allowed:
            sensitivity = "allowed_by_spec"
        else:
            sensitivity = "suspected"
        column: dict[str, JsonValue] = {
            "name": name,
            "storage_type": str(dtype),
            "logical_type": logical_type(dtype),
            "time_zone": _time_zone(dtype),
            "sensitivity": {"status": sensitivity, "kinds": cast(JsonValue, kinds)},
        }
        if sensitivity == "suspected":
            column.update(
                status="withheld",
                null_count=None,
                null_ratio=None,
                nan_count=None,
                infinite_count=None,
                range=None,
            )
            columns.append(column)
            continue
        nulls = int(stats[f"n{index}"])
        nan = int(stats[f"nan{index}"]) if dtype.is_float() else None
        infinite = int(stats[f"inf{index}"]) if dtype.is_float() else None
        column.update(
            status="profiled",
            null_count=nulls,
            null_ratio=None if row_count == 0 else nulls / row_count,
            nan_count=nan,
            infinite_count=infinite,
            range=_range_body(dtype, stats, index, nan, infinite),
        )
        columns.append(column)
    return {
        "algorithm_version": PROFILE_ALGORITHM_VERSION,
        "scope": {"mode": "full", "sampled": False, "sample_size": None},
        "accuracy": "exact",
        "min_range_values": MIN_RANGE_VALUES,
        "row_count": row_count,
        "columns": columns,
    }


def _range_body(
    dtype: pl.DataType,
    stats: dict[str, object],
    index: int,
    nan: int | None,
    infinite: int | None,
) -> JsonValue:
    if not _has_range(dtype):
        return {"status": "not_applicable"}
    excluded = (nan or 0) + (infinite or 0)
    count = int(cast(int, stats[f"cnt{index}"]))
    if count == 0:
        return {"status": "no_values", "value_count": 0, "excluded_count": excluded}
    if count < MIN_RANGE_VALUES:
        return {"status": "withheld_small_group", "value_count": count, "excluded_count": excluded}
    low, high = stats[f"min{index}"], stats[f"max{index}"]
    encoding = _range_encoding(dtype, low, high)
    return {
        "status": "exact",
        "min": encode_value(low, encoding),
        "max": encode_value(high, encoding),
        "wire_encoding": encoding,
        "value_count": count,
        "excluded_count": excluded,
    }


def profile_worker(
    connection: Connection,
    table_path: str,
    plan_json: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    """``QueryEngine`` worker for a profile. Sends the body in ``meta``."""
    del limit
    try:
        startup_ms = max(0, (time.monotonic_ns() - parent_started_ns) // 1_000_000)
        engine_started_ns = time.monotonic_ns()
        body = profile_table(table_path, ProfilePlan.from_json(plan_json))
        connection.send(
            {
                "ok": True,
                "columns": [],
                "column_meta": [],
                "rows": [],
                "truncated": False,
                "startup_ms": startup_ms,
                "engine_execution_ms": (time.monotonic_ns() - engine_started_ns) // 1_000_000,
                "meta": {"profile": body},
            }
        )
    except BaseException:
        # Engine messages can contain absolute parquet paths; never send them across.
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send({"ok": False})
    finally:
        connection.close()


__all__ = [
    "MIN_RANGE_VALUES",
    "PROFILE_ALGORITHM_VERSION",
    "UNCHECKED_VALUES_KIND",
    "ProfilePlan",
    "profile_table",
    "profile_worker",
]

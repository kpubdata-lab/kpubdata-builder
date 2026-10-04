"""Bounded column profiles of one table snapshot (#817).

A table screen wants to say what each column holds — its type, how much of it is
missing, the range of its values — without the client reading the rows. This is the
first, bounded step of that:

- **Exact counts, over every row.** Row count, null count and ratio, NaN and infinite
  counts for float columns. Nothing is sampled, so ``scope`` says ``full`` and
  ``accuracy`` says ``exact``. Quantiles, histograms, distinct counts and top values
  are left out until their cost and their disclosure risk have been weighed — a top
  value or a distinct count of a name column *is* data.
- **A range leaves out the extremes (#903).** The highest income or the largest area
  of a column is one record's value. The min/max of a numeric or temporal column are
  therefore reported after removing the ``RANGE_TRIM`` lowest and the ``RANGE_TRIM``
  highest values — rows, not distinct values: ``min`` is the value below which exactly
  ``RANGE_TRIM`` values lie. Such a range says ``trimmed``, never ``exact``. A value
  more than ``RANGE_TRIM`` records share is not an extreme of one record and is still
  reported.
- **NaN and infinity are not values of a range.** They are excluded from min/max and
  counted, so ``range.excluded_count`` says how many were left out.
- **Small groups are not described.** A range over fewer than ``MIN_RANGE_VALUES``
  values can point at one record, and one over ``2 * RANGE_TRIM`` values or fewer has
  nothing left once the extremes are removed; either way the range is withheld.
  ``min_range_values`` in the body is the count below which that happens.
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
  timeout, memory cap — and takes a slot from the same concurrency limit. The counts and
  ranges are one SQL pass in the locked DuckDB connection (#874); a column's type is the
  Builder dtype the file records, else its DuckDB type in Builder's spelling.

The result is tied to the snapshot id, the snapshot's content digest and
``PROFILE_ALGORITHM_VERSION``; a profile computed for other bytes or by another
algorithm is never reused.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, cast

from ..spec import JsonValue
from ..tabular.wire import JS_SAFE_INTEGER, encode_value
from .sandbox import ORDERED_DATASET

#: Raised whenever what is computed, or how, changes; cached profiles of another
#: version are recomputed.
PROFILE_ALGORITHM_VERSION = 3
#: Fewer finite values than this and a column's min/max is withheld.
MIN_RANGE_VALUES = 10
#: How many of the lowest, and how many of the highest, values a range leaves out (#903).
RANGE_TRIM = 5
#: The sensitivity kind of a column that can hold text the value patterns did not read.
UNCHECKED_VALUES_KIND = "unchecked_values"


@dataclass(frozen=True)
class ProfilePlan:
    """What the worker needs besides the table: the source's PII acceptance."""

    allow_all_pii: bool
    allow_columns: tuple[str, ...]
    #: Values removed from each end of a range before its min/max are read (#903).
    range_trim: int = RANGE_TRIM

    def __post_init__(self) -> None:
        if self.range_trim < 0:
            raise ValueError("range_trim must not be negative")

    @property
    def min_range_values(self) -> int:
        """Fewer finite values than this and a range is withheld: the small-group
        floor, or one more than trimming removes, whichever is larger."""
        return max(MIN_RANGE_VALUES, 2 * self.range_trim + 1)

    def to_json(self) -> str:
        return json.dumps(
            {
                "allow_all_pii": self.allow_all_pii,
                "allow_columns": list(self.allow_columns),
                "range_trim": self.range_trim,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> ProfilePlan:
        data = json.loads(raw)
        return cls(
            bool(data["allow_all_pii"]),
            tuple(str(c) for c in data["allow_columns"]),
            int(data.get("range_trim", RANGE_TRIM)),
        )


_NUMERIC = ("Int8", "Int16", "Int32", "Int64", "Int128", "UInt8", "UInt16", "UInt32", "UInt64")
_FLOAT = ("Float32", "Float64")
_TEMPORAL = ("Date", "Datetime", "Time", "Duration")
_TEXT = ("String", "Categorical", "Enum", "Utf8")


def _base(dtype: str) -> str:
    return dtype.split("(", 1)[0]


def _inner(dtype: str) -> str:
    """The element dtype of ``List(x)`` or ``Array(x, …)``."""
    body = dtype[dtype.index("(") + 1 : -1]
    depth = 0
    for index, char in enumerate(body):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return body[:index].strip()
    return body.strip()


def _struct_fields(dtype: str) -> list[str]:
    """The field dtypes of ``Struct({'a': x, 'b': y})``."""
    body = dtype[len("Struct({") : -2]
    fields: list[str] = []
    depth = 0
    quoted: str | None = None
    start = 0
    for index, char in enumerate(body):
        if quoted:
            if char == quoted:
                quoted = None
            continue
        if char in "'\"":
            quoted = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            fields.append(body[start:index])
            start = index + 1
    if body.strip():
        fields.append(body[start:])
    return [field.split(":", 1)[1].strip() for field in fields if ":" in field]


def _is_numeric(dtype: str) -> bool:
    return _base(dtype) in (*_NUMERIC, *_FLOAT, "Decimal")


def _is_text(dtype: str) -> bool:
    """A scalar type whose values the patterns read as text."""
    return _base(dtype) in _TEXT


def _text_element(dtype: str) -> bool:
    """A ``List`` or ``Array`` whose elements are scalar text."""
    return _base(dtype) in ("List", "Array") and _is_text(_inner(dtype))


def _can_hold_text(dtype: str) -> bool:
    """Whether some value of ``dtype`` could be, or contain, text."""
    base = _base(dtype)
    if _is_text(dtype):
        return True
    if base in ("List", "Array"):
        return _can_hold_text(_inner(dtype))
    if base == "Struct":
        return any(_can_hold_text(field) for field in _struct_fields(dtype))
    return not (_is_numeric(dtype) or base in _TEMPORAL or base in ("Boolean", "Null"))


def _has_range(dtype: str) -> bool:
    return _is_numeric(dtype) or _base(dtype) in _TEMPORAL


def _range_encoding(dtype: str, low: object, high: object) -> str:
    base = _base(dtype)
    if base == "Decimal":
        return "decimal_string"
    if base in _NUMERIC:
        beyond = any(isinstance(v, int) and abs(v) > JS_SAFE_INTEGER for v in (low, high))
        return "decimal_string" if beyond else "number"
    if base in _FLOAT:
        return "number"
    return "string"


def _time_zone(dtype: str) -> JsonValue:
    """A datetime column's time zone; None when naive or not a datetime."""
    match = re.search(r"time_zone='([^']*)'", dtype)
    return match.group(1) if _base(dtype) == "Datetime" and match else None


def _pattern_kinds(connection: Any, column: str, element: bool) -> list[str]:
    """The PII kinds some value of ``column`` matches, read over its distinct values.

    The patterns are Python's (Unicode digits and word boundaries), as Silver's scan
    reads them, not DuckDB's RE2. For a list column a row matches when any element does.
    """
    from ..stages.silver.pii import VALUE_PATTERNS

    found: set[str] = set()
    cursor = connection.cursor()
    try:
        cursor.execute(
            f"SELECT DISTINCT {column} FROM {ORDERED_DATASET} WHERE {column} IS NOT NULL"
        )
        while batch := cursor.fetchmany(10_000):
            for (value,) in batch:
                texts = [v for v in value if isinstance(v, str)] if element else [str(value)]
                for kind, pattern in VALUE_PATTERNS.items():
                    if kind not in found and any(pattern.search(t) for t in texts):
                        found.add(kind)
    finally:
        cursor.close()
    return [kind for kind in VALUE_PATTERNS if kind in found]


def profile_table(table_path: str, plan: ProfilePlan) -> dict[str, JsonValue]:
    """Compute the profile body: one SQL pass for the counts and ranges, and a pass over
    each text column's distinct values for the PII patterns (#874)."""
    from ..stages.silver.pii import suspect_column_kind
    from ..tabular.dtypes import logical_type
    from .result import result_dtype
    from .sandbox import open_sandbox

    with open_sandbox(table_path) as sandbox:
        connection = sandbox.connection
        stored = connection.execute(f"DESCRIBE {ORDERED_DATASET}").fetchall()
        dtypes = [
            sandbox.dtypes.get(name) or result_dtype(str(row[1]))
            for name, row in zip(sandbox.columns, stored, strict=False)
        ]
        exprs: list[str] = ["count(*)"]
        slots: dict[str, int] = {}

        def slot(key: str, sql: str) -> None:
            slots[key] = len(exprs)
            exprs.append(sql)

        for index, (name, dtype) in enumerate(zip(sandbox.columns, dtypes, strict=True)):
            col = sandbox.alias(name)
            slot(f"n{index}", f"count(*) - count({col})")
            finite = f"{col} IS NOT NULL"
            if _base(dtype) in _FLOAT:
                slot(f"nan{index}", f"count(*) FILTER (WHERE isnan({col}))")
                slot(f"inf{index}", f"count(*) FILTER (WHERE isinf({col}))")
                finite = f"isfinite({col})"
            if _has_range(dtype):
                value = f"CAST({col} AS TIMESTAMP)" if _time_zone(dtype) is not None else col
                # The value just inside the trimmed ends: the (trim + 1)-th lowest and
                # highest, null when there are not that many (#903).
                nth = plan.range_trim + 1
                slot(f"min{index}", f"min({value}, {nth}) FILTER (WHERE {finite})[{nth}]")
                slot(f"max{index}", f"max({value}, {nth}) FILTER (WHERE {finite})[{nth}]")
                slot(f"cnt{index}", f"count(*) FILTER (WHERE {finite})")
        row = connection.execute(f"SELECT {', '.join(exprs)} FROM {ORDERED_DATASET}").fetchone()
        assert row is not None
        stats: dict[str, object] = {key: row[position] for key, position in slots.items()}
        for index, dtype in enumerate(dtypes):
            if _time_zone(dtype) is None:
                continue
            # A zoned datetime is read as UTC wall time (no pytz) and marked UTC.
            for key in (f"min{index}", f"max{index}"):
                found = stats.get(key)
                if isinstance(found, dt.datetime):
                    stats[key] = found.replace(tzinfo=dt.timezone.utc)

        row_count = int(row[0])
        allowed = set(plan.allow_columns)
        columns: list[JsonValue] = []
        for index, (name, dtype) in enumerate(zip(sandbox.columns, dtypes, strict=True)):
            checked = _is_text(dtype) or _text_element(dtype)
            kinds = (
                _pattern_kinds(connection, sandbox.alias(name), _text_element(dtype))
                if checked
                else []
            )
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
                "storage_type": dtype,
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
            nulls = int(cast(int, stats[f"n{index}"]))
            is_float = _base(dtype) in _FLOAT
            nan = int(cast(int, stats[f"nan{index}"])) if is_float else None
            infinite = int(cast(int, stats[f"inf{index}"])) if is_float else None
            column.update(
                status="profiled",
                null_count=nulls,
                null_ratio=None if row_count == 0 else nulls / row_count,
                nan_count=nan,
                infinite_count=infinite,
                range=_range_body(dtype, stats, index, nan, infinite, plan),
            )
            columns.append(column)
    return {
        "algorithm_version": PROFILE_ALGORITHM_VERSION,
        "scope": {"mode": "full", "sampled": False, "sample_size": None},
        "accuracy": "exact",
        "min_range_values": plan.min_range_values,
        "range_trim": plan.range_trim,
        "row_count": row_count,
        "columns": columns,
    }


def _range_body(
    dtype: str,
    stats: dict[str, object],
    index: int,
    nan: int | None,
    infinite: int | None,
    plan: ProfilePlan,
) -> JsonValue:
    if not _has_range(dtype):
        return {"status": "not_applicable"}
    excluded = (nan or 0) + (infinite or 0)
    count = int(cast(int, stats[f"cnt{index}"]))
    if count == 0:
        return {"status": "no_values", "value_count": 0, "excluded_count": excluded}
    if count < plan.min_range_values:
        return {"status": "withheld_small_group", "value_count": count, "excluded_count": excluded}
    low, high = stats[f"min{index}"], stats[f"max{index}"]
    encoding = _range_encoding(dtype, low, high)
    return {
        "status": "trimmed" if plan.range_trim else "exact",
        "min": encode_value(low, encoding),
        "max": encode_value(high, encoding),
        "wire_encoding": encoding,
        "value_count": count,
        "excluded_count": excluded,
        "trimmed_count": 2 * plan.range_trim,
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

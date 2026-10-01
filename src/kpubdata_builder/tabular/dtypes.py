"""DuckDB column types in Builder's canonical dtype vocabulary (ADR 0021 D3, #866).

Schemas, manifests, previews and the API spell a column's type as ``Int64``, ``String``,
``Datetime(time_unit='us', time_zone=None)``, ``Decimal(precision=10, scale=2)`` — the
strings Polars prints today. After the migration those strings stay; what changes is
their meaning: they are Builder's vocabulary, not an engine's. This module is the
translation from DuckDB's type names, so no DuckDB name reaches a public contract.

The table is closed. A DuckDB type with no Builder spelling raises
:class:`UnsupportedDtype` instead of falling back to some default, so a new type shows
up as an error in a test, not as a quietly renamed column in a user's schema.

``logical_type`` (``tabular/wire.py``) is the canonical dtype's name without
parameters, lower-cased, exactly as it is derived from a Polars dtype today.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

#: DuckDB scalar type → canonical dtype. Aliases DuckDB reports under one name only
#: (``INT8`` is reported as ``BIGINT``) are not listed.
SCALAR_DTYPES: Mapping[str, str] = {
    "BOOLEAN": "Boolean",
    "TINYINT": "Int8",
    "SMALLINT": "Int16",
    "INTEGER": "Int32",
    "BIGINT": "Int64",
    "HUGEINT": "Int128",
    "UTINYINT": "UInt8",
    "USMALLINT": "UInt16",
    "UINTEGER": "UInt32",
    "UBIGINT": "UInt64",
    "FLOAT": "Float32",
    "DOUBLE": "Float64",
    "VARCHAR": "String",
    "BLOB": "Binary",
    "DATE": "Date",
    "TIME": "Time",
    "TIMESTAMP": "Datetime(time_unit='us', time_zone=None)",
    "TIMESTAMP_MS": "Datetime(time_unit='ms', time_zone=None)",
    "TIMESTAMP_NS": "Datetime(time_unit='ns', time_zone=None)",
    # Builder connections run with TimeZone = 'UTC' (duckdb_runtime), so an instant is
    # read back in UTC.
    "TIMESTAMP WITH TIME ZONE": "Datetime(time_unit='us', time_zone='UTC')",
    '"NULL"': "Null",
}

#: DuckDB types with no Builder spelling yet, and why. Asking for one is an error.
UNSUPPORTED: Mapping[str, str] = {
    "INTERVAL": "months, days and microseconds do not fit one duration unit",
    "TIMESTAMP_S": "the vocabulary has no second-precision datetime",
    "UHUGEINT": "the vocabulary has no unsigned 128-bit integer",
    "UUID": "no source produces one; decide its spelling when one does",
    "BIT": "no source produces one",
}

_DECIMAL = re.compile(r"^DECIMAL\((\d+),(\d+)\)$")


class UnsupportedDtype(ValueError):
    """A DuckDB type with no canonical Builder dtype."""


def canonical_dtype(duckdb_type: str) -> str:
    """The canonical dtype for a DuckDB type name as DuckDB prints it.

    Raises:
        UnsupportedDtype: The type has no Builder spelling.
    """
    name = duckdb_type.strip()
    if name in SCALAR_DTYPES:
        return SCALAR_DTYPES[name]
    decimal = _DECIMAL.match(name)
    if decimal:
        return f"Decimal(precision={decimal.group(1)}, scale={decimal.group(2)})"
    if name.endswith("[]"):
        return f"List({canonical_dtype(name[:-2])})"
    if name.startswith("STRUCT(") and name.endswith(")"):
        fields = ", ".join(
            f"{field!r}: {canonical_dtype(field_type)}"
            for field, field_type in _struct_fields(name[len("STRUCT(") : -1])
        )
        return f"Struct({{{fields}}})"
    reason = UNSUPPORTED.get(name.split("(")[0])
    raise UnsupportedDtype(
        f"DuckDB type {name!r} has no Builder dtype" + (f": {reason}" if reason else "")
    )


_INTEGERS = frozenset(
    ("Int8", "Int16", "Int32", "Int64", "Int128", "UInt8", "UInt16", "UInt32", "UInt64")
)
_STORED: Mapping[str, str] = {
    dtype: name
    for name, dtype in SCALAR_DTYPES.items()
    if name not in ('"NULL"', "TIMESTAMP WITH TIME ZONE")
}


def is_nested(dtype: str) -> bool:
    """Whether the dtype holds other values: a list, an array or a struct."""
    return dtype.startswith(("List(", "Array(", "Struct("))


def is_numeric(dtype: str) -> bool:
    """Whether the dtype is a number: an integer, a float or a decimal."""
    base = dtype.split("(", 1)[0]
    return base in _INTEGERS or base in ("Float32", "Float64", "Decimal")


def is_temporal(dtype: str) -> bool:
    """Whether the dtype is a date, a time, a datetime or a duration."""
    return dtype.split("(", 1)[0] in ("Date", "Time", "Datetime", "Duration")


def scalar_sql_type(dtype: str) -> str:
    """The DuckDB type a value of a scalar dtype is compared as in a query of a Builder
    table: the stored type, a zoned datetime as an instant and a duration as its
    microseconds (``query.sandbox``).

    Raises:
        ValueError: The dtype is nested or Null, which has no value to compare with.
    """
    if dtype in _STORED:
        return _STORED[dtype]
    base = dtype.split("(", 1)[0]
    if base == "Datetime":
        unit = re.search(r"time_unit='(\w+)'", dtype)
        if "time_zone=None" not in dtype:
            return "TIMESTAMP WITH TIME ZONE"
        return {"ms": "TIMESTAMP_MS", "ns": "TIMESTAMP_NS"}.get(
            unit.group(1) if unit else "us", "TIMESTAMP"
        )
    if base == "Duration":
        return "BIGINT"
    decimal = re.fullmatch(r"Decimal\(precision=(\d+), scale=(\d+)\)", dtype)
    if decimal:
        return f"DECIMAL({decimal.group(1)},{decimal.group(2)})"
    if base in ("Utf8", "Categorical"):
        return "VARCHAR"
    raise ValueError(f"no value can be compared with a {dtype} column")


def logical_type(canonical: str) -> str:
    """The canonical dtype without parameters, lower-cased: ``decimal``, ``datetime``…"""
    return canonical.split("(", 1)[0].lower()


def _struct_fields(body: str) -> list[tuple[str, str]]:
    """``a INTEGER, "x y" VARCHAR`` → ``[("a", "INTEGER"), ("x y", "VARCHAR")]``."""
    fields: list[tuple[str, str]] = []
    for part in _split_top_level(body):
        part = part.strip()
        if part.startswith('"'):
            end = 1
            while True:
                end = part.index('"', end)
                if part[end : end + 2] == '""':
                    end += 2
                    continue
                break
            name, rest = part[1:end].replace('""', '"'), part[end + 1 :]
        else:
            name, _, rest = part.partition(" ")
        fields.append((name, rest.strip()))
    return fields


def _split_top_level(body: str) -> list[str]:
    """Split on commas that are not inside parentheses or a quoted name."""
    parts: list[str] = []
    depth = 0
    quoted = False
    start = 0
    for index, char in enumerate(body):
        if char == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(body[start:index])
            start = index + 1
    parts.append(body[start:])
    return parts


__all__ = [
    "SCALAR_DTYPES",
    "UNSUPPORTED",
    "UnsupportedDtype",
    "canonical_dtype",
    "is_nested",
    "is_numeric",
    "is_temporal",
    "logical_type",
    "scalar_sql_type",
]

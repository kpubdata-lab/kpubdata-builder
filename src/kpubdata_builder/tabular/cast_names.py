"""Declared cast names and the audit record of a cast — engine-neutral (#869).

They lived in ``polars_helpers`` until the Polars engine went (#876).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

#: The type names a spec's ``casts`` and ``dtypes`` may declare, and the kind of value
#: each one is: ``boolean``, ``date``, ``datetime``, ``float``, ``int`` or ``string``.
NAMED_TARGETS: Mapping[str, str] = {
    "bool": "boolean",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "float": "float",
    "float64": "float",
    "int": "int",
    "int64": "int",
    "str": "string",
    "string": "string",
    "utf8": "string",
}

#: Casts that read a formatted number — source public data writes amounts with
#: thousands separators ("120,000"), which a plain int cast turns into nulls, failing
#: #188's data-loss guard — and the kind of value each gives.
FORMATTED_CASTS: Mapping[str, str] = {"int_comma": "int", "float_comma": "float"}

#: named cast collecting mixed notation strings into canonical text (#620). Separate from
#: the named dtypes because it is a casting strategy, not a dtype.
TEXT_CASTS: frozenset[str] = frozenset({"year_month"})

#: Two notations year_month accepts. **Fix length too** — loose parser reads "20230" as
#: "2023-0" etc., creating wrong year/month. Month range also blocked here. Python and
#: Rust (Polars) regex syntax, where ``\d`` is any Unicode decimal digit.
YEAR_MONTH_DASHED = r"^\d{4}-(0[1-9]|1[0-2])$"
YEAR_MONTH_COMPACT = r"^\d{4}(0[1-9]|1[0-2])$"

#: The same patterns for DuckDB's RE2, whose ``\d`` is ASCII only: ``\p{Nd}`` keeps a
#: year written in, say, full-width digits accepted as Polars accepted it (#891 review).
YEAR_MONTH_DASHED_RE2 = YEAR_MONTH_DASHED.replace(r"\d", r"\p{Nd}")
YEAR_MONTH_COMPACT_RE2 = YEAR_MONTH_COMPACT.replace(r"\d", r"\p{Nd}")


@dataclass(frozen=True)
class CastReport:
    """Per-column count of null values added by strict=False casting.

    Attributes:
        column: Target column name.
        nulls_before: Null count before casting.
        nulls_after: Null count after casting.
    """

    column: str
    nulls_before: int
    nulls_after: int

    @property
    def nulls_introduced(self) -> int:
        """Return count of newly created nulls from casting."""
        return self.nulls_after - self.nulls_before


__all__ = [
    "FORMATTED_CASTS",
    "NAMED_TARGETS",
    "TEXT_CASTS",
    "YEAR_MONTH_COMPACT",
    "YEAR_MONTH_COMPACT_RE2",
    "YEAR_MONTH_DASHED",
    "YEAR_MONTH_DASHED_RE2",
    "CastReport",
]

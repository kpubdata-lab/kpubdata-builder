"""Polars helper functions for making raw records into table form.

This module provides utilities to convert raw JSON-like records to Polars DataFrame,
perform required column validation and loose type casting.

Key components:
    - CastReport / CastResult: Report objects tracking null increase during casting
    - validate_required_columns: Check required column presence
    - cast_columns: Cast columns to specified types

records ↔ DataFrame conversion moved to convert module (#49).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, overload

import polars as pl

from .cast_names import TEXT_CASTS as TEXT_CASTS
from .cast_names import YEAR_MONTH_COMPACT as YEAR_MONTH_COMPACT
from .cast_names import YEAR_MONTH_DASHED as YEAR_MONTH_DASHED
from .cast_names import CastReport as CastReport

DtypeSpec = str | pl.DataType | type[pl.DataType]

#: Named cast for reading mixed-format strings as numbers (#611). Casting
#: strategy, not dtype, so separated from _NAMED_DTYPES — source public data amounts are
#: strings with thousands separators ("120,000"), and direct int cast → all null
#: making #188's data-loss guard fail the build.
_FORMATTED_CASTS: Mapping[str, pl.DataType] = {
    "int_comma": pl.Int64(),
    "float_comma": pl.Float64(),
}

_TRUE_TOKENS = {"1", "t", "true", "y", "yes"}
_FALSE_TOKENS = {"0", "f", "false", "n", "no"}

_NAMED_DTYPES: Mapping[str, pl.DataType] = {
    "bool": pl.Boolean(),
    "boolean": pl.Boolean(),
    "date": pl.Date(),
    "datetime": pl.Datetime(),
    "float": pl.Float64(),
    "float64": pl.Float64(),
    "int": pl.Int64(),
    "int64": pl.Int64(),
    "str": pl.Utf8(),
    "string": pl.Utf8(),
    "utf8": pl.Utf8(),
}


@dataclass(frozen=True)
class CastResult:
    """Value object holding audit result and per-column null report.

    Attributes:
        df: Cast-complete DataFrame.
        reports: Collection of column reports where null increase detected.
    """

    df: pl.DataFrame
    reports: tuple[CastReport, ...] = field(default_factory=tuple)

    @property
    def has_nulls_introduced(self) -> bool:
        """Return whether null increased in any column."""
        return any(r.nulls_introduced > 0 for r in self.reports)


def validate_required_columns(
    df: pl.DataFrame,
    required_columns: Sequence[str],
) -> pl.DataFrame:
    """Verify all required columns present, raise exception if missing.

    Args:
        df: DataFrame to check.
        required_columns: List of column names that must exist.

    Returns:
        pl.DataFrame: Same DataFrame as input.

    Raises:
        ValueError: If one or more required columns absent.
    """
    missing_columns = [column for column in required_columns if column not in df.columns]
    if missing_columns:
        missing = ", ".join(missing_columns)
        raise ValueError(f"Missing required columns: {missing}")
    return df


@overload
def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
    *,
    audit: Literal[False] = ...,
) -> pl.DataFrame: ...


@overload
def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
    *,
    audit: Literal[True],
) -> CastResult: ...


def cast_columns(
    df: pl.DataFrame,
    dtypes: Mapping[str, DtypeSpec],
    *,
    audit: bool = False,
) -> pl.DataFrame | CastResult:
    """Cast column to specified type, return null report if needed.

    If audit=True, return CastResult with per-column null report.
    If audit=False (default), return DataFrame directly.
    """
    reports: list[CastReport] = []
    expressions: list[pl.Expr] = []
    nulls_before: dict[str, int] = {}

    for column, dtype in dtypes.items():
        if column not in df.columns:
            raise ValueError(
                f"Cannot cast missing column: {column!r}. Available columns: {df.columns}"
            )
        formatted = _formatted_cast(dtype)
        if formatted is not None:
            expressions.append(_cast_formatted_numeric(column, formatted))
            continue
        if isinstance(dtype, str) and dtype.strip().lower() == "year_month":
            expressions.append(_cast_year_month(column))
            continue
        resolved_dtype = _resolve_dtype(dtype)
        if isinstance(resolved_dtype, pl.Boolean):
            expressions.append(_cast_boolean(column))
        else:
            expressions.append(pl.col(column).cast(resolved_dtype, strict=False).alias(column))

    if not expressions:
        if audit:
            return CastResult(df=df, reports=())
        return df

    if audit:
        nulls_before = {col: df[col].null_count() for col in dtypes if col in df.columns}

    result_df = df.with_columns(expressions)

    if audit:
        for column in dtypes:
            if column in df.columns:
                report = CastReport(
                    column=column,
                    nulls_before=nulls_before[column],
                    nulls_after=result_df[column].null_count(),
                )
                if report.nulls_introduced > 0:
                    reports.append(report)
        return CastResult(df=result_df, reports=tuple(reports))

    return result_df


def _formatted_cast(dtype: DtypeSpec) -> pl.DataType | None:
    """Return target dtype if named formatted cast, else None (#611)."""
    if isinstance(dtype, str):
        return _FORMATTED_CASTS.get(dtype.strip().lower())
    return None


def _cast_formatted_numeric(column: str, dtype: pl.DataType) -> pl.Expr:
    """Remove thousands separator and whitespace, cast to number (#611).

    Failure to remove separator leaves value that becomes null via strict=False,
    and caller's audit detects that loss — widening format, not hiding failure.
    """
    return (
        pl.col(column)
        .cast(pl.Utf8)
        .str.replace_all(",", "")
        .str.strip_chars()
        .cast(dtype, strict=False)
        .alias(column)
    )


def _cast_year_month(column: str) -> pl.Expr:
    """Collect ``2020-01`` and ``202207`` into canonical ``"YYYY-MM"`` (#620).

    Text, not Date. Making it ``pl.Date`` adds ``01`` (day) not in source;
    Polars lacks month-only period type. Downstream handles if month-series interpretation needed.

    Values matching neither notation become null, caught by caller's audit. But audit
    message only reports count, so Silver normalization checks which values rejected first
    — ``normalize._year_month_violations``.
    """
    text = pl.col(column).cast(pl.Utf8).str.strip_chars()
    return (
        pl.when(text.str.contains(YEAR_MONTH_DASHED))
        .then(text)
        .when(text.str.contains(YEAR_MONTH_COMPACT))
        .then(text.str.slice(0, 4) + pl.lit("-") + text.str.slice(4, 2))
        .otherwise(pl.lit(None, dtype=pl.Utf8))
        .alias(column)
    )


def _resolve_dtype(dtype: DtypeSpec) -> pl.DataType:
    """Interpret string or Polars type spec as actual DataType instance."""
    if isinstance(dtype, str):
        normalized = dtype.strip().lower()
        try:
            return _NAMED_DTYPES[normalized]
        except KeyError as exc:
            supported = ", ".join(sorted(_NAMED_DTYPES.keys()))
            raise ValueError(f"Unsupported dtype: {dtype!r}. Supported: {supported}") from exc
    if isinstance(dtype, pl.DataType):
        return dtype
    if isinstance(dtype, type) and issubclass(dtype, pl.DataType):
        return dtype()
    raise TypeError(f"Invalid dtype spec: {dtype!r}")


def _cast_boolean(column: str) -> pl.Expr:
    """Safely convert string-based boolean tokens to Polars Boolean expression.

    Keep unknown values as null instead of strict failure, so audit stage can detect.
    """
    normalized = pl.col(column).cast(pl.Utf8).str.strip_chars().str.to_lowercase()
    return (
        # Normalize multiple truthy/falsy notations into single bool representation.
        pl.when(normalized.is_in(_TRUE_TOKENS))
        .then(pl.lit(True))
        .when(normalized.is_in(_FALSE_TOKENS))
        .then(pl.lit(False))
        .otherwise(pl.lit(None, dtype=pl.Boolean))
        .alias(column)
    )

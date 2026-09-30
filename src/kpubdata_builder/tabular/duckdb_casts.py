"""Builder's cast rules, expressed in DuckDB SQL (#868, ADR 0021).

``polars_helpers.cast_columns`` is the contract: a declared cast either converts a value
or turns it into null, and the Silver audit fails the build when a cast introduced a
null (#188). DuckDB's own casts accept different inputs — ``CAST(' 12' AS BIGINT)`` is
12, ``CAST(1.9 AS BIGINT)`` is 2 — so none is used bare. Every cast here is:

1. **lexical validation** — the text must match the grammar the current engine accepts;
2. **conversion** — only then is the value converted, with the rounding stated
   (float → integer truncates, as Polars does);
3. **audit** — the caller counts nulls before and after (:func:`cast_table`), exactly as
   ``cast_columns(audit=True)`` does.

The grammars were read off Polars 1.x and are pinned value by value against it in
``tests/unit/test_duckdb_casts.py``. They match it everywhere except where Polars
returns a value that is not the one written — those become null here, and the audit
fails the build instead of storing a wrong value:

- a date whose year is not four digits (Polars reads ``24-01-01`` as year 24), year
  0000, or a negative year;
- a datetime with a UTC offset or zone text (Polars drops ``+09:00`` and keeps the
  wall-clock time as if it were UTC);
- an integer beyond ±2**53 cast to a float (Polars rounds it);
- a leap second (Polars moves ``12:30:60`` to ``12:31:00``);
- a date or datetime outside years 1-9999 (Polars produces one, or panics, where Python
  and JSON clients cannot hold it).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import duckdb

from .cast_names import (
    TEXT_CASTS,
    YEAR_MONTH_COMPACT_RE2,
    YEAR_MONTH_DASHED_RE2,
    CastReport,
)
from .sql import quote_identifier

#: Whitespace Polars' ``strip_chars`` removes: Unicode White_Space.
_SPACE = r"[\t\n\x0B\f\r\x{85}\p{Z}]"
_INT = r"[+-]?[0-9]+"
_FLOAT = r"(?i)[+-]?(([0-9]+\.?[0-9]*|\.[0-9]+)(e[+-]?[0-9]+)?|inf|infinity|nan)"
_DATE = r"\s*\+?([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})\s*"
_DATETIME = (
    r"\s*([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})T([0-9]{1,2}):([0-9]{1,2})"
    r"(?::([0-9]{1,2})(?:\.([0-9]+))?)?[Zz]?\s*"
)
_JS_SAFE = 2**53
#: Dates and datetimes are kept within 0001-01-01 .. 9999-12-31, the range Python (and
#: every client reading JSON dates) can hold. Polars produces years beyond it, or
#: panics; here they are nulls the audit reports.
_MIN_DAYS = -719162
_MAX_DAYS = 2932896
_MIN_MICROS = _MIN_DAYS * 86_400_000_000
_MAX_MICROS = (_MAX_DAYS + 1) * 86_400_000_000 - 1

#: Named targets, as ``polars_helpers._NAMED_DTYPES`` spells them.
_TARGETS: Mapping[str, str] = {
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
_FORMATTED: Mapping[str, str] = {"int_comma": "int", "float_comma": "float"}
_TRUE = ("1", "t", "true", "y", "yes")
_FALSE = ("0", "f", "false", "n", "no")

#: The SQL macro that spells a float as Polars does (see :data:`_FLOAT_TEXT_MACROS`).
FLOAT_TEXT_FUNCTION = "kpubdata_float_text"

#: Polars writes a float as its shortest round-trip digits, in fixed notation when the
#: leading digit's decimal exponent is in -5..15 and as ``1.5e+20`` / ``1e-7`` otherwise;
#: ``NaN``, ``inf``, ``-inf``; ``-0.0``. DuckDB's own text for a double carries the same
#: shortest digits in another layout, so these macros take the digits and exponent out
#: of it and lay them out Polars' way. Pinned against Polars on random doubles in
#: tests/unit/test_duckdb_casts.py.
_FLOAT_TEXT_MACROS = (
    # a: DuckDB's text without its sign
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_mant(a) AS split_part(a, 'e', 1)",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_exp(a) AS "
    "CASE WHEN contains(a, 'e') THEN CAST(split_part(a, 'e', 2) AS INTEGER) ELSE 0 END",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_int(a) AS split_part(kpubdata_ft_mant(a), '.', 1)",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_frac(a) AS "
    "CASE WHEN contains(kpubdata_ft_mant(a), '.') "
    "THEN split_part(kpubdata_ft_mant(a), '.', 2) ELSE '' END",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_digits(a) AS "
    "rtrim(ltrim(kpubdata_ft_int(a) || kpubdata_ft_frac(a), '0'), '0')",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_point(a) AS "
    "CASE WHEN ltrim(kpubdata_ft_int(a), '0') <> '' "
    "THEN length(ltrim(kpubdata_ft_int(a), '0')) + kpubdata_ft_exp(a) "
    "ELSE kpubdata_ft_exp(a) - (length(kpubdata_ft_frac(a)) "
    "- length(ltrim(kpubdata_ft_frac(a), '0'))) END",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_layout(d, p) AS "
    "CASE WHEN d = '' THEN '0.0' "
    "WHEN p - 1 BETWEEN -5 AND 15 THEN "
    "  CASE WHEN p <= 0 THEN '0.' || repeat('0', -p) || d "
    "       WHEN p >= length(d) THEN d || repeat('0', p - length(d)) || '.0' "
    "       ELSE left(d, p) || '.' || substr(d, p + 1) END "
    "ELSE left(d, 1) || CASE WHEN length(d) > 1 THEN '.' || substr(d, 2) ELSE '' END "
    "  || 'e' || CASE WHEN p - 1 > 0 THEN '+' ELSE '-' END || CAST(abs(p - 1) AS VARCHAR) END",
    "CREATE OR REPLACE TEMP MACRO kpubdata_ft_abs(a) AS "
    "kpubdata_ft_layout(kpubdata_ft_digits(a), kpubdata_ft_point(a))",
    f"CREATE OR REPLACE TEMP MACRO {FLOAT_TEXT_FUNCTION}(x) AS "
    "CASE WHEN x IS NULL THEN NULL WHEN isnan(x) THEN 'NaN' "
    "WHEN isinf(x) THEN CASE WHEN x > 0 THEN 'inf' ELSE '-inf' END "
    "ELSE CASE WHEN signbit(x) THEN '-' ELSE '' END "
    "  || kpubdata_ft_abs(ltrim(CAST(x AS VARCHAR), '-')) END",
)


def register_functions(connection: duckdb.DuckDBPyConnection) -> None:
    """Add the macros these casts use to ``connection``. Safe to call again."""
    for statement in _FLOAT_TEXT_MACROS:
        connection.execute(statement)


def _kind(dtype: str) -> str:
    """The family of a canonical dtype (``dtypes.canonical_dtype``)."""
    base = dtype.split("(", 1)[0]
    if base in ("Int8", "Int16", "Int32", "Int64", "Int128", "UInt8", "UInt16", "UInt32"):
        return "int"
    if base == "UInt64":
        return "int"
    if base in ("Float32", "Float64"):
        return "float"
    if base == "String":
        return "string"
    if base == "Boolean":
        return "boolean"
    if base == "Date":
        return "date"
    if base == "Datetime":
        return "datetime"
    if base == "Null":
        return "null"
    return "other"


def _full(pattern: str, value: str) -> str:
    return f"regexp_full_match({value}, '{pattern}')"


def strip_expression(text: str) -> str:
    """``text`` without leading and trailing whitespace, as Polars' ``strip_chars``."""
    return f"regexp_replace({text}, '^{_SPACE}+|{_SPACE}+$', '', 'g')"


_strip = strip_expression


def text_expression(column: str, source: str) -> str:
    """``column`` as text, the way Polars' cast to ``Utf8`` writes it."""
    kind = _kind(source)
    if kind == "string":
        return column
    if kind == "float":
        return f"{FLOAT_TEXT_FUNCTION}(CAST({column} AS DOUBLE))"
    if kind == "date":
        return f"strftime({column}, '%Y-%m-%d')"
    if kind == "datetime":
        return f"strftime({column}, '%Y-%m-%d %H:%M:%S.%f')"
    if kind == "null":
        return "CAST(NULL AS VARCHAR)"
    return f"CAST({column} AS VARCHAR)"


def _int_from_text(text: str) -> str:
    return f"CASE WHEN {_full(_INT, text)} THEN TRY_CAST({text} AS BIGINT) END"


def _float_from_text(text: str) -> str:
    return f"CASE WHEN {_full(_FLOAT, text)} THEN TRY_CAST({text} AS DOUBLE) END"


def _date_from_text(text: str) -> str:
    part = f"regexp_extract({text}, '^{_DATE}$', ['y', 'm', 'd'])"
    return (
        f"CASE WHEN {_full(_DATE, text)} AND CAST({part}.y AS INTEGER) >= 1 "
        f"THEN TRY_CAST(printf('%04d-%02d-%02d', CAST({part}.y AS INTEGER), "
        f"CAST({part}.m AS INTEGER), CAST({part}.d AS INTEGER)) AS DATE) END"
    )


def _datetime_from_text(text: str) -> str:
    part = f"regexp_extract({text}, '^{_DATETIME}$', ['y', 'mo', 'd', 'h', 'mi', 's', 'f'])"
    return (
        f"CASE WHEN {_full(_DATETIME, text)} AND CAST({part}.y AS INTEGER) >= 1 "
        f"AND CAST({part}.h AS INTEGER) <= 23 AND CAST({part}.mi AS INTEGER) <= 59 "
        f"AND CAST(COALESCE(NULLIF({part}.s, ''), '0') AS INTEGER) <= 59 "
        f"THEN TRY_CAST(printf('%04d-%02d-%02d %02d:%02d:%02d.%s', "
        f"CAST({part}.y AS INTEGER), CAST({part}.mo AS INTEGER), CAST({part}.d AS INTEGER), "
        f"CAST({part}.h AS INTEGER), CAST({part}.mi AS INTEGER), "
        f"CAST(COALESCE(NULLIF({part}.s, ''), '0') AS INTEGER), "
        f"rpad(left({part}.f, 6), 6, '0')) AS TIMESTAMP) END"
    )


def cast_expression(column: str, source: str, target: str) -> str:
    """The SQL for casting ``column`` (quoted) of canonical dtype ``source`` to ``target``.

    ``target`` is a cast as a BuildSpec declares it: a named dtype (``int``, ``float``,
    ``str``, ``date``, ``datetime``, ``bool`` and their aliases), a formatted numeric cast
    (``int_comma``, ``float_comma``) or ``year_month``.

    Raises:
        ValueError: ``target`` is not a cast Builder knows.
    """
    name = target.strip().lower()
    kind = _kind(source)
    if name in _FORMATTED:
        text = _strip(f"replace({text_expression(column, source)}, ',', '')")
        return _int_from_text(text) if _FORMATTED[name] == "int" else _float_from_text(text)
    if name in TEXT_CASTS:
        text = _strip(text_expression(column, source))
        compact = f"left({text}, 4) || '-' || substr({text}, 5, 2)"
        return (
            f"CASE WHEN regexp_matches({text}, '{YEAR_MONTH_DASHED_RE2}') THEN {text} "
            f"WHEN regexp_matches({text}, '{YEAR_MONTH_COMPACT_RE2}') THEN {compact} END"
        )
    if name not in _TARGETS:
        supported = ", ".join(sorted(_TARGETS))
        raise ValueError(f"Unsupported dtype: {target!r}. Supported: {supported}")
    goal = _TARGETS[name]
    if kind == "null":
        return {
            "int": "CAST(NULL AS BIGINT)",
            "float": "CAST(NULL AS DOUBLE)",
            "string": "CAST(NULL AS VARCHAR)",
            "date": "CAST(NULL AS DATE)",
            "datetime": "CAST(NULL AS TIMESTAMP)",
            "boolean": "CAST(NULL AS BOOLEAN)",
        }[goal]
    if goal == "string":
        return text_expression(column, source)
    if goal == "boolean":
        token = f"lower({_strip(text_expression(column, source))})"
        true = ", ".join(f"'{t}'" for t in _TRUE)
        false = ", ".join(f"'{t}'" for t in _FALSE)
        return f"CASE WHEN {token} IN ({true}) THEN true WHEN {token} IN ({false}) THEN false END"
    if goal == "int":
        if kind == "string":
            return _int_from_text(column)
        if kind == "float":
            # Truncation, stated: DuckDB's own CAST rounds 1.9 to 2.
            return f"TRY_CAST(trunc({column}) AS BIGINT)"
        if kind == "date":
            return f"CAST(({column} - DATE '1970-01-01') AS BIGINT)"
        if kind == "datetime":
            return f"epoch_us({column})"
        return f"TRY_CAST({column} AS BIGINT)"
    if goal == "float":
        if kind == "string":
            return _float_from_text(column)
        if kind == "int":
            # Polars rounds an integer beyond 2**53; here it is a null the audit reports.
            return (
                f"CASE WHEN {column} BETWEEN -{_JS_SAFE} AND {_JS_SAFE} "
                f"THEN CAST({column} AS DOUBLE) END"
            )
        if kind == "date":
            return f"CAST(({column} - DATE '1970-01-01') AS DOUBLE)"
        if kind == "datetime":
            return f"CAST(epoch_us({column}) AS DOUBLE)"
        return f"TRY_CAST({column} AS DOUBLE)"
    if goal == "date":
        if kind == "string":
            return _date_from_text(column)
        if kind == "date":
            return column
        if kind == "datetime":
            return f"CAST({column} AS DATE)"
        days = _epoch_units(column, kind)
        if days is None:
            return "CAST(NULL AS DATE)"
        return (
            f"CASE WHEN {days} BETWEEN {_MIN_DAYS} AND {_MAX_DAYS} "
            f"THEN DATE '1970-01-01' + CAST({days} AS INTEGER) END"
        )
    # datetime
    if kind == "string":
        return _datetime_from_text(column)
    if kind == "datetime":
        return f"CAST({column} AS TIMESTAMP)"
    if kind == "date":
        return f"CAST({column} AS TIMESTAMP)"
    micros = _epoch_units(column, kind)
    if micros is None:
        return "CAST(NULL AS TIMESTAMP)"
    return (
        f"CASE WHEN {micros} BETWEEN {_MIN_MICROS} AND {_MAX_MICROS} "
        f"THEN make_timestamp(CAST({micros} AS BIGINT)) END"
    )


def _epoch_units(column: str, kind: str) -> str | None:
    """A numeric source as whole epoch units — days for a date, µs for a datetime — as
    Polars reads it: integers as they are, booleans as 0/1, floats truncated."""
    if kind == "int":
        return column
    if kind == "boolean":
        return f"CAST({column} AS BIGINT)"
    if kind == "float":
        return f"CASE WHEN isfinite({column}) THEN trunc({column}) END"
    return None


def zfill_expression(column: str, width: int) -> str:
    """``column`` zero-filled to ``width`` as Polars' ``str.zfill`` does it.

    A leading ``+`` or ``-`` stays in front. The padding is counted in **bytes**, as
    Polars counts it, so a value of non-ASCII text is not padded; the width check before
    it (:func:`zfill_violations`) counts characters, as Silver's does.
    """
    missing = f"({width} - strlen({column}))"
    return (
        f"CASE WHEN {column} IS NULL OR {missing} <= 0 THEN {column} "
        f"WHEN left({column}, 1) IN ('+', '-') "
        f"THEN left({column}, 1) || repeat('0', {missing}) || substr({column}, 2) "
        f"ELSE repeat('0', {missing}) || {column} END"
    )


def zfill_violations(
    connection: duckdb.DuckDBPyConnection, table: str, column: str, width: int
) -> tuple[int, int]:
    """``(count, longest)`` of values longer than ``width`` characters — a build failure
    (#620): identifiers are never truncated."""
    quoted = quote_identifier(column)
    row = connection.execute(
        f"SELECT count(*), coalesce(max(length({quoted})), 0) FROM {quote_identifier(table)} "
        f"WHERE length({quoted}) > ?",
        [width],
    ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


@dataclass(frozen=True)
class CastOutcome:
    """The cast table's name and, per cast column, the nulls the cast introduced."""

    table: str
    reports: tuple[CastReport, ...]

    @property
    def has_nulls_introduced(self) -> bool:
        return any(r.nulls_introduced > 0 for r in self.reports)


def cast_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    casts: Mapping[str, str],
    *,
    into: str,
) -> CastOutcome:
    """Write ``into`` as ``table`` with ``casts`` applied, and audit the nulls.

    The column order is kept. Columns are matched by exact name.

    Raises:
        ValueError: A cast names a column the table does not have, or an unknown dtype.
    """
    register_functions(connection)
    described = connection.execute(f"DESCRIBE {quote_identifier(table)}").fetchall()
    columns = [(str(row[0]), str(row[1])) for row in described]
    names = [name for name, _ in columns]
    for column in casts:
        if column not in names:
            raise ValueError(f"Cannot cast missing column: {column!r}. Available columns: {names}")
    from .dtypes import canonical_dtype

    select = []
    for name, duck_type in columns:
        quoted = quote_identifier(name)
        if name in casts:
            expression = cast_expression(quoted, canonical_dtype(duck_type), casts[name])
            select.append(f"{expression} AS {quoted}")
        else:
            select.append(quoted)
    before = _null_counts(connection, table, list(casts))
    connection.execute(
        f"CREATE OR REPLACE TABLE {quote_identifier(into)} AS "
        f"SELECT {', '.join(select)} FROM {quote_identifier(table)}"
    )
    after = _null_counts(connection, into, list(casts))
    reports = tuple(
        CastReport(column=c, nulls_before=before[c], nulls_after=after[c])
        for c in casts
        if after[c] > before[c]
    )
    return CastOutcome(table=into, reports=reports)


def _null_counts(
    connection: duckdb.DuckDBPyConnection, table: str, columns: list[str]
) -> dict[str, int]:
    if not columns:
        return {}
    counts = ", ".join(f"count(*) - count({quote_identifier(c)})" for c in columns)
    row = connection.execute(f"SELECT {counts} FROM {quote_identifier(table)}").fetchone()
    assert row is not None
    return {c: int(v) for c, v in zip(columns, row, strict=True)}


__all__ = [
    "FLOAT_TEXT_FUNCTION",
    "CastOutcome",
    "cast_expression",
    "cast_table",
    "register_functions",
    "strip_expression",
    "text_expression",
    "zfill_expression",
    "zfill_violations",
]

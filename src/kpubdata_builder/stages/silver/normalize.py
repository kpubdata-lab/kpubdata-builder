"""Silver normalization (#46).

Convert Bronze raw records to Polars table and apply only declared normalization rules
(type casting). Builder does not arbitrarily define "clean data"—undeclared
transformations are not performed.

Main functions:
    - normalize_table: BronzeArtifact → pl.DataFrame
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import polars as pl

from ...errors import TabularError
from ...spec import ColumnNullTokens, DerivedColumn, JsonValue
from ...tabular.convert import records_to_dataframe
from ...tabular.polars_helpers import (
    YEAR_MONTH_COMPACT,
    YEAR_MONTH_DASHED,
    DtypeSpec,
    cast_columns,
)
from ..bronze.models import BronzeArtifact

#: separator for join_key derived columns (#611).
JOIN_KEY_SEPARATOR = "|"

#: escape character used when embedding separator within values (#611).
JOIN_KEY_ESCAPE = "\\"


def normalize_table(
    bronze: BronzeArtifact,
    *,
    casts: Mapping[str, DtypeSpec] | None = None,
    rename: Mapping[str, str] | None = None,
    derived: Sequence[DerivedColumn] = (),
    read_as: Mapping[str, str] | None = None,
    null_tokens: Sequence[str] = (),
    column_null_tokens: Mapping[str, ColumnNullTokens] | None = None,
    coalesce: Mapping[str, Sequence[str]] | None = None,
    zfill: Mapping[str, int] | None = None,
) -> pl.DataFrame:
    """converts Bronze raw records to table and applies only declared casts."""
    # null_tokens applied *before* table is created. records_to_dataframe
    # rejects heterogeneous type columns(#187), if public API gives missing as "", same column has
    # number 84.5 and string "" mixed, build stops before declaration takes effect — declared
    # notation must first be collected as null for that declaration to take effect (#613).
    # Silver still builds one Polars table, so it reads every Bronze record here; the
    # DuckDB Silver (#869) reads the file instead. Bronze itself no longer holds them (#622).
    records = _apply_null_tokens(list(bronze.iter_records()), null_tokens, column_null_tokens or {})
    table = records_to_dataframe(records, read_as=read_as)
    if coalesce:
        for target, candidates in _coalesce_order(coalesce):
            table = _apply_coalesce(table, target, candidates)
    if rename:
        missing = [source for source in rename if source not in table.columns]
        if missing:
            raise TabularError(
                f"declared rename refers to columns absent from the source: {missing}"
            )
        # two source columns merge to same name, or target name overlaps with existing
        # column, Polars raises DuplicateError — not internal exception
        # fail in spec terms. (value duplicates caught by validator at declaration time.)
        untouched = set(table.columns) - set(rename)
        collisions = sorted({target for target in rename.values() if target in untouched})
        if collisions:
            raise TabularError(
                f"declared rename targets collide with existing columns: {collisions}"
            )
        table = table.rename(dict(rename))
    if zfill:
        for column, width in zfill.items():
            table = _apply_zfill(table, column, width)
    if casts:
        _check_year_month(table, casts)
        result = cast_columns(table, casts, audit=True)
        if result.has_nulls_introduced:
            details = "; ".join(
                f"{report.column!r}: {report.nulls_introduced} value(s) -> null"
                for report in result.reports
                if report.nulls_introduced > 0
            )
            raise TabularError(f"declared cast dropped values to null (data loss): {details}")
        table = result.df
    for rule in derived:
        table = _apply_derived(table, rule)
    return table


def _apply_null_tokens(
    records: Sequence[dict[str, JsonValue]],
    null_tokens: Sequence[str],
    column_null_tokens: Mapping[str, ColumnNullTokens],
) -> Sequence[dict[str, JsonValue]]:
    """collects missing notation as null. global + per-column declaration (#620, #623).

    Missing representation recognized in one column is **global + that column's declaration**.
    Per-column declaration does not overwrite global—if it did, adding one token to one
    column could silently lose global tokens.

    Same-meaning missing values may be notated differently per column. Global declaration alone
    cannot express that **without changing other columns' meaning**—declaring empty string as
    missing in gender would also null empty strings in rental station names.

    Fails if declared column doesn't exist in table. If typo silently does nothing,
    missing stays as value and quality metrics don't count it.

    runs on raw records *before* table creation (#613). records_to_dataframe
    rejects heterogeneous type columns (#187). if public API gives missing as `""`, same column has
    Number ``84.5`` mixed with string ``""`` causes build to halt before declaration
    even takes effect.
    """
    if not null_tokens and not column_null_tokens:
        return records

    # source column set is union of record keys — keys may be missing per record.
    columns: dict[str, None] = {}
    for record in records:
        for key in record:
            columns.setdefault(key, None)

    # "what is missing in this column" and "must this column always exist" are separate
    # contracts. latter declared separately with on_absent — else marking missing would mean
    # all generations must have that column.
    missing = [
        name
        for name, rule in column_null_tokens.items()
        if name not in columns and rule.on_absent == "error"
    ]
    if missing:
        raise TabularError(
            f"declared column_null_tokens refers to columns absent from the source: {missing}. "
            "Declare on_absent: ignore if the column is optional in this source."
        )
    present = [name for name in column_null_tokens if name in columns]

    # token cannot match in column with no string values. silently do nothing
    # or no one knows declaration is wrong. all-null column is exception — match
    # no values exist, not that declaration is wrong.
    wrong_type: dict[str, str] = {}
    for name in present:
        values = [record[name] for record in records if record.get(name) is not None]
        if values and not any(isinstance(value, str) for value in values):
            wrong_type[name] = type(values[0]).__name__
    if wrong_type:
        raise TabularError(
            f"column_null_tokens declared on non-string columns: {wrong_type}. "
            "Declare read_as so the source values are read as text."
        )

    shared = frozenset(null_tokens)
    per_column = {name: shared | frozenset(column_null_tokens[name].tokens) for name in present}
    return [
        {
            key: (
                None if isinstance(value, str) and value in per_column.get(key, shared) else value
            )
            for key, value in record.items()
        }
        for record in records
    ]


def _coalesce_order(
    coalesce: Mapping[str, Sequence[str]],
) -> list[tuple[str, tuple[str, ...]]]:
    """verifies coalesce rules don't overlap and determines application order (#620).

    Each rule removes converged candidate columns. So if one rule's target is another rule's
    candidate (or two rules contend for same candidate), result depends on rule traversal order
    —source column ``x`` with ``{"a": ["x"], "b": ["a"]}`` succeeds in ``a,b`` order but
    fails in ``b,a`` order. Also ``canonical_spec_mapping()`` sorts keys for snapshot, so
    same-digest declaration may behave differently from original build. Reproducible recipe
    contract breaks there—so overlapping groups are rejected instead of choosing order.
    """
    rules = [(target, tuple(candidates)) for target, candidates in coalesce.items()]
    targets = {target for target, _ in rules}
    seen: dict[str, str] = {}
    for target, candidates in rules:
        for candidate in candidates:
            if candidate in targets and candidate != target:
                raise TabularError(
                    f"coalesce target {candidate!r} is also a candidate of {target!r}; "
                    "overlapping coalesce groups make the result depend on declaration "
                    "order. Declare independent alias groups."
                )
            owner = seen.setdefault(candidate, target)
            if owner != target:
                raise TabularError(
                    f"coalesce candidate {candidate!r} is claimed by both {owner!r} and "
                    f"{target!r}; overlapping coalesce groups make the result depend on "
                    "declaration order. Declare independent alias groups."
                )
    # non-overlapping, result same regardless of order. declaration order does not remain
    # in result, so sort, snapshot replay follows same order as original build.
    return sorted(rules)


def _apply_coalesce(table: pl.DataFrame, target: str, candidates: tuple[str, ...]) -> pl.DataFrame:
    """collects per-generation alias columns into single canonical column (#620)."""
    present = [name for name in candidates if name in table.columns]
    if not present:
        raise TabularError(
            f"coalesce target {target!r} found none of its candidates in the source: "
            f"{list(candidates)}"
        )
    # if target name overlaps with non-candidate existing column, silently overwrites it.
    # contract limiting to alias group breaks there.
    if target in table.columns and target not in present:
        raise TabularError(
            f"coalesce target {target!r} would overwrite an existing column that is not "
            "one of its candidates"
        )
    # all-null candidate inferred as pl.Null — common shape in mixed-generation snapshots, and,
    # fits any type so excluded from consensus judgment.
    dtypes = {table.schema[name] for name in present} - {pl.Null}
    if len(dtypes) > 1:
        raise TabularError(
            f"coalesce target {target!r} has candidates of differing dtypes: "
            f"{ {name: str(table.schema[name]) for name in present} }. "
            "Declare read_as to read them as one type."
        )
    if len(present) > 1:
        # if multiple non-null candidates in one row with different values, generation
        # boundary wrong; caught incorrectly. first-wins silently passes wrong value.
        distinct = (
            pl.concat_list([pl.col(name) for name in present])
            .list.drop_nulls()
            .list.unique()
            .list.len()
        )
        conflicts = table.select(present).filter(distinct > 1)
        if conflicts.height:
            # does not include value itself. this message appears in manifest and /builds response
            # and, PII scan(#441)runs after it — if original value included
            # resident ID/phone leaks before scan. Naming which column and how
            # many rows is sufficient to locate the fix.
            raise TabularError(
                f"coalesce target {target!r} has {conflicts.height} row(s) where candidates "
                f"{present} disagree"
            )
    # extract value first then discard candidate. target may have same name as one candidate, so
    # reversing drop and with_columns order makes newly created column disappear.
    merged = table.select(pl.coalesce([pl.col(name) for name in present]).alias(target)).to_series()
    return table.drop(present).with_columns(merged)


def _apply_zfill(table: pl.DataFrame, column: str, width: int) -> pl.DataFrame:
    """aligns identifier width (#620).

    Same rental station as ``3`` and ``00003`` splits in aggregation. null stays null—if
    filled with ``"00000"``, missing becomes valid identifier and quality metrics count
    different missing values.
    """
    if column not in table.columns:
        raise TabularError(f"declared zfill refers to a column absent from the table: {column!r}")
    dtype = table.schema[column]
    if dtype == pl.Null:
        # if all values null, Polars infers as pl.Null — mixed-generation snapshots or
        # common result of coalescing all-null alias. cannot be resolved by read_as
        # (_apply_read_as deliberately does not touch null). zfill leaves null as
        # so no reason to reject this shape, thus, promote to string column
        # but leave values as null.
        return table.with_columns(pl.col(column).cast(pl.Utf8).alias(column))
    if dtype != pl.Utf8:
        raise TabularError(
            f"zfill target {column!r} is {dtype}, not a string; "
            "declare read_as so the leading zeros survive reading"
        )
    # values longer than declared width fail instead of being truncated. silent truncation
    # corrupts identifiers; contract declares width, longer values are drift signal.
    too_long = table.filter(pl.col(column).str.len_chars() > width)
    if too_long.height:
        longest = int(too_long.select(pl.col(column).str.len_chars().max()).item())
        raise TabularError(
            f"zfill target {column!r} has {too_long.height} value(s) longer than the declared "
            f"width {width} (longest is {longest} characters)"
        )
    return table.with_columns(pl.col(column).str.zfill(width).alias(column))


def _check_year_month(table: pl.DataFrame, casts: Mapping[str, DtypeSpec]) -> None:
    """pre-reports values that `year_month` cast will reject (#620).

    cast itself nulls mismatched values; #188's audit counts them. count alone is insufficient
    What and why is unclear; R2 must read exactly that.
    """
    for column, dtype in casts.items():
        if not isinstance(dtype, str) or dtype.strip().lower() != "year_month":
            continue
        if column not in table.columns:
            continue
        text = pl.col(column).cast(pl.Utf8).str.strip_chars()
        bad = table.filter(
            text.is_not_null()
            & ~text.str.contains(YEAR_MONTH_DASHED)
            & ~text.str.contains(YEAR_MONTH_COMPACT)
        )
        if bad.height:
            raise TabularError(
                f"year_month cast on {column!r} rejected {bad.height} value(s); "
                "expected YYYY-MM or YYYYMM"
            )


def _apply_derived(table: pl.DataFrame, rule: DerivedColumn) -> pl.DataFrame:
    """applies single derivation rule (#611)."""
    missing = [column for column in rule.columns if column not in table.columns]
    if missing:
        raise TabularError(
            f"derived column {rule.name!r} refers to columns absent from the table: {missing}"
        )
    if rule.name in table.columns:
        # with_columns silently overwrites existing column of same name — source values
        # disappear so downstream builds; surface as declaration error. if derivation rule
        # uses its input column as result name(name=dealMonth,
        # columns=[dealYear, dealMonth, dealDay])also caught here.
        raise TabularError(
            f"derived column {rule.name!r} would overwrite an existing column of the same name"
        )
    if rule.kind == "date_parts":
        year, month, day = rule.columns
        composed = (
            pl.col(year).cast(pl.Utf8).str.zfill(4)
            + pl.lit("-")
            + pl.col(month).cast(pl.Utf8).str.zfill(2)
            + pl.lit("-")
            + pl.col(day).cast(pl.Utf8).str.zfill(2)
        )
        result = table.with_columns(composed.str.to_date("%Y-%m-%d", strict=False).alias(rule.name))
        # same as #188: rows where all pieces exist but fail to become date are losses by rule
        # values. if cast, build would have failed; but only derivation rule silently nulls
        # not allowed. rows where pieces already missing are not losses.
        #
        # piece existence must be judged from *original* table. if rule.name is input
        # column name (e.g., name=dealMonth, columns=[dealYear, dealMonth,
        # dealDay]—validator permits declaration) with_columns already overwrote that input column
        # so, in rows with invalid date, piece itself appears null. then
        # "all pieces existed"becomes false and intended loss slips through.
        parts_present = table.select(
            pl.all_horizontal(pl.col(c).is_not_null() for c in rule.columns)
        ).to_series()
        lost = table.filter(parts_present & result.get_column(rule.name).is_null())
        if lost.height:
            raise TabularError(
                f"derived column {rule.name!r} dropped {lost.height} value(s) to null "
                f"(data loss): rows whose {list(rule.columns)} form no valid date"
            )
        return result
    if rule.kind == "join_key":
        # if any key column null, result null(concat_str default behavior) —
        # does not create rows that cannot form join key as empty string.
        composed_key = pl.concat_str(
            [_escape_join_key_part(column) for column in rule.columns],
            separator=JOIN_KEY_SEPARATOR,
        )
        return table.with_columns(composed_key.alias(rule.name))
    raise TabularError(f"unsupported derived column kind: {rule.kind!r}")


def _escape_join_key_part(column: str) -> pl.Expr:
    """escapes single join_key component to avoid separator collision (#611).

    Concatenating separator naively is not injective—``("a|b", "c")`` and ``("a", "b|c")``
    both become ``a|b|c``, so different key tuples merge into same join key. Gold composition
    (``stages/gold/compose.py``) uses this as single equi-join key, so unrelated rows join
    and duplicate-key stats corrupt. First double escape chars, then escape separator to
    recover original components.
    """
    return (
        pl.col(column)
        .cast(pl.Utf8)
        .str.replace_all(JOIN_KEY_ESCAPE, JOIN_KEY_ESCAPE * 2, literal=True)
        .str.replace_all(JOIN_KEY_SEPARATOR, JOIN_KEY_ESCAPE + JOIN_KEY_SEPARATOR, literal=True)
    )

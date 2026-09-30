"""Silver normalization on DuckDB (#46, #869).

Load Bronze records into a DuckDB table and apply only the declared normalization rules.
Builder does not arbitrarily define "clean data" — undeclared transformations are not
performed.

Each declaration is its own step and its own table, in the order Silver has always
applied them::

    raw → null tokens (on the records, before any type is decided) → coalesced →
    renamed → zero-filled → cast → derived

so a failure names the declaration and the column it failed on, as before. The column
types, values and messages are those of the Polars implementation this replaces
(``tabular/duckdb_load.py`` for loading, ``tabular/duckdb_casts.py`` for casts).

Main functions:
    - normalize_table: BronzeArtifact → TableHandle
"""

from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import duckdb

from ...errors import TabularError
from ...spec import ColumnNullTokens, DerivedColumn, JsonValue
from ...tabular.cast_names import TEXT_CASTS, YEAR_MONTH_COMPACT_RE2, YEAR_MONTH_DASHED_RE2
from ...tabular.convert import check_case_fold_collisions
from ...tabular.duckdb_casts import (
    cast_expression,
    register_functions,
    strip_expression,
    text_expression,
    zfill_expression,
)
from ...tabular.duckdb_load import (
    LoadedTable,
    Node,
    TableHandle,
    derive_table,
    load_records,
    node_of,
)
from ...tabular.duckdb_runtime import reserve_row_seq
from ..bronze.models import BronzeArtifact

#: separator for join_key derived columns (#611).
JOIN_KEY_SEPARATOR = "|"

#: escape character used when embedding separator within values (#611).
JOIN_KEY_ESCAPE = "\\"

_NULL_NODE: Node = ("null",)


def open_connection(workdir: Path) -> duckdb.DuckDBPyConnection:
    """A private connection for a Silver built outside a run (library use, tests)."""
    from ...tabular.duckdb_runtime import BuildProfile, connect

    workdir.mkdir(parents=True, exist_ok=True)
    return connect(BuildProfile(), workdir)


def normalize_table(
    bronze: BronzeArtifact,
    *,
    casts: Mapping[str, str] | None = None,
    rename: Mapping[str, str] | None = None,
    derived: Sequence[DerivedColumn] = (),
    read_as: Mapping[str, str] | None = None,
    null_tokens: Sequence[str] = (),
    column_null_tokens: Mapping[str, ColumnNullTokens] | None = None,
    coalesce: Mapping[str, Sequence[str]] | None = None,
    zfill: Mapping[str, int] | None = None,
    connection: duckdb.DuckDBPyConnection | None = None,
    workdir: Path | None = None,
) -> TableHandle:
    """Loads Bronze records into DuckDB and applies only the declared normalization.

    ``connection`` is the source's connection (the caller closes it); without one, a
    private connection is opened. ``workdir`` holds the load file while loading; the
    Bronze staging directory by default.
    """
    workdir = workdir or bronze.staging_dir
    owns_connection = connection is None
    connection = connection or open_connection(workdir)
    register_functions(connection)
    # null_tokens applied *before* the table is typed: the type checks reject
    # heterogeneous columns (#187), so if a public API gives missing as "", a column of
    # 84.5 and "" would stop the build before the declaration takes effect (#613).
    tokens = _null_token_rule(bronze, null_tokens, column_null_tokens or {})

    def records() -> Iterator[dict[str, JsonValue]]:
        for record in bronze.iter_records():
            yield tokens(record) if tokens is not None else record

    try:
        table = load_records(
            connection, records, table="silver_raw", workdir=workdir, read_as=read_as
        )
        if coalesce:
            for target, candidates in _coalesce_order(coalesce):
                table = _advance(
                    connection, table, _apply_coalesce(connection, table, target, candidates)
                )
        if rename:
            table = _apply_rename(table, rename)
        if zfill:
            for column, width in zfill.items():
                table = _advance(connection, table, _apply_zfill(connection, table, column, width))
        if casts:
            _check_year_month(connection, table, casts)
            table = _advance(connection, table, _apply_casts(connection, table, casts))
        for rule in derived:
            table = _advance(connection, table, _apply_derived(connection, table, rule))
        # After every declared transform, so a rename or a coalesce can resolve it
        # (#868); the row ordinal's name is Builder's (ADR 0021 D8).
        check_case_fold_collisions(table.names)
        reserve_row_seq(table.names)
    except BaseException:
        if owns_connection:
            connection.close()
        raise
    return TableHandle(connection, table, workdir, owns_connection=owns_connection)


_STEPS = itertools.count()


def _step_name(step: str) -> str:
    """A table name no earlier step used, so no step reads and replaces one table."""
    return f"silver_{step}_{next(_STEPS)}"


def _advance(
    connection: duckdb.DuckDBPyConnection, old: LoadedTable, new: LoadedTable
) -> LoadedTable:
    """``new`` from here on; the step before it is dropped, so a normalization holds one
    table at a time, not one per declaration."""
    if old.relation != new.relation:
        connection.execute(f"DROP TABLE IF EXISTS {old.relation.sql}")
    return new


def _null_token_rule(
    bronze: BronzeArtifact,
    null_tokens: Sequence[str],
    column_null_tokens: Mapping[str, ColumnNullTokens],
) -> Callable[[dict[str, JsonValue]], dict[str, JsonValue]] | None:
    """The per-record rewrite collecting missing notations as null (#620, #623).

    Missing representation recognized in one column is **global + that column's
    declaration**. Per-column declaration does not overwrite global — if it did, adding
    one token to one column could silently lose global tokens.

    Fails if a declared column doesn't exist in the source (unless ``on_absent:
    ignore``), or holds values but no text: a typo or a wrong type must not silently do
    nothing. Checked in one pass over the records before any is rewritten.
    """
    if not null_tokens and not column_null_tokens:
        return None
    columns: dict[str, None] = {}
    first_value_type: dict[str, str] = {}
    has_text: set[str] = set()
    for record in bronze.iter_records():
        for key, value in record.items():
            columns.setdefault(key, None)
            if key in column_null_tokens and value is not None:
                first_value_type.setdefault(key, type(value).__name__)
                if isinstance(value, str):
                    has_text.add(key)

    # "what is missing in this column" and "must this column always exist" are separate
    # contracts. latter declared separately with on_absent — else marking missing would
    # mean all generations must have that column.
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
    wrong_type = {
        name: first_value_type[name]
        for name in present
        if name in first_value_type and name not in has_text
    }
    if wrong_type:
        raise TabularError(
            f"column_null_tokens declared on non-string columns: {wrong_type}. "
            "Declare read_as so the source values are read as text."
        )

    shared = frozenset(null_tokens)
    per_column = {name: shared | frozenset(column_null_tokens[name].tokens) for name in present}

    def rewrite(record: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return {
            key: (
                None if isinstance(value, str) and value in per_column.get(key, shared) else value
            )
            for key, value in record.items()
        }

    return rewrite


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


def _keep(table: LoadedTable, names: Sequence[str]) -> list[tuple[str, str, Node]]:
    """``(name, sql, node)`` for keeping ``names`` of ``table`` as they are."""
    return [(name, table.column(name), table.nodes[table.names.index(name)]) for name in names]


def _dtype(table: LoadedTable, name: str) -> str:
    return table.dtypes[table.names.index(name)]


def _apply_coalesce(
    connection: duckdb.DuckDBPyConnection,
    table: LoadedTable,
    target: str,
    candidates: tuple[str, ...],
) -> LoadedTable:
    """collects per-generation alias columns into single canonical column (#620)."""
    present = [name for name in candidates if name in table.names]
    if not present:
        raise TabularError(
            f"coalesce target {target!r} found none of its candidates in the source: "
            f"{list(candidates)}"
        )
    # if target name overlaps with non-candidate existing column, silently overwrites it.
    # contract limiting to alias group breaks there.
    if target in table.names and target not in present:
        raise TabularError(
            f"coalesce target {target!r} would overwrite an existing column that is not "
            "one of its candidates"
        )
    # an all-null candidate is typed Null — common shape in mixed-generation snapshots,
    # and fits any type, so it is excluded from the consensus.
    typed = [name for name in present if _dtype(table, name) != "Null"]
    if len({_dtype(table, name) for name in typed}) > 1:
        raise TabularError(
            f"coalesce target {target!r} has candidates of differing dtypes: "
            f"{ {name: _dtype(table, name) for name in present} }. "
            "Declare read_as to read them as one type."
        )
    if len(typed) > 1:
        # if multiple non-null candidates in one row with different values, generation
        # boundary wrong; caught incorrectly. first-wins silently passes wrong value.
        values = ", ".join(table.column(name) for name in typed)
        row = connection.execute(
            f"SELECT count(*) FROM {table.relation.sql} WHERE len(list_distinct([{values}])) > 1"
        ).fetchone()
        conflicts = int(row[0]) if row else 0
        if conflicts:
            # does not include value itself. this message appears in manifest and
            # /builds response, and the PII scan (#441) runs after it — naming which
            # column and how many rows is sufficient to locate the fix.
            raise TabularError(
                f"coalesce target {target!r} has {conflicts} row(s) where candidates "
                f"{present} disagree"
            )
    merged = (
        f"coalesce({', '.join(table.column(name) for name in typed)})"
        if typed
        else "CAST(NULL AS INTEGER)"
    )
    node = table.nodes[table.names.index(typed[0])] if typed else _NULL_NODE
    kept = [name for name in table.names if name not in present]
    return derive_table(
        connection,
        table,
        into=_step_name("coalesced"),
        columns=[*_keep(table, kept), (target, merged, node)],
    )


def _apply_rename(table: LoadedTable, rename: Mapping[str, str]) -> LoadedTable:
    missing = [source for source in rename if source not in table.names]
    if missing:
        raise TabularError(f"declared rename refers to columns absent from the source: {missing}")
    # two source columns merging to one name, or a target that is an existing column,
    # would lose a column; fail in spec terms. (value duplicates are caught by the
    # validator at declaration time.)
    untouched = set(table.names) - set(rename)
    collisions = sorted({target for target in rename.values() if target in untouched})
    if collisions:
        raise TabularError(f"declared rename targets collide with existing columns: {collisions}")
    # Names only: the physical columns are unchanged.
    return dataclasses.replace(table, names=tuple(rename.get(n, n) for n in table.names))


def _apply_zfill(
    connection: duckdb.DuckDBPyConnection, table: LoadedTable, column: str, width: int
) -> LoadedTable:
    """aligns identifier width (#620).

    Same rental station as ``3`` and ``00003`` splits in aggregation. null stays null — if
    filled with ``"00000"``, missing becomes valid identifier and quality metrics count
    different missing values.
    """
    if column not in table.names:
        raise TabularError(f"declared zfill refers to a column absent from the table: {column!r}")
    dtype = _dtype(table, column)
    if dtype == "Null":
        # an all-null column (mixed-generation snapshots, or coalesced all-null aliases)
        # cannot be fixed by read_as, which leaves null alone; zfill leaves nulls as they
        # are, so the column becomes a text column of nulls.
        sql = "CAST(NULL AS VARCHAR)"
    elif dtype != "String":
        raise TabularError(
            f"zfill target {column!r} is {dtype}, not a string; "
            "declare read_as so the leading zeros survive reading"
        )
    else:
        # values longer than declared width fail instead of being truncated. silent
        # truncation corrupts identifiers; contract declares width, longer values are a
        # drift signal.
        quoted = table.column(column)
        row = connection.execute(
            f"SELECT count(*), coalesce(max(length({quoted})), 0) "
            f"FROM {table.relation.sql} WHERE length({quoted}) > ?",
            [width],
        ).fetchone()
        too_long, longest = (int(row[0]), int(row[1])) if row else (0, 0)
        if too_long:
            raise TabularError(
                f"zfill target {column!r} has {too_long} value(s) longer than the declared "
                f"width {width} (longest is {longest} characters)"
            )
        sql = zfill_expression(quoted, width)
    columns = [
        (name, sql, ("str",)) if name == column else _keep(table, [name])[0] for name in table.names
    ]
    return derive_table(connection, table, into=_step_name("zfilled"), columns=columns)


def _check_year_month(
    connection: duckdb.DuckDBPyConnection, table: LoadedTable, casts: Mapping[str, str]
) -> None:
    """pre-reports values that the `year_month` cast will reject (#620).

    The cast itself nulls mismatched values and the audit counts them, but a count alone
    does not say what and why.
    """
    for column, dtype in casts.items():
        if dtype.strip().lower() not in TEXT_CASTS or column not in table.names:
            continue
        text = strip_expression(text_expression(table.column(column), _dtype(table, column)))
        row = connection.execute(
            f"SELECT count(*) FROM {table.relation.sql} WHERE {text} IS NOT NULL "
            f"AND NOT regexp_matches({text}, ?) AND NOT regexp_matches({text}, ?)",
            [YEAR_MONTH_DASHED_RE2, YEAR_MONTH_COMPACT_RE2],
        ).fetchone()
        bad = int(row[0]) if row else 0
        if bad:
            raise TabularError(
                f"year_month cast on {column!r} rejected {bad} value(s); expected YYYY-MM or YYYYMM"
            )


def _apply_casts(
    connection: duckdb.DuckDBPyConnection, table: LoadedTable, casts: Mapping[str, str]
) -> LoadedTable:
    for column in casts:
        if column not in table.names:
            raise ValueError(
                f"Cannot cast missing column: {column!r}. Available columns: {list(table.names)}"
            )
    columns = [
        (
            (
                name,
                cast_expression(table.column(name), _dtype(table, name), casts[name]),
                node_of(casts[name]),
            )
            if name in casts
            else _keep(table, [name])[0]
        )
        for name in table.names
    ]
    cast_table = derive_table(connection, table, into=_step_name("cast"), columns=columns)
    before = _null_counts(connection, table, list(casts))
    after = _null_counts(connection, cast_table, list(casts))
    lost = {column: after[column] - before[column] for column in casts}
    if any(n > 0 for n in lost.values()):
        details = "; ".join(
            f"{column!r}: {n} value(s) -> null" for column, n in lost.items() if n > 0
        )
        raise TabularError(f"declared cast dropped values to null (data loss): {details}")
    return cast_table


def _null_counts(
    connection: duckdb.DuckDBPyConnection, table: LoadedTable, names: Sequence[str]
) -> dict[str, int]:
    counts = ", ".join(f"count(*) - count({table.column(name)})" for name in names)
    row = connection.execute(f"SELECT {counts} FROM {table.relation.sql}").fetchone()
    assert row is not None
    return {name: int(value) for name, value in zip(names, row, strict=True)}


def _apply_derived(
    connection: duckdb.DuckDBPyConnection, table: LoadedTable, rule: DerivedColumn
) -> LoadedTable:
    """applies single derivation rule (#611)."""
    missing = [column for column in rule.columns if column not in table.names]
    if missing:
        raise TabularError(
            f"derived column {rule.name!r} refers to columns absent from the table: {missing}"
        )
    if rule.name in table.names:
        # a derived column would silently replace a source column of the same name —
        # its values would disappear; surface it as a declaration error. (a rule using
        # one of its inputs as its name is caught here too.)
        raise TabularError(
            f"derived column {rule.name!r} would overwrite an existing column of the same name"
        )
    text = {name: text_expression(table.column(name), _dtype(table, name)) for name in rule.columns}
    if rule.kind == "date_parts":
        year, month, day = rule.columns
        composed = (
            f"{zfill_expression(text[year], 4)} || '-' || {zfill_expression(text[month], 2)} "
            f"|| '-' || {zfill_expression(text[day], 2)}"
        )
        sql = f"CAST(try_strptime({composed}, '%Y-%m-%d') AS DATE)"
        derived = derive_table(
            connection,
            table,
            into=_step_name("derived"),
            columns=[*_keep(table, table.names), (rule.name, sql, ("date",))],
        )
        # same as #188: rows where all pieces exist but form no date are losses by the
        # rule. rows where a piece is already missing are not losses.
        present = " AND ".join(f"{table.column(c)} IS NOT NULL" for c in rule.columns)
        row = connection.execute(
            f"SELECT count(*) FROM {table.relation.sql} WHERE {present} AND {sql} IS NULL"
        ).fetchone()
        lost = int(row[0]) if row else 0
        if lost:
            raise TabularError(
                f"derived column {rule.name!r} dropped {lost} value(s) to null "
                f"(data loss): rows whose {list(rule.columns)} form no valid date"
            )
        return derived
    if rule.kind == "join_key":
        # if any key column is null, the key is null — no row gets an empty-string key.
        sql = " || '|' || ".join(_escape_join_key_part(text[c]) for c in rule.columns)
        return derive_table(
            connection,
            table,
            into=_step_name("derived"),
            columns=[*_keep(table, table.names), (rule.name, sql, ("str",))],
        )
    raise TabularError(f"unsupported derived column kind: {rule.kind!r}")


def _escape_join_key_part(text: str) -> str:
    """escapes one join_key component so the separator cannot collide (#611).

    Concatenating naively is not injective — ``("a|b", "c")`` and ``("a", "b|c")`` both
    become ``a|b|c``, so different key tuples would merge into one join key. First double
    the escape character, then escape the separator, so the components can be recovered.
    """
    return f"replace(replace({text}, '\\', '\\\\'), '|', '\\|')"


__all__ = ["JOIN_KEY_ESCAPE", "JOIN_KEY_SEPARATOR", "normalize_table", "open_connection"]

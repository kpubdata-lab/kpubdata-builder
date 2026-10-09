"""Which columns of a query's result are a stored Null column, read as it is.

A column that held no value in any row is stored as Null, and a page of rows reports it
``null``. DuckDB has no such type to give a result: the sandbox's ``NULL AS "name"`` is
an INTEGER there, so the same column came back ``int32`` from a query — two types for
one column, depending on which screen asked.

This walks the validated SQL and finds the result columns that are that stored column
and nothing else: named bare, through ``*``, through a subquery or a CTE, under another
name. Anything computed keeps the type DuckDB gave it — ``coalesce(c, 0)`` is an integer.
Every case this cannot follow answers "not known", never "Null": the result is then what
it was before, DuckDB's type.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

#: One column a query gives: its name, case-folded, and whether it is a stored Null column.
_Output = tuple[str, bool]


class _Unknown(Exception):
    """The query has a shape whose columns cannot be told from its text."""


def _branches(scope: Scope) -> list[Scope]:
    # sqlglot 30.19 renamed ``union_scopes`` (see ``security._set_operation_scopes``).
    found = getattr(scope, "set_operation_scopes", None)
    if found is None:
        found = getattr(scope, "union_scopes", None)
    if found is None:
        raise _Unknown
    return list(found)


class _Lineage:
    def __init__(self, columns: Sequence[str], null_columns: Collection[str]) -> None:
        self._stored = [name.casefold() for name in columns]
        self._null = {name.casefold() for name in null_columns}
        self._known: dict[int, list[_Output]] = {}

    def outputs(self, scope: Scope) -> list[_Output]:
        """The columns ``scope`` gives, in order."""
        known = self._known.get(id(scope))
        if known is None:
            known = self._outputs(scope)
            self._known[id(scope)] = known
        return known

    def _outputs(self, scope: Scope) -> list[_Output]:
        expression = scope.expression
        if isinstance(expression, exp.SetOperation):
            if expression.args.get("by_name"):
                raise _Unknown
            sides = [self.outputs(branch) for branch in _branches(scope)]
            if not sides or any(len(side) != len(sides[0]) for side in sides):
                raise _Unknown
            # A set operation's column is Null only when every branch's is: beside a
            # column with a type, DuckDB gives the result that type.
            return [
                (name, all(side[index][1] for side in sides))
                for index, (name, _null) in enumerate(sides[0])
            ]
        if not isinstance(expression, exp.Select):
            raise _Unknown
        aliases = {
            item.alias.casefold() for item in expression.expressions if isinstance(item, exp.Alias)
        }
        found: list[_Output] = []
        for item in expression.expressions:
            inner = item.this if isinstance(item, exp.Alias) else item
            if isinstance(inner, exp.Star):
                found.extend(self._star(scope, inner, None))
            elif isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star):
                found.extend(self._star(scope, inner.this, inner.table))
            elif isinstance(inner, exp.Column):
                # A name that is also an alias of this SELECT may mean that alias.
                null = inner.name.casefold() not in aliases and self._column(scope, inner)
                found.append((item.alias_or_name.casefold(), null))
            else:
                found.append((item.alias_or_name.casefold(), False))
        return found

    def _sources(self, scope: Scope) -> dict[str, tuple[Any, object]]:
        return {
            name.casefold(): (node, source)
            for name, (node, source) in scope.selected_sources.items()
        }

    def _relation(self, node: Any, source: object) -> list[_Output]:
        """The columns of one relation in a FROM clause."""
        if node.args.get("pivots") or node.alias_column_names:
            raise _Unknown
        if isinstance(source, Scope):
            parent = source.expression.parent
            if isinstance(parent, exp.CTE) and parent.alias_column_names:
                raise _Unknown
            return self.outputs(source)
        if isinstance(source, exp.Table):
            # The validator has let through only the logical dataset table.
            return [(name, name in self._null) for name in self._stored]
        raise _Unknown

    def _star(self, scope: Scope, star: exp.Star, table: str | None) -> list[_Output]:
        if any(star.args.values()):
            raise _Unknown
        sources = self._sources(scope)
        if table:
            pair = sources.get(table.casefold())
            if pair is None:
                raise _Unknown
            return self._relation(*pair)
        # With a join, which columns `*` gives depends on how it is joined (USING,
        # NATURAL, SEMI): not followed.
        if len(sources) != 1 or scope.expression.args.get("joins"):
            raise _Unknown
        return self._relation(*next(iter(sources.values())))

    def _column(self, scope: Scope, column: exp.Column) -> bool:
        if column.args.get("db") or column.args.get("catalog"):
            return False
        sources = self._sources(scope)
        name = column.name.casefold()
        if column.table:
            qualifier = column.table.casefold()
            # `a.b` is also field `b` of a struct column `a`.
            if qualifier in self._stored or qualifier not in sources:
                return False
            pair = sources[qualifier]
        elif len(sources) == 1:
            pair = next(iter(sources.values()))
        else:
            return False
        matches = [null for found, null in self._relation(*pair) if found == name]
        return len(matches) == 1 and matches[0]


def stored_null_outputs(
    canonical_sql: str,
    result_columns: Sequence[str],
    *,
    columns: Sequence[str],
    null_columns: Collection[str],
) -> list[bool] | None:
    """Per result column, whether it is a stored Null column and nothing else.

    Args:
        canonical_sql: The validated SQL (``ValidatedSql.canonical_sql``).
        result_columns: The names DuckDB gave the result, in order.
        columns: The table's columns, in the file's order.
        null_columns: The ones among them stored as Null.

    Returns:
        One flag per result column, or ``None`` when the query cannot be followed or
        what was followed does not match the result DuckDB gave.
    """
    if not null_columns:
        return None
    try:
        root = build_scope(parse_one(canonical_sql, read="duckdb"))
        if root is None:
            return None
        found = _Lineage(columns, null_columns).outputs(root)
    except (_Unknown, SqlglotError, RecursionError):
        return None
    if len(found) != len(result_columns):
        return None
    if any(
        null and name != given.casefold()
        for (name, null), given in zip(found, result_columns, strict=True)
    ):
        return None
    return [null for _name, null in found]


__all__ = ["stored_null_outputs"]

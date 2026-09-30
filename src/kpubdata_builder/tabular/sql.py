"""The one way Builder writes SQL for DuckDB (ADR 0021, #866).

Generated SQL mixes two kinds of input, and each has exactly one path in:

- **Identifiers** (column and relation names, which come from provider data and user
  specs) go through :func:`quote_identifier`. Nothing else quotes an identifier.
- **Values** never enter SQL text. They are bound as parameters: a :class:`Statement`
  carries its SQL and its parameters apart, and :func:`execute` hands both to DuckDB.

A value passed through :func:`quote_identifier` would still be a working query, just a
wrong one, so the rule is by convention and review, not by type: identifiers here,
values as parameters.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb


def quote_identifier(name: str) -> str:
    """``name`` as a DuckDB identifier: double-quoted, with ``"`` doubled.

    Quoting keeps the name's spelling, so a column called ``select`` or ``distance (m)``
    is still just a column. DuckDB still matches names case-insensitively, quoted or
    not: ``Name`` and ``name`` are one column to it.

    Raises:
        ValueError: ``name`` is empty or holds a NUL character, which no identifier can.
    """
    if not name:
        raise ValueError("an identifier cannot be empty")
    if "\x00" in name:
        raise ValueError("an identifier cannot contain a NUL character")
    return '"' + name.replace('"', '""') + '"'


def identifier_list(names: Iterable[str]) -> str:
    """Comma-separated quoted identifiers, for a select or group-by list."""
    return ", ".join(quote_identifier(name) for name in names)


@dataclass(frozen=True)
class Statement:
    """SQL text with ``?`` placeholders, and the values bound to them."""

    sql: str
    params: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        placeholders = _count_placeholders(self.sql)
        if placeholders != len(self.params):
            raise ValueError(
                f"statement has {placeholders} placeholder(s) but {len(self.params)} value(s)"
            )


def _count_placeholders(sql: str) -> int:
    """``?`` outside quoted text — a literal ``'?'`` or an identifier ``"?"`` is not one."""
    count = 0
    quote: str | None = None
    for char in sql:
        if quote is not None:
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
        elif char == "?":
            count += 1
    return count


def execute(
    connection: duckdb.DuckDBPyConnection, statement: Statement
) -> duckdb.DuckDBPyConnection:
    """Run ``statement`` with its values bound, never interpolated."""
    return connection.execute(statement.sql, list(statement.params))


def placeholders(values: Sequence[object]) -> str:
    """``?, ?, ?`` for an ``IN (...)`` list of ``len(values)`` bound values."""
    if not values:
        raise ValueError("an IN list needs at least one value")
    return ", ".join("?" for _ in values)


__all__ = [
    "Statement",
    "execute",
    "identifier_list",
    "placeholders",
    "quote_identifier",
]

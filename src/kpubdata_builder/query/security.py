"""AST-based SQL sandbox for the logical ``dataset`` relation.

The first of three layers (ADR 0021 D6, #874): this validator, the locked DuckDB
connection (``sandbox``) and the child process (``engine``). It parses and writes the
DuckDB dialect, admits one SELECT over ``dataset`` and refuses, besides other relations
and table functions:

- **introspective functions** — settings, variables, the environment, the current
  query, versions, sequences — whatever DuckDB would answer about itself rather than
  about the data;
- **nondeterministic SQL** — random values, UUIDs, the current time, and sampling
  (``USING SAMPLE`` / ``TABLESAMPLE``): a query's result must be the same for the same
  snapshot.

The function lists are not the sandbox boundary on their own — the locked connection
refuses every file, network and configuration access whatever a function asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, build_scope

#: Functions about DuckDB itself rather than the data, enumerated from DuckDB 1.2–1.5's
#: ``duckdb_functions()`` (scalar ones; table functions are refused as relations).
INTROSPECTIVE_FUNCTIONS = frozenset(
    {
        "current_setting",
        "getenv",
        "getvariable",
        "current_query",
        "write_log",
        "sleep_ms",
        "nextval",
        "currval",
        "setseed",
        "stats",
        "error",
        "version",
        "current_version",
        "current_database",
        "current_catalog",
        "current_schema",
        "current_schemas",
        "current_user",
        "session_user",
        "user",
        "txid_current",
        "current_connection_id",
        "current_query_id",
        "current_role",
        "current_transaction_id",
        "in_search_path",
        "pg_conf_load_time",
        "pg_postmaster_start_time",
        "pg_is_other_temp_schema",
        "pg_my_temp_schema",
        "pg_sleep",
        "pg_backend_pid",
        "has_any_column_privilege",
        "has_column_privilege",
        "has_database_privilege",
        "has_foreign_data_wrapper_privilege",
        "has_function_privilege",
        "has_language_privilege",
        "has_schema_privilege",
        "has_sequence_privilege",
        "has_server_privilege",
        "has_table_privilege",
        "has_tablespace_privilege",
        "pg_has_role",
    }
)
#: Functions whose value changes from one run to the next.
NONDETERMINISTIC_FUNCTIONS = frozenset(
    {
        "random",
        "rand",
        "uuid",
        "gen_random_uuid",
        "uuidv4",
        "uuidv7",
        "now",
        "today",
        "current_date",
        "current_time",
        "current_timestamp",
        "current_localtime",
        "current_localtimestamp",
        "localtime",
        "localtimestamp",
        "get_current_time",
        "get_current_timestamp",
        "transaction_timestamp",
    }
)


class UnsafeQueryError(ValueError):
    """The SQL is syntactically invalid or outside the read-only subset."""


@dataclass(frozen=True)
class ValidatedSql:
    canonical_sql: str


def _normalized_identifier(identifier: exp.Identifier) -> str:
    return identifier.name.casefold()


def _reject_unsupported_relations(expression: exp.Expression) -> None:
    """Reject relation-producing nodes other than tables/subqueries/CTEs.

    This is intentionally deny-by-default. DuckDB has many file-reading table functions,
    so a name denylist alone would not be a sufficient sandbox.
    """
    for node in expression.walk():
        if isinstance(node, (exp.Values, exp.Unnest, exp.Lateral)):
            raise UnsafeQueryError("relation type is not allowed")

        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise UnsafeQueryError("table functions are not allowed")


def _function_name(node: exp.Func) -> str:
    return (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).casefold()


def _reject_unsafe_functions(expression: exp.Expression) -> None:
    for node in expression.walk():
        if isinstance(node, exp.TableSample):
            raise UnsafeQueryError("sampling is not allowed: a query must be reproducible")
        if not isinstance(node, exp.Func):
            continue
        name = _function_name(node)
        if name in INTROSPECTIVE_FUNCTIONS:
            raise UnsafeQueryError(f"function {name} is not allowed")
        if name in NONDETERMINISTIC_FUNCTIONS:
            raise UnsafeQueryError(f"function {name} is not allowed: a query must be reproducible")


def _validate_scope(scope: Scope) -> int:
    physical_dataset_refs = 0

    for name in scope.cte_sources:
        if name.casefold() == "dataset":
            raise UnsafeQueryError("CTE alias must not shadow dataset")

    for relation_source in scope.sources.values():
        if isinstance(relation_source, Scope):
            continue
        if not isinstance(relation_source, exp.Table):
            raise UnsafeQueryError("relation type is not allowed")

    for table in scope.tables:
        table_source = scope.sources.get(table.alias_or_name)
        if isinstance(table_source, Scope):
            continue
        if not isinstance(table.this, exp.Identifier):
            raise UnsafeQueryError("table functions are not allowed")
        if table.this.args.get("quoted"):
            raise UnsafeQueryError("quoted table identifiers are not allowed")
        if table.args.get("db") is not None or table.args.get("catalog") is not None:
            raise UnsafeQueryError("qualified tables are not allowed")
        if _normalized_identifier(table.this) != "dataset":
            raise UnsafeQueryError("only the logical dataset table is allowed")
        physical_dataset_refs += 1

    return physical_dataset_refs


def _reachable_dataset_refs(scope: Scope, visited: set[int] | None = None) -> int:
    """Count physical dataset relations reachable from the final result graph."""
    seen = visited if visited is not None else set()
    identity = id(scope)
    if identity in seen:
        return 0
    seen.add(identity)

    count = 0
    for _node, source in scope.selected_sources.values():
        if isinstance(source, Scope):
            count += _reachable_dataset_refs(source, seen)
        elif isinstance(source, exp.Table):
            count += 1
    for child in (*scope.subquery_scopes, *_set_operation_scopes(scope)):
        count += _reachable_dataset_refs(child, seen)
    return count


def _set_operation_scopes(scope: Scope) -> list[Scope]:
    """Return the branch scopes of a set operation, across sqlglot versions.

    sqlglot 30.19 renamed ``Scope.union_scopes`` to ``Scope.set_operation_scopes``
    (UNION/INTERSECT/EXCEPT all being set operations). The declared range allows
    both, and reading the old name on a new sqlglot raises AttributeError from
    inside the guard that decides whether a query may run — so this walker would
    fail open-ended rather than count the branches it exists to count. Prefer the
    current name and fall back to the old one.
    """
    branches = getattr(scope, "set_operation_scopes", None)
    if branches is None:
        branches = getattr(scope, "union_scopes", None)
    if branches is None:  # pragma: no cover - neither name exists
        raise UnsafeQueryError("unsupported sqlglot version: cannot inspect set operations")
    return list(branches)


def validate_read_only_sql(sql: str) -> ValidatedSql:
    """Parse and validate one SELECT/CTE query, returning canonical SQL.

    The returned SQL, rather than the original text, is the only form handed to
    DuckDB — written in the DuckDB dialect it was parsed in. This removes comments and
    reduces parser differential surface.
    """
    if not sql or len(sql.encode("utf-8")) > 64 * 1024:
        raise UnsafeQueryError("SQL must be a non-empty string up to 64 KiB")
    try:
        statements = [statement for statement in parse(sql, read="duckdb") if statement is not None]
    except ParseError as exc:
        raise UnsafeQueryError("invalid SQL syntax") from exc
    if len(statements) != 1:
        raise UnsafeQueryError("exactly one SQL statement is required")

    expression = statements[0]
    if not isinstance(expression, exp.Query):
        raise UnsafeQueryError("only SELECT queries are allowed")

    for with_node in expression.find_all(exp.With):
        if with_node.args.get("recursive"):
            raise UnsafeQueryError("recursive CTEs are not allowed")
        for cte in with_node.expressions:
            if cte.alias.casefold() == "dataset":
                raise UnsafeQueryError("CTE alias must not shadow dataset")

    _reject_unsupported_relations(cast(exp.Expression, expression))
    _reject_unsafe_functions(cast(exp.Expression, expression))
    root_scope = build_scope(expression)
    if root_scope is None:
        raise UnsafeQueryError("query scope could not be validated")

    for scope in root_scope.traverse():
        _validate_scope(scope)
    if _reachable_dataset_refs(root_scope) == 0:
        raise UnsafeQueryError("query must reference the logical dataset table")

    canonical = expression.copy()
    for node in canonical.walk():
        node.comments = []
    return ValidatedSql(canonical.sql(dialect="duckdb", pretty=False))


__all__ = [
    "INTROSPECTIVE_FUNCTIONS",
    "NONDETERMINISTIC_FUNCTIONS",
    "UnsafeQueryError",
    "ValidatedSql",
    "validate_read_only_sql",
]

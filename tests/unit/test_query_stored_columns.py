"""A stored Null column is one type in a page of rows and in a query's result.

Every case runs on a real sandbox: what the SQL's text is read to say is checked against
the columns DuckDB gives, and a column called Null must be one DuckDB typed INTEGER and
filled with nothing.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.query.engine import QueryEngine
from kpubdata_builder.query.result import to_wire
from kpubdata_builder.query.rows import parse_rows_plan, read_page
from kpubdata_builder.query.sandbox import open_sandbox
from kpubdata_builder.query.security import validate_read_only_sql
from kpubdata_builder.query.stored_columns import stored_null_outputs
from tests.support.polars_bridge import handle_from_frame


@pytest.fixture(scope="module")
def table(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`flag` and `Mark` held no value in any row; `empty` is an integer column of nulls."""
    root = tmp_path_factory.mktemp("stored-null")
    frame = pl.DataFrame(
        {
            "id": ["a", "b", "c"],
            "flag": pl.Series([None, None, None], dtype=pl.Null),
            "v": [1, 2, 3],
            "empty": pl.Series([None, None, None], dtype=pl.Int64),
            "Mark": pl.Series([None, None, None], dtype=pl.Null),
        }
    )
    handle = handle_from_frame(frame, workdir=root / "work")
    path = root / "table.parquet"
    handle.write_parquet(path)
    handle.close()
    return path


#: The table's columns as a page of rows reports them.
_ALL = ("string", "null", "int64", "int64", "null")


def _null_outputs(table: Path, sql: str) -> tuple[list[bool] | None, list[str], list[str]]:
    canonical = validate_read_only_sql(sql).canonical_sql
    with open_sandbox(str(table)) as sandbox:
        relation = sandbox.connection.sql(canonical)
        found = stored_null_outputs(
            canonical,
            relation.columns,
            columns=sandbox.columns,
            null_columns=[n for n, dtype in sandbox.dtypes.items() if dtype == "Null"],
        )
        stored = None if found is None else ["Null" if flag else None for flag in found]
        wire = to_wire(relation, stored=stored)
        if found is not None:
            for index, flag in enumerate(found):
                if flag:
                    # Sound: only a column DuckDB typed as the sandbox's NULL, with no value.
                    assert str(relation.types[index]) == "INTEGER"
                    assert all(row[index] is None for row in wire.raw_rows)
    return found, list(relation.columns), [str(m["logical_type"]) for m in wire.column_meta]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM dataset", [*_ALL]),
        ("FROM dataset", [*_ALL]),
        ("SELECT flag FROM dataset", ["null"]),
        ("SELECT FLAG, mark FROM dataset", ["null", "null"]),
        ('SELECT "flag" AS f, v FROM dataset WHERE flag IS NULL ORDER BY v', ["null", "int64"]),
        (
            "SELECT d.flag AS f, d.* FROM dataset AS d",
            ["null", *_ALL],
        ),
        ("SELECT DISTINCT flag FROM dataset", ["null"]),
        ("SELECT flag, COUNT(*) AS n FROM dataset GROUP BY flag", ["null", "int64"]),
        ("SELECT f FROM (SELECT flag AS f, v FROM dataset) AS s WHERE v > 1", ["null"]),
        ("SELECT * FROM (SELECT * FROM dataset) AS s", [*_ALL]),
        ("WITH c AS (SELECT id, flag FROM dataset) SELECT c.flag, id FROM c", ["null", "string"]),
        ("WITH c AS (SELECT * FROM dataset), e AS (SELECT * FROM c) SELECT Mark FROM e", ["null"]),
        ("SELECT flag FROM dataset UNION ALL SELECT Mark FROM dataset", ["null"]),
        ("SELECT a.flag FROM dataset AS a JOIN dataset AS b ON a.id = b.id", ["null"]),
    ],
)
def test_a_stored_null_column_read_as_it_is_stays_null(
    table: Path, sql: str, expected: list[str]
) -> None:
    _found, _columns, types = _null_outputs(table, sql)

    assert types == expected


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # Computed: the type is DuckDB's.
        ("SELECT flag + 1 AS flag FROM dataset", ["int32"]),
        ("SELECT COALESCE(flag, 0) AS flag FROM dataset", ["int32"]),
        ("SELECT CAST(flag AS VARCHAR) AS flag FROM dataset", ["string"]),
        ("SELECT MAX(flag) AS flag FROM dataset", ["int32"]),
        ("SELECT (flag) FROM dataset", ["int32"]),
        # A column that has a type and happens to hold no value is that type.
        ("SELECT empty FROM dataset", ["int64"]),
        ("SELECT empty AS flag FROM dataset", ["int64"]),
        ("SELECT v AS flag FROM dataset", ["int64"]),
        # Beside a typed column, a set operation takes the type.
        ("SELECT flag FROM dataset UNION ALL SELECT v FROM dataset", ["int64"]),
        ("SELECT v AS flag FROM dataset UNION ALL SELECT flag FROM dataset", ["int64"]),
        # A subquery that gives the name to something else.
        ("SELECT flag FROM (SELECT v AS flag FROM dataset) AS s", ["int64"]),
        ("WITH c AS (SELECT id AS flag FROM dataset) SELECT flag FROM c", ["string"]),
        ("SELECT s.flag FROM (SELECT v + 0 AS flag FROM dataset) AS s", ["int64"]),
    ],
)
def test_anything_else_keeps_the_type_duckdb_gave(
    table: Path, sql: str, expected: list[str]
) -> None:
    _found, _columns, types = _null_outputs(table, sql)

    assert types == expected


@pytest.mark.parametrize(
    "sql",
    [
        # Shapes that are not followed: nothing is called Null, and the result is as before.
        "SELECT * EXCLUDE (id) FROM dataset",
        "SELECT * REPLACE (v AS flag) FROM dataset",
        "SELECT * RENAME (flag AS other) FROM dataset",
        "SELECT COLUMNS('flag|Mark') FROM dataset",
        "SELECT * FROM dataset AS a JOIN (SELECT id FROM dataset) AS b USING (id)",
        "SELECT * FROM dataset AS a NATURAL JOIN dataset AS b",
        "SELECT x FROM (SELECT flag, v FROM dataset) AS s(y, x)",
        "WITH c(v, flag) AS (SELECT flag, v FROM dataset) SELECT flag FROM c",
        "SELECT flag FROM dataset UNION ALL BY NAME SELECT v AS flag FROM dataset",
        "SELECT v AS flag, flag AS g FROM dataset",
    ],
)
def test_a_shape_that_is_not_followed_calls_nothing_null_wrongly(table: Path, sql: str) -> None:
    """The check inside `_null_outputs` is the subject: a Null flag is never on a value."""
    found, columns, types = _null_outputs(table, sql)

    assert len(types) == len(columns)
    if found is None:
        assert "null" not in types


def test_without_a_null_column_the_sql_is_not_read() -> None:
    assert stored_null_outputs("not sql at all (", ["a"], columns=["a"], null_columns=[]) is None


def test_sql_that_cannot_be_read_answers_not_known() -> None:
    assert stored_null_outputs("SELECT (", ["a"], columns=["a"], null_columns=["a"]) is None


def test_a_result_that_does_not_match_the_text_answers_not_known() -> None:
    sql = "SELECT a FROM dataset"

    assert stored_null_outputs(sql, ["a", "b"], columns=["a"], null_columns=["a"]) is None
    assert stored_null_outputs(sql, ["other"], columns=["a"], null_columns=["a"]) is None
    assert stored_null_outputs(sql, ["A"], columns=["a"], null_columns=["a"]) == [True]


def test_a_query_and_a_page_of_rows_say_the_same_type(table: Path) -> None:
    """End to end, through the child process: the two answers Studio shows side by side."""
    page, _count, _more = read_page(str(table), parse_rows_plan({"page_size": 2}))
    canonical = validate_read_only_sql("SELECT * FROM dataset").canonical_sql
    result = QueryEngine(timeout_seconds=60).execute(table, canonical, limit=2)

    assert [dict(meta) for meta in result.column_meta] == page.column_meta
    assert {m["name"]: m["logical_type"] for m in page.column_meta}["flag"] == "null"

"""Security contract for #504 read-only SQL."""

from __future__ import annotations

import pytest

from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM dataset",
        "SELECT city, COUNT(*) AS n FROM dataset GROUP BY city ORDER BY n DESC",
        "WITH filtered AS (SELECT * FROM dataset WHERE value > 0) SELECT * FROM filtered",
        "WITH a AS (SELECT * FROM dataset), b AS (SELECT * FROM a) SELECT * FROM b",
        "SELECT * FROM (SELECT * FROM dataset WHERE value > 0) AS nested",
    ],
)
def test_allows_queries_derived_from_dataset(sql: str) -> None:
    assert validate_read_only_sql(sql).canonical_sql


@pytest.mark.parametrize(
    "sql",
    [
        "WITH dataset AS (SELECT 1 AS x) SELECT * FROM dataset",
        'WITH "dataset" AS (SELECT 1 AS x) SELECT * FROM "dataset"',
        "WITH RECURSIVE x AS (SELECT * FROM dataset) SELECT * FROM x",
        "SELECT * FROM other",
        "SELECT * FROM main.dataset",
        'SELECT * FROM "dataset"',
        "SELECT 1",
        "WITH unused AS (SELECT * FROM dataset) SELECT 1",
        "SELECT * FROM (VALUES (1)) AS x",
        "SELECT * FROM UNNEST([1, 2])",
        "SELECT * FROM read_csv('data.csv')",
        "SELECT * FROM ReAd_ParQuEt /* disguised */ ('data.parquet')",
        "SELECT * FROM dataset JOIN read_json('x.json') AS x ON true",
        "SELECT * FROM dataset; SELECT * FROM dataset",
        "INSERT INTO dataset VALUES (1)",
        "UPDATE dataset SET value = 1",
        "DELETE FROM dataset",
        "CREATE TABLE other AS SELECT * FROM dataset",
        "DROP TABLE dataset",
        "ALTER TABLE dataset ADD COLUMN x INT",
        "COPY dataset TO 'x.csv'",
        "ATTACH 'x.db' AS x",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET threads = 4",
        "PRAGMA version",
    ],
)
def test_rejects_unsafe_or_unbound_relations(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql(sql)


def test_canonical_sql_removes_comments() -> None:
    result = validate_read_only_sql("SELECT /* untrusted */ * FROM dataset")
    assert "untrusted" not in result.canonical_sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT city FROM dataset UNION SELECT city FROM dataset",
        "SELECT city FROM dataset UNION ALL SELECT city FROM dataset",
        "SELECT city FROM dataset INTERSECT SELECT city FROM dataset",
        "SELECT city FROM dataset EXCEPT SELECT city FROM dataset",
        "WITH a AS (SELECT city FROM dataset UNION SELECT city FROM dataset) SELECT * FROM a",
        "SELECT * FROM (SELECT city FROM dataset UNION SELECT city FROM dataset) AS u",
    ],
)
def test_allows_set_operations_over_dataset(sql: str) -> None:
    """Set operations are also queries derived from dataset (#504)."""
    assert validate_read_only_sql(sql).canonical_sql


def test_set_operation_branches_are_counted_not_skipped() -> None:
    # If the branch cannot be traced, the "must reference dataset" rule cannot pass — this query
    # references dataset only within the branch.
    assert validate_read_only_sql(
        "SELECT * FROM (SELECT city FROM dataset UNION SELECT city FROM dataset) AS u"
    ).canonical_sql


def test_set_operation_over_a_non_dataset_table_is_still_rejected() -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql("SELECT city FROM other UNION SELECT city FROM other")

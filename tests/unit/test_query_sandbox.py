"""The query sandbox (#874, ADR 0021 D6): validator, locked connection, child process.

Each layer is tested on its own: the locked DuckDB connection refuses what the
validator would never let through, and the validator refuses it before any connection.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import duckdb
import polars as pl
import pytest

from kpubdata_builder.query.engine import QueryEngine
from kpubdata_builder.query.result import to_wire
from kpubdata_builder.query.sandbox import DATASET, open_sandbox
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.tabular.duckdb_runtime import BuildProfile
from kpubdata_builder.tabular.polars_bridge import handle_from_frame


def _table(tmp_path: Path, frame: pl.DataFrame, name: str = "table.parquet") -> Path:
    handle = handle_from_frame(frame, workdir=tmp_path / "work")
    path = tmp_path / name
    handle.write_parquet(path)
    handle.close()
    return path


@pytest.fixture
def table(tmp_path: Path) -> Path:
    return _table(tmp_path, pl.DataFrame({"id": ["1", "2"], "v": [1, 2]}))


# ------------------------------------------------------------- layer 2: locked connection


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('{other}')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM '{other}'",
        "SELECT * FROM read_csv('https://example.com/data.csv')",
        "COPY (SELECT 1) TO '{tmp}/leak.csv'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SET enable_external_access = true",
        "RESET enable_external_access",
        "SET allowed_paths = ['/']",
    ],
)
def test_the_connection_refuses_files_network_and_settings(
    table: Path, tmp_path: Path, sql: str
) -> None:
    """Negative: even SQL the validator would never pass is refused by the connection."""
    other = _table(tmp_path, pl.DataFrame({"secret": ["x"]}), "other.parquet")

    with open_sandbox(str(table)) as sandbox, pytest.raises(duckdb.Error):
        sandbox.connection.execute(sql.format(other=other, tmp=tmp_path)).fetchall()

    assert not (tmp_path / "leak.csv").exists()


def test_the_connection_is_locked_and_limited(table: Path) -> None:
    profile = BuildProfile(memory_limit="128MB", threads=1, max_temp_directory_size="16MB")

    with open_sandbox(str(table), profile=profile) as sandbox:
        settings = dict(
            sandbox.connection.execute(
                "SELECT name, value FROM duckdb_settings() WHERE name IN "
                "('enable_external_access', 'lock_configuration', 'threads', "
                "'autoload_known_extensions', 'autoinstall_known_extensions', 'TimeZone')"
            ).fetchall()
        )
        assert sandbox.connection.sql(f"SELECT count(*) FROM {DATASET}").fetchone() == (2,)

    assert settings == {
        "enable_external_access": "false",
        "lock_configuration": "true",
        "threads": "1",
        "autoload_known_extensions": "false",
        "autoinstall_known_extensions": "false",
        "TimeZone": "UTC",
    }


def test_a_query_spill_stops_at_the_quota(table: Path) -> None:
    """A sort past its buffer memory spills, and past the quota fails — alone."""
    profile = BuildProfile(memory_limit="64MB", threads=1, max_temp_directory_size="4MB")

    with (
        open_sandbox(str(table), profile=profile) as sandbox,
        pytest.raises(duckdb.OutOfMemoryException),
    ):
        sandbox.connection.execute(
            "SELECT max(rn) FROM (SELECT row_number() OVER (ORDER BY md5(i::VARCHAR)) "
            "AS rn FROM range(3000000) t(i))"
        ).fetchall()


def test_the_view_gives_builder_dtypes_back(tmp_path: Path) -> None:
    frame = pl.DataFrame(
        {
            "big": pl.Series([2**70, None], dtype=pl.Int128),
            "took": [dt.timedelta(seconds=90), None],
            "at": pl.Series([dt.datetime(2024, 1, 1, 9), None]).dt.replace_time_zone("Asia/Seoul"),
        }
    )
    path = _table(tmp_path, frame)

    with open_sandbox(str(path)) as sandbox:
        result = to_wire(sandbox.connection.sql(f"SELECT * FROM {DATASET}"))

    assert [m["logical_type"] for m in result.column_meta] == ["int128", "duration", "datetime"]
    assert result.rows[0] == {
        "big": "1180591620717411303424",
        "took": "0:01:30",
        # Instants are sent in UTC (#874): 09:00 in Seoul is 00:00 UTC.
        "at": "2024-01-01T00:00:00+00:00",
    }


def test_a_table_sql_cannot_name_is_refused_but_still_paged(tmp_path: Path) -> None:
    """An empty column name has no SQL spelling: user SQL is refused, rows still read."""
    from kpubdata_builder.query.rows import RowsPlan, read_page

    path = _table(tmp_path, pl.DataFrame({"": ["blank"], "v": [1]}))

    with open_sandbox(str(path)) as sandbox:
        assert sandbox.dataset_refused is not None
        with pytest.raises(duckdb.Error):
            sandbox.connection.execute(f"SELECT * FROM {DATASET}").fetchall()
    page, count, more = read_page(str(path), RowsPlan(offset=0, page_size=10))

    assert page.rows == [{"": "blank", "v": 1}]
    assert (count, more) == (1, False)


# -------------------------------------------------------------------- layer 1: validator


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_parquet('/other/table.parquet')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM '/etc/passwd'",
        "COPY dataset TO '/tmp/x.csv'",
        "ATTACH '/tmp/x.db' AS x",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA version",
        "SET enable_external_access = true",
        "RESET ALL",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM dataset, pragma_version()",
        "SELECT current_setting('home_directory') FROM dataset",
        "SELECT getvariable('x') FROM dataset",
        "SELECT version() FROM dataset",
        "SELECT current_query() FROM dataset",
        "SELECT nextval('s') FROM dataset",
        "SELECT random() FROM dataset",
        "SELECT uuid() FROM dataset",
        "SELECT now() FROM dataset",
        "SELECT current_date FROM dataset",
        "SELECT * FROM dataset USING SAMPLE 10%",
        "SELECT * FROM dataset TABLESAMPLE (10 PERCENT)",
    ],
)
def test_the_validator_refuses_before_any_connection(sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql(sql)


def test_the_validator_writes_the_duckdb_dialect() -> None:
    assert (
        validate_read_only_sql("SELECT v::INT, DATE '2024-01-01' FROM dataset").canonical_sql
        == "SELECT CAST(v AS INT), CAST('2024-01-01' AS DATE) FROM dataset"
    )


# --------------------------------------------------------------------- end to end


def test_no_raw_path_leaves_the_child(table: Path) -> None:
    """Negative: a failing query answers ok: False, never DuckDB's message with a path."""
    from kpubdata_builder.query.engine import QueryExecutionError

    with pytest.raises(QueryExecutionError) as caught:
        QueryEngine(timeout_seconds=30).execute(
            table, "SELECT no_such_column FROM dataset", limit=1
        )

    assert str(table) not in str(caught.value)
    assert "query execution failed" in str(caught.value)

"""The inputs from the review of the Polars → DuckDB cutover (#876, PR #964).

Each case is the reviewer's input, run against what Polars did before. A malformed file
is refused rather than read short; a client never sees a server path; a filter that the
query can run is not refused by the check in front of it.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import time
import tracemalloc
from pathlib import Path

import duckdb
import polars as pl
import pytest

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.tabular_ingest import parse_tabular_bytes
from kpubdata_builder.query.rows import RowFilter, RowsPlan, check_plan, read_page, table_dtypes
from kpubdata_builder.tabular.builder_kv import NO_COLUMNS
from kpubdata_builder.tabular.duckdb_load import load_parquet
from tests.support.polars_bridge import handle_from_frame

# ------------------------------------------------------- 1. an unclosed quote is refused


@pytest.mark.parametrize(
    ("content", "line"),
    [
        (b'a,b\n1,2\n"abc,x\n3,4\n5,6\n', 3),  # returned one row, three dropped
        (b'a,b\n1,"2', 2),  # returned {a: 1, b: None}
        (b'a,b\n"abc,x\n', 2),  # returned no rows
    ],
)
def test_a_quote_never_closed_is_refused_with_its_line(content: bytes, line: int) -> None:
    with pytest.raises(IngestionError) as raised:
        parse_tabular_bytes(content, format="csv")

    assert str(raised.value) == (
        f"failed to parse csv content: a quoted field starting on line {line} is never closed"
    )


def test_a_quoted_field_may_span_lines_and_lines_may_end_differently() -> None:
    content = b'a,b\r\n"x\ny",1\n2,"he said ""hi"""\r3,\n'

    assert parse_tabular_bytes(content, format="csv") == (
        {"a": "x\ny", "b": "1"},
        {"a": "2", "b": 'he said "hi"'},
        {"a": "3", "b": None},
    )


def test_mixed_line_endings_read_as_polars_read_them() -> None:
    assert parse_tabular_bytes(b"a,b\n1,2\r\n3,4\r\n", format="csv") == (
        {"a": 1, "b": 2},
        {"a": 3, "b": 4},
    )


# ------------------------------------------------- 2. a byte-order mark before quotes


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ('﻿"id","name"\n"1","kim"\n', ({"id": 1, "name": "kim"},)),
        ('﻿"a,b",c\n1,2\n', ({"a,b": 1, "c": 2},)),
    ],
)
def test_a_byte_order_mark_does_not_reach_a_quoted_header(
    content: str, expected: tuple[dict[str, object], ...]
) -> None:
    assert parse_tabular_bytes(content.encode(), format="csv") == expected


# --------------------------------------------- 3. no server path in an ingestion error


@pytest.mark.parametrize(
    ("content", "format_", "message"),
    [
        (b"a,b\n1,2,3\n", "csv", "failed to parse csv content: line 2 has 3 fields, the header 2"),
        (
            b"not parquet at all",
            "parquet",
            "failed to parse parquet content: not a Parquet file Builder can read",
        ),
        *(
            (
                content,
                "csv",
                "failed to parse csv content: a value does not fit the type its column was "
                "read as (an integer beyond 64 bits, or a number written in digits other "
                "than 0-9)",
            )
            for content in (
                b"a\n99999999999999999999999\n",
                "a\n١٢٣\n".encode(),  # Arabic-Indic digits: Polars refused them as well
                "a\n１２３\n".encode(),  # full-width digits
                "a\n١.٥\n".encode(),
            )
        ),
    ],
)
def test_an_ingestion_error_names_no_path_or_sql(
    content: bytes, format_: str, message: str
) -> None:
    with pytest.raises(IngestionError) as raised:
        parse_tabular_bytes(content, format=format_)

    assert str(raised.value) == message
    assert "/" not in str(raised.value).replace("<name>_duplicated_<n>", "")


# ------------------------------------------- 4. filters on a zoned datetime or duration


@pytest.fixture
def temporal_table(tmp_path: Path) -> str:
    frame = pl.DataFrame(
        {
            "at": pl.Series(
                [dt.datetime(2024, 1, 1, 9), dt.datetime(2024, 1, 2, 9)]
            ).dt.replace_time_zone("Asia/Seoul"),
            "took": [dt.timedelta(seconds=90), dt.timedelta(minutes=5)],
        }
    )
    handle = handle_from_frame(frame, workdir=tmp_path / "w")
    path = tmp_path / "t.parquet"
    handle.write_parquet(path)
    handle.close()
    return str(path)


@pytest.mark.parametrize(
    "row_filter",
    [
        RowFilter("at", "eq", "2024-01-01T09:00:00+09:00"),
        RowFilter("at", "in", values=("2024-01-01T00:00:00+00:00",)),
        RowFilter("took", "eq", "00:01:30"),
        RowFilter("took", "lt", "00:02:00"),
    ],
)
def test_a_filter_the_query_runs_passes_the_check(
    temporal_table: str, row_filter: RowFilter
) -> None:
    plan = RowsPlan(offset=0, page_size=10, filters=(row_filter,))

    check_plan(plan, table_dtypes(temporal_table))
    page, count, _ = read_page(temporal_table, plan)

    assert count == 1
    assert len(page.rows) == 1


def test_a_filter_value_that_does_not_fit_is_still_refused(temporal_table: str) -> None:
    plan = RowsPlan(offset=0, page_size=10, filters=(RowFilter("at", "eq", "not a time"),))

    with pytest.raises(ValueError, match="is not a valid Datetime"):
        check_plan(plan, table_dtypes(temporal_table))


# ------------------------------------------------------------ the non-blocking items


def test_a_renamed_duplicate_that_takes_a_header_name_is_refused() -> None:
    """``a,a,a_duplicated_0``: one of two columns was lost; Polars refused it."""
    with pytest.raises(IngestionError, match="the header names a column twice"):
        parse_tabular_bytes(b"a,a,a_duplicated_0\n1,2,3\n", format="csv")


# ------------------------------------------- the reader's time and memory are linear
# (the second review of f042f03: a record was parsed again from its start on every line
# with a quote, and a regular expression over a quoted field remembered every character)


@pytest.mark.parametrize(
    "content",
    [
        # a valid cell over 4,000 lines, each with a doubled quote: 10 seconds before
        b'a,b\n"' + b'he said ""hi"" today\n' * 4_000 + b'",1\n',
        # a quote never closed over 8,000 lines: 5 seconds before it was refused
        b'a\n"' + b'""\n' * 8_000,
    ],
    ids=["multi-line-cell", "never-closed"],
)
def test_a_quoted_field_over_many_lines_is_read_in_linear_time(content: bytes) -> None:
    started = time.perf_counter()
    with contextlib.suppress(IngestionError):  # the second is refused; only time is checked
        parse_tabular_bytes(content, format="csv")

    assert time.perf_counter() - started < 1.0


def test_a_multi_line_cell_is_read_whole() -> None:
    content = b'a,b\n"' + b'he said ""hi"" today\n' * 4_000 + b'",1\n'

    (row,) = parse_tabular_bytes(content, format="csv")

    assert row == {"a": 'he said "hi" today\n' * 4_000, "b": 1}


def test_a_large_quoted_field_takes_memory_in_proportion_to_its_size() -> None:
    content = b'a\n"' + b"x" * 5_000_000 + b'"\n'
    tracemalloc.start()
    try:
        (row,) = parse_tabular_bytes(content, format="csv")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert len(row["a"]) == 5_000_000
    # 25 MB now; 1.3 GB in the regular expression alone before
    assert peak < 100_000_000


def test_a_header_too_long_for_the_csv_module_is_read() -> None:
    """A first record over 131,072 characters raised ``csv.Error`` outside the handler."""
    name = "h" * 200_000
    assert parse_tabular_bytes(f"{name}\n1\n".encode(), format="csv") == ({name: 1},)


def test_a_line_over_two_megabytes_is_read() -> None:
    value = "x" * (3 * 1024 * 1024)
    assert parse_tabular_bytes(f"a\n{value}\n".encode(), format="csv") == ({"a": value},)


def test_blank_lines_before_the_header_are_skipped() -> None:
    assert parse_tabular_bytes(b"\n\na,b\n1,2\n", format="csv") == ({"a": 1, "b": 2},)


def _parquet(tmp_path: Path, select: str) -> bytes:
    path = tmp_path / "upload.parquet"
    with duckdb.connect(":memory:") as connection:
        connection.execute(f"COPY ({select}) TO '{path}' (FORMAT parquet)")
    return path.read_bytes()


def test_a_uuid_column_is_read_as_its_text(tmp_path: Path) -> None:
    content = _parquet(tmp_path, "SELECT uuid '5f0e7c1e-1b2c-4d3e-8f90-123456789abc' AS u")

    assert parse_tabular_bytes(content, format="parquet") == (
        {"u": "5f0e7c1e-1b2c-4d3e-8f90-123456789abc"},
    )


@pytest.mark.parametrize(
    "select",
    [
        "SELECT [TIMESTAMPTZ '2024-01-01 09:00:00+09'] AS v",
        "SELECT {'a': [TIMESTAMPTZ '2024-01-01 09:00:00+09']} AS v",
        "SELECT {'u': uuid '5f0e7c1e-1b2c-4d3e-8f90-123456789abc'} AS v",
    ],
)
def test_a_nested_instant_or_uuid_is_refused_by_name(tmp_path: Path, select: str) -> None:
    with pytest.raises(IngestionError, match="column 'v' holds zoned timestamps or UUIDs"):
        parse_tabular_bytes(_parquet(tmp_path, select), format="parquet")


def test_a_struct_field_named_like_a_type_is_not_refused(tmp_path: Path) -> None:
    content = _parquet(tmp_path, "SELECT {'UUID': 1} AS v")

    assert parse_tabular_bytes(content, format="parquet") == ({"v": {"UUID": 1}},)


@pytest.mark.parametrize("name", ["file_row_number", "FILE_ROW_NUMBER"])
def test_a_column_named_file_row_number_loads_in_order(tmp_path: Path, name: str) -> None:
    handle = handle_from_frame(pl.DataFrame({name: [5, 6, 7], "v": [1, 2, 3]}), workdir=tmp_path)
    path = tmp_path / "t.parquet"
    handle.write_parquet(path)
    handle.close()

    with duckdb.connect(":memory:") as connection:
        table = load_parquet(connection, path, table="t")
        rows = connection.execute(f"SELECT * FROM {table.relation.sql}").fetchall()

    assert table.names == (name, "v")
    assert [row[1:] for row in rows] == [(5, 1), (6, 2), (7, 3)]
    assert [row[0] for row in rows] == [0, 1, 2]


def test_the_sandbox_shows_a_table_without_columns_as_one(tmp_path: Path) -> None:
    handle = handle_from_frame(pl.DataFrame(), workdir=tmp_path)
    path = tmp_path / "t.parquet"
    handle.write_parquet(path)
    handle.close()

    page, count, _ = read_page(str(path), RowsPlan(offset=0, page_size=10))

    assert page.columns == []
    assert page.column_meta == []
    assert all(row == {} for row in page.rows)
    assert NO_COLUMNS not in str(page.rows)

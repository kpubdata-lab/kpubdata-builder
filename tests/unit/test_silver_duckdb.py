"""Silver on DuckDB: handles, reserved names, readback and the rest of the #891 review."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import polars as pl
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.spec import DerivedColumn, JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact
from kpubdata_builder.stages.silver.build import build_silver_dataset
from kpubdata_builder.stages.silver.normalize import normalize_table
from kpubdata_builder.tabular.duckdb_load import (
    TableClosedError,
    TableHandle,
    load_parquet,
    load_records,
)
from kpubdata_builder.tabular.duckdb_runtime import ReservedColumnError
from kpubdata_builder.tabular.sql import quote_literal
from tests.support.builder_parquet import (
    builder_dtypes,
    parse_dtype,
    read_builder_parquet,
)
from tests.support.polars_convert import records_to_dataframe

from .test_duckdb_load import CASES, _same


def _bronze(records: list[dict[str, Any]], tmp_path: Path) -> BronzeArtifact:
    return BronzeArtifact.from_records("p.d", records, staging_dir=tmp_path / "bronze")


# ------------------------------------------------------------------ handles


def test_a_closed_table_says_so(tmp_path: Path) -> None:
    silver = build_silver_dataset(_bronze([{"v": 1}], tmp_path))

    silver.close()

    assert silver.table.closed
    with pytest.raises(TableClosedError, match="closed"):
        silver.table.rows(limit=1)
    with pytest.raises(TableClosedError):
        silver.table.schema()


def test_a_private_connection_is_closed_with_its_table(tmp_path: Path) -> None:
    """Library use: the handle that opened a connection closes it (no leak)."""
    with build_silver_dataset(_bronze([{"v": 1}], tmp_path)) as silver:
        connection = silver.table._connection
        assert connection is not None
        assert silver.table.rows(limit=1) == ({"v": 1},)

    with pytest.raises(duckdb.ConnectionException):
        connection.execute("SELECT 1")


def test_a_shared_connection_stays_open(tmp_path: Path) -> None:
    """A caller's connection belongs to the caller: closing the table leaves it open."""
    connection = duckdb.connect()
    table = normalize_table(_bronze([{"v": 1}], tmp_path), connection=connection)

    table.close()

    assert connection.execute("SELECT 1").fetchone() == (1,)


def test_the_connection_is_not_a_public_attribute(tmp_path: Path) -> None:
    table = normalize_table(_bronze([{"v": 1}], tmp_path))

    assert not hasattr(table, "connection")
    assert "open" in repr(table)
    table.close()


def test_rows_at_reads_in_one_query_and_refuses_out_of_range(tmp_path: Path) -> None:
    table = normalize_table(_bronze([{"v": n} for n in range(10)], tmp_path))

    assert table.rows_at([7, 2, 7]) == ({"v": 7}, {"v": 2}, {"v": 7})
    with pytest.raises(IndexError):
        table.rows_at([10])
    table.close()


def test_earlier_steps_are_dropped(tmp_path: Path) -> None:
    connection = duckdb.connect()
    normalize_table(
        _bronze([{"a": "1", "b": None}], tmp_path),
        connection=connection,
        coalesce={"x": ("a", "b")},
        casts={"x": "int"},
    )

    tables = [r[0] for r in connection.execute("SHOW TABLES").fetchall()]

    assert len(tables) == 1, tables


# ------------------------------------------------------------------ reserved names


@pytest.mark.parametrize(
    "declaration",
    [
        {"rename": {"a": "_kpubdata_row_seq"}},
        {"rename": {"a": "_KPUBDATA_ROW_SEQ"}},
        {"coalesce": {"_kpubdata_row_seq": ("a",)}},
        {"derived": (DerivedColumn(name="_kpubdata_row_seq", kind="join_key", columns=("a",)),)},
    ],
    ids=["rename", "rename upper", "coalesce target", "derived"],
)
def test_declarations_cannot_name_the_row_ordinal(
    tmp_path: Path, declaration: dict[str, Any]
) -> None:
    """Negative: the ordinal's name is Builder's, however a column arrives at it."""
    with pytest.raises(ReservedColumnError):
        normalize_table(_bronze([{"a": "x"}], tmp_path), **declaration)


# ------------------------------------------------------------------ year_month


def test_year_month_accepts_a_year_in_other_digits_as_polars_did(tmp_path: Path) -> None:
    """Polars' ``\\d`` is Unicode; RE2's is ASCII — the patterns use ``\\p{Nd}``."""
    from tests.support.polars_helpers import cast_columns

    records: list[dict[str, JsonValue]] = [{"ym": "２０２４-01"}, {"ym": "２０２４07"}]
    expected = cast_columns(pl.DataFrame(records), {"ym": "year_month"})

    table = normalize_table(_bronze(records, tmp_path), casts={"ym": "year_month"})

    assert [r["ym"] for r in table.rows(limit=5)] == expected.get_column("ym").to_list()
    table.close()


def test_year_month_still_refuses_a_bad_month(tmp_path: Path) -> None:
    with pytest.raises(TabularError, match="year_month"):
        normalize_table(_bronze([{"ym": "2024-13"}], tmp_path), casts={"ym": "year_month"})


# ------------------------------------------------------------------ Parquet


@pytest.mark.parametrize("name", sorted(CASES))
def test_persisted_parquet_reads_back_as_polars_wrote_it(tmp_path: Path, name: str) -> None:
    """Every record set, written by DuckDB, read back with its Builder dtypes — by
    Builder's own reader (``load_parquet``) and by the Polars one the tests keep."""
    records = CASES[name]
    expected = records_to_dataframe([dict(r) for r in records])
    connection = duckdb.connect()
    loaded = load_records(connection, lambda: iter(records), table="raw", workdir=tmp_path)
    handle = TableHandle(connection, loaded, tmp_path)
    path = tmp_path / "table.parquet"

    handle.write_parquet(path)
    back = TableHandle(connection, load_parquet(connection, path, table="back"), tmp_path)

    assert (back.table.names, back.table.dtypes) == (loaded.names, loaded.dtypes)
    assert back.table.row_count == len(records)
    assert _same(list(back.iter_rows()), list(handle.iter_rows()))
    if not expected.width:
        # A table without columns holds a placeholder no Builder reader shows (#876).
        return
    frame = read_builder_parquet(path)
    assert frame.schema == expected.schema
    assert _same(frame.to_dicts(), expected.to_dicts())
    assert builder_dtypes(path) == dict(zip(loaded.names, loaded.dtypes, strict=True))


def test_int128_extremes_are_written_exactly(tmp_path: Path) -> None:
    """``abs`` of the smallest HUGEINT overflows; the decimal check does not use it."""
    extremes = [{"v": -(2**127)}, {"v": 2**127 - 1}, {"v": 10**37}]
    connection = duckdb.connect()
    loaded = load_records(connection, lambda: iter(extremes), table="raw", workdir=tmp_path)
    handle = TableHandle(connection, loaded, tmp_path)
    path = tmp_path / "t.parquet"

    handle.write_parquet(path)

    frame = read_builder_parquet(path)
    assert frame.schema["v"] == pl.Int128()
    assert frame.get_column("v").to_list() == [-(2**127), 2**127 - 1, 10**37]


@pytest.mark.parametrize(
    "dtype",
    [
        "Null",
        "Int128",
        "Decimal(precision=38, scale=2)",
        "Datetime(time_unit='us', time_zone=None)",
        "Datetime(time_unit='us', time_zone='Asia/Seoul')",
        "Duration(time_unit='us')",
        "List(List(Int64))",
        "Struct({'a b': Int64, \"it's\": String, 'x': List(Null)})",
        "Struct({})",
    ],
)
def test_builder_dtype_strings_parse_back(dtype: str) -> None:
    assert str(parse_dtype(dtype)) == dtype


@pytest.mark.parametrize("name", ["it's", "back\\slash", "a space", "한글 경로"])
def test_quote_literal_round_trips_a_copy_target(tmp_path: Path, name: str) -> None:
    connection = duckdb.connect()
    target = tmp_path / name / "t.parquet"
    target.parent.mkdir()

    connection.execute(f"COPY (SELECT 1 AS v) TO {quote_literal(str(target))} (FORMAT PARQUET)")

    assert pl.read_parquet(target).get_column("v").to_list() == [1]
    with pytest.raises(ValueError):
        quote_literal("a\x00b")


def test_a_frame_keeps_128_bit_integers_as_numbers(tmp_path: Path) -> None:
    """#872: Polars spills Int128 as binary; the bridge loads it as HUGEINT, exactly, so
    SQL compares it as a number (composition loads its sides this way)."""
    from tests.support.polars_bridge import handle_from_frame, to_polars

    frame = pl.DataFrame({"v": [2**70, None, -(2**100)]}, schema={"v": pl.Int128})
    table = handle_from_frame(frame, workdir=tmp_path)

    (row,) = table.fetch(
        f"SELECT typeof(c0), count(*) FILTER (WHERE c0 > ?) FROM {table.table.relation.sql} "
        "GROUP BY 1",
        [0],
    )
    assert row == ("HUGEINT", 1)
    assert table.rows(limit=3) == ({"v": 2**70}, {"v": None}, {"v": -(2**100)})
    table.cache.clear()
    assert to_polars(table).equals(frame)

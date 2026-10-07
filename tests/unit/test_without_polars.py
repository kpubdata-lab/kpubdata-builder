"""Builder without Polars (#876): nothing under ``kpubdata_builder`` imports it, and the
pieces that replaced it give what the Polars engine gave — checked against that engine,
which the tests keep (``tests/support``)."""

from __future__ import annotations

import datetime as dt
import io
import math
import subprocess
import sys
import zoneinfo
from decimal import Decimal
from pathlib import Path

import duckdb
import polars as pl
import pytest

from kpubdata_builder.ingestion.errors import IngestionError
from kpubdata_builder.ingestion.tabular_ingest import parse_tabular_bytes
from kpubdata_builder.query.rows import table_dtypes, typed_literal
from kpubdata_builder.stages.gold.split import key_text
from kpubdata_builder.tabular.builder_kv import NO_COLUMNS
from kpubdata_builder.tabular.dtypes import is_nested, is_numeric, is_temporal, scalar_sql_type
from kpubdata_builder.tabular.duckdb_load import (
    TableHandle,
    canonical,
    load_parquet,
    load_records,
    node_from_dtype,
    parquet_columns,
)


def test_no_builder_module_imports_polars() -> None:
    """Every module of the package imports, and Polars is never loaded."""
    script = (
        "import importlib, pkgutil, sys\n"
        "import kpubdata_builder\n"
        "for module in pkgutil.walk_packages(kpubdata_builder.__path__, 'kpubdata_builder.'):\n"
        "    importlib.import_module(module.name)\n"
        "assert 'polars' not in sys.modules, 'polars was imported'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ------------------------------------------------------------------ CSV


def _polars_records(text: str) -> list[dict[str, object]] | str:
    try:
        frame = pl.read_csv(io.BytesIO(text.encode()), infer_schema_length=None)
    except Exception:  # Polars raises several error types for a bad file
        return "error"
    return frame.to_dicts()


def _records(text: str) -> list[dict[str, object]] | str:
    try:
        return [dict(r) for r in parse_tabular_bytes(text.encode(), format="csv")]
    except IngestionError:
        return "error"


def _same(left: object, right: object) -> bool:
    if isinstance(left, float) and isinstance(right, float) and math.isnan(left):
        return math.isnan(right)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(_same, left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return list(left) == list(right) and all(_same(left[k], right[k]) for k in left)
    return type(left) is type(right) and left == right


CSV_CASES = {
    "quoted numbers are numbers": 'a,b\n"1",2\n"3",4\n',
    "quoted empty is text, unquoted empty is null": 'a,b\n"",x\n,y\n',
    "a plus sign is text": "a\n+1\n2\n",
    "leading zeros are dropped": "a\n007\n-0\n",
    "booleans in any case": "a\nTrue\nfalse\nTRUE\n",
    "booleans and numbers are text": "a\ntrue\n1\n",
    "integers and floats are floats": "a\n1\n2.5\n",
    "float spellings": "a\n1.\n.5\n1e3\ninf\nNaN\n-inf\n+2.5\n1E5\n",
    "Inf is text": "a\nInf\n",
    "an integer beyond 64 bits fails": "a\n9223372036854775808\n",
    "64-bit extremes": "a\n9223372036854775807\n-9223372036854775808\n",
    "spaces keep text": "a\n 1\n2 \n",
    "a column without values is text": "a,b\n,1\n,2\n",
    "repeated names": "a,a,b,a\n1,2,3,4\n",
    "an empty name": ",b\n1,2\n",
    "a short row is padded": "a,b,c\n1,2\n",
    "a long row fails": "a,b\n1,2,3\n",
    "CRLF": "a,b\r\n1,x\r\n2,y\r\n",
    "BOM": "﻿a,b\n1,2\n",
    "a newline in quotes": 'a,b\n"x\ny",1\n',
    "an escaped quote": 'a\n"he said ""hi"""\n',
    "a quote inside an unquoted field": 'a\nab"c\n',
    "header only": "a,b\n",
    "no final newline": "a\n1",
    "dates stay text": "a\n2020-01-01\n",
    "a quoted comma": 'a\n"1,5"\n',
    "an integer and a quoted empty": 'a\n1\n""\n',
    "Korean": "이름,값\n홍길동,1\n",
}


@pytest.mark.parametrize("name", sorted(CSV_CASES))
def test_csv_records_are_the_ones_polars_read(name: str) -> None:
    text = CSV_CASES[name]
    assert _same(_records(text), _polars_records(text))


def test_csv_read_as_str_keeps_the_text() -> None:
    records = parse_tabular_bytes(b"a,b\n007,1\n", format="csv", read_as={"a": "str"})
    assert records == ({"a": "007", "b": 1},)


def test_csv_blank_line_is_skipped_not_a_row_of_nulls() -> None:
    """The one intended difference: Polars read a blank line as a row of nulls."""
    assert parse_tabular_bytes(b"a,b\n1,2\n\n3,4\n", format="csv") == (
        {"a": 1, "b": 2},
        {"a": 3, "b": 4},
    )


def test_parquet_upload_records(tmp_path: Path) -> None:
    frame = pl.DataFrame(
        {
            "i": [1, None],
            "d": [dt.date(2020, 1, 1), None],
            "dec": pl.Series([Decimal("1.50"), None], dtype=pl.Decimal(10, 2)),
            "tz": pl.Series([dt.datetime(2020, 1, 1), None], dtype=pl.Datetime("us", "UTC")),
            "l": [[1, 2], None],
            "st": [{"a": 1}, None],
        }
    )
    buffer = io.BytesIO()
    frame.write_parquet(buffer)

    records = parse_tabular_bytes(buffer.getvalue(), format="parquet")

    assert records == tuple(frame.to_dicts())


# ------------------------------------------------------------------ split key names


@pytest.mark.parametrize(
    "value",
    [1.0, 2.5, 1e20, 1.5e-7, -0.0, 1e16, 1e15, 0.1, 1 / 3, 1e-5, 1e-4, 123456789.123, 5e-324],
)
def test_float_key_text_is_polars_text(value: float) -> None:
    assert key_text(value, ("float",)) == pl.Series([value]).cast(pl.Utf8).item()


@pytest.mark.parametrize(
    ("value", "node"),
    [
        (True, ("bool",)),
        (Decimal("1.5"), ("decimal", 2)),
        (dt.date(2020, 1, 2), ("date",)),
        (dt.time(1, 2, 3, 4), ("time",)),
        (dt.datetime(2020, 1, 1, 1, 2, 3, 4), ("datetime", None)),
        (
            dt.datetime(2020, 1, 1, 1, 2, 3, tzinfo=zoneinfo.ZoneInfo("Asia/Seoul")),
            ("datetime", "Asia/Seoul"),
        ),
        # An offset with seconds — Seoul's local mean time, +08:27:52 (#876 review).
        (
            dt.datetime(1900, 1, 1, tzinfo=zoneinfo.ZoneInfo("Asia/Seoul")),
            ("datetime", "Asia/Seoul"),
        ),
        (
            dt.datetime(1900, 1, 1, tzinfo=zoneinfo.ZoneInfo("America/New_York")),
            ("datetime", "America/New_York"),
        ),
        (2**70, ("int128",)),
        ("서울", ("str",)),
    ],
)
def test_key_text_is_polars_text(value: object, node: tuple[object, ...]) -> None:
    dtype = {
        ("decimal", 2): pl.Decimal(38, 2),
        ("int128",): pl.Int128(),
    }.get(node)
    expected = pl.Series([value], dtype=dtype).cast(pl.Utf8).item()
    assert key_text(value, node) == expected


def test_key_text_refuses_a_nested_key() -> None:
    with pytest.raises(ValueError, match="scalar"):
        key_text([1], ("list", ("int",)))


# ------------------------------------------------------------------ dtypes


@pytest.mark.parametrize(
    "dtype",
    [
        "Null",
        "Int64",
        "Int128",
        "Float64",
        "String",
        "Binary",
        "Date",
        "Time",
        "Duration(time_unit='us')",
        "Decimal(precision=38, scale=2)",
        "Datetime(time_unit='us', time_zone=None)",
        "Datetime(time_unit='us', time_zone='Asia/Seoul')",
        "List(Struct({'a': Int64, \"it's\": List(String)}))",
        "Struct({})",
        "Struct({\"a', 'b': 'VARCHAR\": String})",
    ],
)
def test_node_from_dtype_inverts_canonical(dtype: str) -> None:
    assert canonical(node_from_dtype(dtype)) == dtype


def test_dtype_kinds() -> None:
    assert is_numeric("Decimal(precision=10, scale=2)") and is_numeric("UInt8")
    assert not is_numeric("String")
    assert is_temporal("Duration(time_unit='us')") and not is_temporal("Int64")
    assert is_nested("List(Int64)") and is_nested("Struct({})") and not is_nested("String")
    assert scalar_sql_type("Datetime(time_unit='us', time_zone='UTC')") == (
        "TIMESTAMP WITH TIME ZONE"
    )
    with pytest.raises(ValueError):
        scalar_sql_type("List(Int64)")


@pytest.mark.parametrize(
    ("value", "dtype", "expected"),
    [
        ("12", "Int64", 12),
        (12, "String", "12"),
        ("2020-01-02", "Date", dt.date(2020, 1, 2)),
        ("1.50", "Decimal(precision=10, scale=2)", Decimal("1.50")),
        ("170141183460469231731687303715884105727", "Int128", 2**127 - 1),
    ],
)
def test_typed_literal(value: object, dtype: str, expected: object) -> None:
    assert typed_literal(value, dtype) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("value", "dtype"),
    [("abc", "Int64"), (300, "Int8"), ("x", "Null"), ([1], "Int64"), ("1", "List(Int64)")],
)
def test_typed_literal_refuses_what_cannot_fit(value: object, dtype: str) -> None:
    with pytest.raises(ValueError):
        typed_literal(value, dtype)  # type: ignore[arg-type]


# ------------------------------------------------------------------ Parquet


def test_a_table_without_columns_round_trips(tmp_path: Path) -> None:
    connection = duckdb.connect()
    loaded = load_records(connection, lambda: iter([{}, {}, {}]), table="t", workdir=tmp_path)
    path = tmp_path / "t.parquet"

    TableHandle(connection, loaded, tmp_path).write_parquet(path)

    assert parquet_columns(connection, path).names == ()
    back = load_parquet(connection, path, table="back")
    assert (back.names, back.row_count) == ((), 3)
    assert table_dtypes(path) == {}
    # The placeholder is what a reader outside Builder sees.
    assert pl.read_parquet(path).columns == [NO_COLUMNS]


def test_a_file_without_builder_dtypes_reads_as_duckdb_types_it(tmp_path: Path) -> None:
    """A Parquet file another writer made: DuckDB's types in Builder's spelling."""
    path = tmp_path / "other.parquet"
    pl.DataFrame(
        {"i": pl.Series([1], dtype=pl.Int16), "n": pl.Series([None], dtype=pl.Null)}
    ).write_parquet(path)

    assert table_dtypes(path) == {"i": "Int16", "n": "Null"}

"""Records load into DuckDB with the dtypes and values Polars gave them (#869).

``records_to_dataframe`` is the reference: for each record set, the loaded table must
report the same column names and dtype strings, and give back the same rows.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import duckdb
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.ingestion.tabular_ingest import parse_tabular_bytes
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.tabular.convert import records_to_dataframe
from kpubdata_builder.tabular.duckdb_load import fetch_rows, load_records

_KST = dt.timezone(dt.timedelta(hours=9))
_FIXTURES = Path(__file__).parents[1] / "fixtures" / "duckdb_parity"

CASES: dict[str, list[dict[str, Any]]] = {
    "int": [{"v": 1}, {"v": 2}],
    "int and float": [{"v": 1}, {"v": 2.5}],
    "float specials": [{"v": math.nan}, {"v": math.inf}, {"v": -math.inf}, {"v": -0.0}],
    "int128": [{"v": 2**63}, {"v": 1}, {"v": -(2**100)}],
    "null only": [{"v": None}, {"v": None}],
    "bool": [{"v": True}, {"v": None}, {"v": False}],
    "str": [{"v": "a"}, {"v": ""}, {"v": "한글"}],
    "missing keys": [{"a": 1}, {"b": "x"}, {"a": 2, "c": True}],
    "list int": [{"v": [1, 2]}, {"v": []}, {"v": None}, {"v": [None]}],
    "list int and float": [{"v": [1]}, {"v": [2.5]}],
    "list null": [{"v": [None]}, {"v": []}],
    "list of lists": [{"v": [[1], [2, 3]]}, {"v": [[]]}],
    "struct": [{"v": {"b": 1, "a": "x"}}, {"v": {"c": 2.5}}, {"v": None}],
    "struct with null field": [{"v": {"x": None}}, {"v": {"x": 1}}],
    "struct quoted field": [{"v": {"a b": 1, 'q"t': "x", "Name": 2}}],
    "struct hostile fields": [
        {"v": {"it's": 1, "a', 'c1': 'VARCHAR": "x", "x'); DROP TABLE raw; --": 2.5}}
    ],
    "list of structs": [{"v": [{"x": 1}, {"y": "a"}]}, {"v": []}],
    "empty struct": [{"v": {}}, {"v": None}],
    "date": [{"v": dt.date(2024, 1, 1)}, {"v": dt.date(1, 1, 1)}],
    "naive datetime": [{"v": dt.datetime(2024, 1, 1, 1, 2, 3, 456789)}],
    "fixed offset": [{"v": dt.datetime(2024, 1, 1, 21, 30, tzinfo=_KST)}],
    "utc": [{"v": dt.datetime(2024, 1, 1, 1, tzinfo=dt.timezone.utc)}],
    "zoned": [{"v": dt.datetime(2024, 3, 1, 9, tzinfo=ZoneInfo("Asia/Seoul"))}],
    "decimal": [{"v": Decimal("12.50")}, {"v": Decimal("0.1")}, {"v": None}],
    "decimal big": [{"v": Decimal("123456789012345678901234567890.5")}],
    "time": [{"v": dt.time(1, 2, 3, 500000)}],
    "duration": [{"v": dt.timedelta(days=1, microseconds=3)}, {"v": None}],
    "binary": [{"v": b"\x00\xffab"}, {"v": None}],
    "tuple": [{"v": (1, 2)}],
    "case-variant names": [{"Name": "a", "name": "b", "NAME": "c"}],
    "odd names": [{"a b": 1, 'q"t': 2, "": 3, "이동거리(m)": 4.5}],
    "no rows": [],
    "empty records": [{}, {}],
}


def _load(tmp_path: Path, records: list[dict[str, Any]], **kwargs: Any) -> tuple[Any, ...]:
    connection = duckdb.connect()
    loaded = load_records(
        connection, lambda: iter(records), table="raw", workdir=tmp_path, **kwargs
    )
    return loaded, fetch_rows(connection, loaded, limit=len(records) + 1)


def _same(a: object, b: object) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, float) and isinstance(b, float):
        return a == b and math.copysign(1, a) == math.copysign(1, b)
    if isinstance(a, dict) and isinstance(b, dict):
        return list(a) == list(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dt.datetime) and isinstance(b, dt.datetime):
        return a == b and str(a.tzinfo) == str(b.tzinfo)
    return type(a) is type(b) and a == b


@pytest.mark.parametrize("name", sorted(CASES))
def test_loaded_table_matches_polars(tmp_path: Path, name: str) -> None:
    records = CASES[name]
    frame = records_to_dataframe([dict(r) for r in records])

    loaded, rows = _load(tmp_path, records)

    assert list(loaded.names) == frame.columns
    assert list(loaded.dtypes) == [str(t) for t in frame.dtypes]
    assert loaded.row_count == len(records)
    expected = frame.to_dicts() if frame.width else [{} for _ in records]
    assert _same(list(rows), expected), (rows, expected)


def test_types_polars_refuses_are_refused_alike(tmp_path: Path) -> None:
    for records in ([{"v": 1}, {"v": "a"}], [{"v": 2**53 + 1}, {"v": 1.5}]):
        with pytest.raises(TabularError) as polars_error:
            records_to_dataframe(records)
        with pytest.raises(TabularError) as duckdb_error:
            _load(tmp_path, records)
        assert str(duckdb_error.value) == str(polars_error.value)


def test_read_as_applies_before_loading(tmp_path: Path) -> None:
    records: list[dict[str, Any]] = [{"code": 123}, {"code": "00123"}, {"code": None}]
    frame = records_to_dataframe(records, read_as={"code": "str"})

    loaded, rows = _load(tmp_path, records, read_as={"code": "str"})

    assert list(loaded.dtypes) == [str(t) for t in frame.dtypes] == ["String"]
    assert list(rows) == frame.to_dicts()


def test_nothing_is_left_in_the_workdir(tmp_path: Path) -> None:
    _load(tmp_path, [{"v": 1}])

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "fixture", ["seoul-apartment-trades.records.json", "seoul-apartment-rent.records.json"]
)
def test_real_provider_rows_match(tmp_path: Path, fixture: str) -> None:
    records: list[dict[str, JsonValue]] = json.loads(
        (_FIXTURES / fixture).read_text(encoding="utf-8")
    )["records"]
    frame = records_to_dataframe(records)

    loaded, rows = _load(tmp_path, records)

    assert list(loaded.dtypes) == [str(t) for t in frame.dtypes]
    assert _same(list(rows), frame.to_dicts())


def test_parsed_upload_rows_match(tmp_path: Path) -> None:
    raw = (_FIXTURES / "seoul-bike-rent-month.jsonl").read_bytes()
    records = [dict(r) for r in parse_tabular_bytes(raw, format="jsonl")]
    frame = records_to_dataframe(records)

    loaded, rows = _load(tmp_path, records)

    assert list(loaded.dtypes) == [str(t) for t in frame.dtypes]
    assert _same(list(rows), frame.to_dicts())


def test_struct_fields_differing_only_in_case_are_refused(tmp_path: Path) -> None:
    """SQL cannot hold them apart (#868); Polars could, so this is a clear error now."""
    with pytest.raises(TabularError, match="struct fields differ only in letter case"):
        _load(tmp_path, [{"v": {"Name": 1, "name": 2}}])


# ------------------------------------------------------------------ summaries


@pytest.mark.parametrize("name", sorted(CASES))
def test_schema_statistics_and_preview_match_polars(tmp_path: Path, name: str) -> None:
    from kpubdata_builder.tabular.duckdb_summary import preview_of, schema_of, statistics_of
    from kpubdata_builder.tabular.polars_engine import (
        compute_statistics,
        generate_preview,
        infer_schema,
    )

    records = CASES[name]
    frame = records_to_dataframe([dict(r) for r in records])
    if not frame.width and records:
        pytest.skip("Polars cannot summarise rows without columns")
    connection = duckdb.connect()
    loaded = load_records(connection, lambda: iter(records), table="raw", workdir=tmp_path)

    assert schema_of(connection, loaded) == infer_schema(frame)
    expected_stats = compute_statistics(frame)
    actual_stats = statistics_of(connection, loaded)
    assert (actual_stats.row_count, actual_stats.null_counts) == (
        expected_stats.row_count,
        expected_stats.null_counts,
    )
    if frame.width:
        assert actual_stats.duplicate_rate == expected_stats.duplicate_rate
    preview = preview_of(connection, loaded, limit=2)
    expected_preview = generate_preview(frame, limit=2)
    assert preview.total_rows == len(records)
    if frame.width:
        assert _same(list(preview.rows), list(expected_preview.rows))


def test_loading_holds_one_record_at_a_time_in_python(tmp_path: Path) -> None:
    """Both passes stream: Python keeps column types, not rows (#622, #869)."""
    import gc
    import tracemalloc

    def records() -> Any:
        return ({"n": n, "pad": "x" * 200, "tags": [n, n + 1]} for n in range(50_000))

    connection = duckdb.connect()
    gc.collect()
    tracemalloc.start()
    try:
        loaded = load_records(connection, records, table="raw", workdir=tmp_path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert loaded.row_count == 50_000
    assert peak < 2_000_000, peak  # the records are ~12 MB


# ------------------------------------------------------------------ review of #888


def test_a_source_column_named_like_the_row_ordinal_is_refused(tmp_path: Path) -> None:
    from kpubdata_builder.tabular.duckdb_runtime import ReservedColumnError

    for name in ("_kpubdata_row_seq", "_KPUBDATA_ROW_SEQ"):
        with pytest.raises(ReservedColumnError):
            _load(tmp_path, [{name: 1, "v": 2}])


def test_the_row_ordinal_is_not_a_column(tmp_path: Path) -> None:
    from kpubdata_builder.tabular.duckdb_summary import schema_of, statistics_of

    connection = duckdb.connect()
    records = [{"v": 1}, {"v": 1}]
    loaded = load_records(connection, lambda: iter(records), table="raw", workdir=tmp_path)

    assert loaded.names == ("v",)
    assert [c.name for c in schema_of(connection, loaded).columns] == ["v"]
    # Two equal rows are duplicates: the ordinal does not make them distinct.
    assert statistics_of(connection, loaded).duplicate_rate == 0.5


def test_source_order_holds_on_a_parallel_connection(tmp_path: Path) -> None:
    """D8: order comes from the ordinal, not from DuckDB's scan order."""
    from kpubdata_builder.tabular.duckdb_runtime import BuildProfile, build_connection

    count = 200_000
    with build_connection(
        tmp_path, "order", profile=BuildProfile(threads=4, memory_limit="512MB")
    ) as connection:
        connection.execute("SET preserve_insertion_order = false")
        loaded = load_records(
            connection,
            lambda: ({"n": n, "pad": f"row-{n}"} for n in range(count)),
            table="raw",
            workdir=tmp_path / "work",
        )
        head = fetch_rows(connection, loaded, limit=5)
        tail = fetch_rows(connection, loaded, limit=5, offset=count - 5)
        middle = fetch_rows(connection, loaded, limit=3, offset=123_456)

    assert [r["n"] for r in head] == [0, 1, 2, 3, 4]
    assert [r["n"] for r in tail] == list(range(count - 5, count))
    assert [r["n"] for r in middle] == [123_456, 123_457, 123_458]


@pytest.mark.parametrize("name", sorted(CASES))
def test_cases_hold_on_a_runtime_connection(tmp_path: Path, name: str) -> None:
    """The same parity on a connection made the way builds make it (limits, UTC, temp)."""
    from kpubdata_builder.tabular.duckdb_runtime import build_connection

    records = CASES[name]
    frame = records_to_dataframe([dict(r) for r in records])
    with build_connection(tmp_path, "cases") as connection:
        loaded = load_records(
            connection, lambda: iter(records), table="raw", workdir=tmp_path / "work"
        )
        rows = fetch_rows(connection, loaded, limit=len(records) + 1)

    assert list(loaded.dtypes) == [str(t) for t in frame.dtypes]
    expected = frame.to_dicts() if frame.width else [{} for _ in records]
    assert _same(list(rows), expected)


@pytest.mark.parametrize(
    ("scenario", "refusal"),
    [
        ("r01_number_and_string", "heterogeneous"),
        ("r02_unsafe_int_and_float", "precision"),
        ("r03_float_after_inference_window", None),
    ],
)
def test_the_loader_agrees_with_the_committed_baseline(
    tmp_path: Path, scenario: str, refusal: str | None
) -> None:
    """Ties the loader to tests/golden/duckdb_parity (#865), not only to live Polars.

    The input is the scenario's own Bronze, read from its golden file, so the two cannot
    drift apart."""
    golden = json.loads(
        (Path(__file__).parents[1] / "golden" / "duckdb_parity" / f"{scenario}.json").read_text(
            encoding="utf-8"
        )
    )
    records = golden["bronze"]["t"]["records"]
    if refusal is not None:
        assert golden["status_code"] != 200
        with pytest.raises(TabularError, match=refusal):
            _load(tmp_path, records)
        return
    loaded, rows = _load(tmp_path, records)
    silver = golden["silver"]["t"]["table"]
    assert list(loaded.names) == silver["columns"]
    assert list(loaded.dtypes) == silver["dtypes"]
    assert [[r[c] for c in loaded.names] for r in rows] == silver["rows"]


# ------------------------------------------------------------------ Polars bridge


@pytest.mark.parametrize("name", sorted(CASES))
def test_the_bridge_gives_the_frame_polars_built(tmp_path: Path, name: str) -> None:
    from kpubdata_builder.tabular.duckdb_load import TableHandle
    from kpubdata_builder.tabular.polars_bridge import to_polars

    records = CASES[name]
    expected = records_to_dataframe([dict(r) for r in records])
    connection = duckdb.connect()
    loaded = load_records(connection, lambda: iter(records), table="raw", workdir=tmp_path)
    handle = TableHandle(connection, loaded, tmp_path)

    frame = to_polars(handle, keep=True)

    assert frame.columns == expected.columns
    assert frame.schema == expected.schema
    if expected.width:
        assert _same(frame.to_dicts(), expected.to_dicts())
    handle.close()
    assert to_polars(handle) is frame  # kept: readable after the table closed

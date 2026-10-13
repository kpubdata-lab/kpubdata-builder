"""The raw Silver file stays out of reach of SQL on a masked sandbox (#1331, #1361 review).

#1361 moved Silver masking into the sandbox's views, but left the raw ``table.parquet``
in ``allowed_paths``: any SQL that reached the locked connection without the validator
could read the original values with ``read_parquet('<path>')``, ``FROM '<path>'`` or
``parquet_metadata`` (whose min/max statistics are values too). The validator was the
only layer between a query and the declared PII.

A masked sandbox now reads the table through a masked Arrow source and may open no file
at all, so each layer holds on its own: the connection refuses the raw path even for SQL
the validator would never pass, and the validator refuses it before any connection.
"""

from __future__ import annotations

import datetime as dt
import decimal
import sys
from collections.abc import Iterator
from multiprocessing import Pipe
from pathlib import Path
from typing import cast

import duckdb
import polars as pl
import pytest

from kpubdata_builder.query.engine import QueryEngine, _query_worker
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.sandbox import (
    DATASET,
    MASKED_COLUMNS_ENV,
    ORDERED_DATASET,
    ROW_ORDER,
    masked_columns_from_env,
    open_sandbox,
)
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.gold.pii import PII_MASK_TOKEN
from tests.support.polars_bridge import handle_from_frame

from .test_gold_pii_masking import _SPEC, _Client

_PHONE = "010-9876-5432"
_AGE = 918273645
_ROWS = 3_000


def _write(tmp_path: Path, frame: pl.DataFrame, name: str = "table.parquet") -> Path:
    handle = handle_from_frame(frame, workdir=tmp_path / f"work-{name}")
    path = tmp_path / name
    handle.write_parquet(path)
    handle.close()
    return path


@pytest.fixture
def table(tmp_path: Path) -> Path:
    """A table whose every row holds the same recognisable phone number and age."""
    return _write(
        tmp_path,
        pl.DataFrame(
            {
                "id": list(range(_ROWS)),
                "phone": [None if i % 5 == 0 else _PHONE for i in range(_ROWS)],
                "age": [_AGE] * _ROWS,
                "city": [f"city {i % 7}" for i in range(_ROWS)],
            }
        ),
    )


@pytest.fixture
def masked(table: Path) -> Iterator[duckdb.DuckDBPyConnection]:
    with open_sandbox(str(table), masked={"phone", "age"}) as sandbox:
        yield sandbox.connection


_RAW_PATH_SQL = [
    "SELECT * FROM read_parquet('{path}')",
    "SELECT * FROM read_parquet(['{path}'])",
    "SELECT * FROM parquet_scan('{path}')",
    "FROM '{path}'",
    "SELECT * FROM '{path}'",
    'SELECT * FROM "{path}"',
    "SELECT * FROM parquet_metadata('{path}')",
    "SELECT * FROM read_parquet('{directory}/*.parquet')",
    "SELECT * FROM read_parquet('{directory}/**')",
    "SELECT * FROM glob('{directory}/*')",
    "SELECT * FROM read_blob('{path}')",
    "SELECT * FROM read_text('{path}')",
    "SELECT * FROM read_csv('{path}')",
]


def _leaks(rows: object) -> bool:
    text = repr(rows)
    return _PHONE in text or str(_AGE) in text


@pytest.mark.parametrize("sql", _RAW_PATH_SQL)
def test_the_masked_connection_cannot_read_the_raw_file(
    table: Path, masked: duckdb.DuckDBPyConnection, sql: str
) -> None:
    """Negative: SQL that skipped the validator still gets no original value."""
    query = sql.format(path=table, directory=table.parent)

    try:
        rows = masked.execute(query).fetchall()
    except duckdb.Error:
        return
    assert not _leaks(rows), f"{query} returned raw values"


@pytest.mark.parametrize("sql", _RAW_PATH_SQL)
def test_the_validator_refuses_the_raw_path(table: Path, sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql(sql.format(path=table, directory=table.parent))


def test_the_masked_connection_reads_no_file_and_no_python_variable(
    table: Path, masked: duckdb.DuckDBPyConnection
) -> None:
    """Python objects in the caller's frame are not tables either (replacement scans)."""
    raw = pl.read_parquet(table)  # noqa: F841 - the variable a replacement scan would find

    with pytest.raises(duckdb.Error):
        masked.execute("SELECT * FROM raw").fetchall()
    settings = dict(
        masked.execute(
            "SELECT name, value FROM duckdb_settings() "
            "WHERE name IN ('allowed_paths', 'python_enable_replacements')"
        ).fetchall()
    )
    assert settings == {"allowed_paths": "[]", "python_enable_replacements": "false"}


def test_every_relation_on_the_masked_connection_is_masked(
    masked: duckdb.DuckDBPyConnection,
) -> None:
    """Whatever a query names — the views or what they read — no original value is in it."""
    relations = [
        str(name)
        for (name,) in masked.execute("SELECT table_name FROM information_schema.tables").fetchall()
    ]

    assert DATASET in relations
    assert ORDERED_DATASET in relations
    for relation in relations:
        rows = masked.execute(f'SELECT * FROM "{relation}"').fetchall()
        assert rows
        assert not _leaks(rows), relation


# ------------------------------------------------------------ queries that scan twice


_CITY_1 = sum(1 for i in range(_ROWS) if i % 7 == 1)
_PHONES = sum(1 for i in range(_ROWS) if i % 5 != 0)
_TWICE = [
    ("SELECT count(*) FROM dataset a JOIN dataset b ON a.id = b.id", (_ROWS,)),
    (
        "WITH t AS MATERIALIZED (SELECT * FROM dataset) "
        "SELECT (SELECT count(*) FROM t), (SELECT count(phone) FROM t)",
        (_ROWS, _PHONES),
    ),
    (
        "SELECT count(*) FROM (SELECT id FROM dataset UNION ALL SELECT id FROM dataset)",
        (2 * _ROWS,),
    ),
    (
        "SELECT count(*) FROM dataset WHERE id IN (SELECT id FROM dataset WHERE city = 'city 1')",
        (_CITY_1,),
    ),
]


@pytest.mark.parametrize(("sql", "expected"), _TWICE)
def test_a_query_that_scans_twice_reads_every_row_each_time(
    masked: duckdb.DuckDBPyConnection, sql: str, expected: tuple[int, ...]
) -> None:
    """The masked source is read afresh by every scan, not consumed by the first."""
    assert masked.execute(sql).fetchall() == [expected]


def test_the_internal_and_user_views_read_together(masked: duckdb.DuckDBPyConnection) -> None:
    rows = masked.execute(
        f"SELECT o.{ROW_ORDER}, d.phone FROM {ORDERED_DATASET} o "
        f"JOIN {DATASET} d ON o._c0 = d.id ORDER BY 1 LIMIT 3"
    ).fetchall()

    assert rows == [(0, None), (1, PII_MASK_TOKEN), (2, PII_MASK_TOKEN)]


def test_the_row_order_is_the_file_order(tmp_path: Path) -> None:
    """Over many row groups, the masked source keeps the file's order."""
    frame = pl.DataFrame({"id": list(range(300_000)), "phone": ["x"] * 300_000})
    path = tmp_path / "groups.parquet"
    frame.write_parquet(path, row_group_size=10_000)

    with open_sandbox(str(path), masked={"phone"}) as sandbox:
        (mismatched,) = sandbox.connection.execute(
            f"SELECT count(*) FROM {ORDERED_DATASET} WHERE {ROW_ORDER} <> _c0"
        ).fetchone() or (None,)
        page = sandbox.connection.execute(
            f"SELECT _c0 FROM {ORDERED_DATASET} WHERE {ROW_ORDER} >= 123456 "
            f"ORDER BY {ROW_ORDER} LIMIT 3"
        ).fetchall()

    assert mismatched == 0
    assert page == [(123456,), (123457,), (123458,)]


def test_the_engine_runs_the_byte_budget_and_the_query_over_one_source(table: Path) -> None:
    """The child plans the query, then streams it (#1332): two reads of the source."""
    result = QueryEngine().execute(
        table,
        "SELECT a.id, a.phone, b.age FROM dataset a JOIN dataset b USING (id) ORDER BY a.id",
        limit=4,
        masked=frozenset({"phone", "age"}),
    )

    assert [r["phone"] for r in result.rows] == [
        None,
        PII_MASK_TOKEN,
        PII_MASK_TOKEN,
        PII_MASK_TOKEN,
    ]
    assert [r["age"] for r in result.rows] == [None] * 4
    assert result.truncated


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is POSIX")
def test_a_masked_query_runs_under_the_child_memory_cap(tmp_path: Path) -> None:
    """pyarrow's allocator and threads fit the cap production runs the child under.

    768 MB is ``docker-compose.prod.app.yml``'s default. Left to its defaults, pyarrow
    reserves a 1 GiB allocator arena and a 64 MiB malloc arena per thread, and even a
    ten-row page of a table with many row groups failed under it.
    """
    path = tmp_path / "table.parquet"
    duckdb.execute(
        "COPY (SELECT i AS id, '010-' || i AS phone, 'shop ' || i AS name "
        f"FROM range(2000000) t(i)) TO '{path}' (FORMAT parquet, ROW_GROUP_SIZE 100000)"
    )
    capped = QueryEngine(timeout_seconds=120, memory_limit_bytes=768 * 1024 * 1024)

    page = capped.execute(path, "SELECT * FROM dataset", limit=10, masked=frozenset({"phone"}))
    counted = capped.execute(
        path,
        "SELECT phone, count(*) AS n FROM dataset GROUP BY phone",
        limit=10,
        masked=frozenset({"phone"}),
    )

    assert [r["phone"] for r in page.rows] == [PII_MASK_TOKEN] * 10
    assert counted.rows == ({"phone": PII_MASK_TOKEN, "n": 2_000_000},)


# ------------------------------------------------------------ types are kept


_BUILDER_FRAME = {
    "phone": ["010-1234-5678", None],
    "text": ["a", None],
    "i64": [2**40, None],
    "big": pl.Series([2**70, None], dtype=pl.Int128),
    "f64": [2.25, None],
    "flag": [True, None],
    "day": [dt.date(2024, 2, 29), None],
    "at": [dt.datetime(2024, 1, 1, 9, 30), None],
    "at_tz": pl.Series([dt.datetime(2024, 1, 1, 9), None]).dt.replace_time_zone("Asia/Seoul"),
    "at_ns": pl.Series([dt.datetime(2024, 1, 1), None], dtype=pl.Datetime("ns")),
    "time": [dt.time(12, 34, 56), None],
    "took": [dt.timedelta(seconds=90), None],
    "amount": pl.Series([decimal.Decimal("12.34"), None], dtype=pl.Decimal(10, 2)),
    "blob": [b"\x00\x01", None],
    "tags": [["a", "b"], None],
    "point": [{"x": 1, "y": "p"}, None],
    "nothing": pl.Series([None, None], dtype=pl.Null),
    "secret_age": [41, None],
}
#: Types only a file Polars wrote itself has (an older snapshot, #874).
_POLARS_ONLY = {
    "i8": pl.Series([1, None], dtype=pl.Int8),
    "u32": pl.Series([3, None], dtype=pl.UInt32),
    "f32": pl.Series([1.5, None], dtype=pl.Float32),
    "kind": pl.Series(["x", None], dtype=pl.Categorical),
}


@pytest.mark.parametrize("writer", ["builder", "polars"])
def test_a_masked_sandbox_keeps_every_column_type_and_value(tmp_path: Path, writer: str) -> None:
    """The unmasked columns read exactly as they do without masking, of the same types."""
    if writer == "builder":
        path = _write(tmp_path, pl.DataFrame(_BUILDER_FRAME))
    else:
        path = tmp_path / "table.parquet"
        frame = {k: v for k, v in _BUILDER_FRAME.items() if k != "big"}
        pl.DataFrame({**frame, **_POLARS_ONLY}).write_parquet(path)

    def read(masked: set[str]) -> tuple[list[tuple[object, ...]], ...]:
        with open_sandbox(str(path), masked=masked) as sandbox:
            connection = sandbox.connection
            return (
                connection.execute(f"DESCRIBE {DATASET}").fetchall(),
                connection.execute(f"DESCRIBE {ORDERED_DATASET}").fetchall(),
                connection.execute(f"SELECT COLUMNS(*)::VARCHAR FROM {DATASET}").fetchall(),
                connection.execute(f"SELECT COLUMNS(*)::VARCHAR FROM {ORDERED_DATASET}").fetchall(),
            )

    plain = read(set())
    masked = read({"phone", "secret_age"})
    names = [str(row[0]) for row in plain[0]]
    kept = [i for i, name in enumerate(names) if name not in ("phone", "secret_age")]

    # Types: both views, every column, masked or not.
    assert masked[0] == plain[0]
    assert masked[1] == plain[1]
    # Values: every column not masked, in both views.
    for view in (2, 3):
        assert [[row[i] for i in kept] for row in masked[view]] == [
            [row[i] for i in kept] for row in plain[view]
        ]
    phone, age = names.index("phone"), names.index("secret_age")
    assert [(row[phone], row[age]) for row in masked[2]] == [(PII_MASK_TOKEN, None), (None, None)]


def test_a_file_the_masked_source_cannot_read_is_refused_not_read_raw(tmp_path: Path) -> None:
    """pyarrow cannot read a 128-bit integer Polars wrote itself: refused, never unmasked."""
    path = tmp_path / "table.parquet"
    pl.DataFrame({"phone": [_PHONE], "big": pl.Series([2**70], dtype=pl.Int128)}).write_parquet(
        path
    )

    with pytest.raises(Exception, match="128|bits"), open_sandbox(str(path), masked={"phone"}):
        pass


# ------------------------------------------------------------ the worker's mask list


def test_a_missing_mask_list_is_refused_not_read_as_none(
    table: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is always told what to mask; not being told is an error (finding 4)."""
    monkeypatch.delenv(MASKED_COLUMNS_ENV, raising=False)

    with pytest.raises(ValueError, match=MASKED_COLUMNS_ENV):
        masked_columns_from_env()
    receiver, sender = Pipe(duplex=False)
    _query_worker(sender, str(table), "SELECT phone FROM dataset", 10, 0)
    assert receiver.recv() == {"ok": False}


class _Recording(QueryService):
    def __init__(self) -> None:
        super().__init__()
        self.masked: list[frozenset[str]] = []

    def execute(
        self,
        table_path: Path,
        canonical_sql: str,
        *,
        limit: int,
        masked: frozenset[str] = frozenset(),
    ) -> QueryResult:
        self.masked.append(masked)
        return super().execute(table_path, canonical_sql, limit=limit, masked=masked)


def test_the_sandbox_is_given_every_declared_column(tmp_path: Path) -> None:
    """The full declaration reaches the sandbox, not the part an earlier read found."""
    engine = _Recording()
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: _Client(pii=("siteTel", "ghost")),
        query_service=engine,
    )
    assert service.build(_SPEC.format(gold=""), run_id="r1").status_code == 200

    response = service.query(
        {
            "dataset_id": "permits.restaurant",
            "run_id": "r1",
            "stage": "silver",
            "sql": "SELECT siteTel FROM dataset",
            "limit": 10,
        },
        principal=Principal("dev"),
    )

    assert response.status_code == 200, response.body
    assert engine.masked == [frozenset({"siteTel", "ghost"})]
    # What the client is told is unchanged: the masked columns the table has.
    assert response.body["masked_columns"] == ["siteTel"]
    assert {r["siteTel"] for r in cast(list[dict[str, JsonValue]], response.body["rows"])} <= {
        PII_MASK_TOKEN,
        None,
    }

"""A Silver query masks declared PII without copying the whole table first (#1331).

``POST /query`` on Silver used to write a masked Parquet copy of the entire table in
the request thread before the query's child process even started, so a ten-row page
cost a full read and a full write of the table, outside the query's timeout, memory
cap, spill quota and cancellation. The masking now lives in the sandbox's ``dataset``
view: the query reads the stored table, DuckDB applies the mask to the rows the query
actually touches, and every cost is the query's own.

The masking itself must not get weaker: each returned row shows the declared column
as Gold masks it, whatever the SQL does with it.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.gold.pii import PII_MASK_TOKEN

from .test_gold_pii_masking import _SPEC, _Client

_DEV = Principal("dev")
_KEY = "localdata.general_restaurant"
_ROWS = 50_000


class _RecordingService(QueryService):
    """A real query service that records which table each query was handed."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[Path] = []
        self.rows_in_table: list[int] = []

    def execute(
        self,
        table_path: Path,
        canonical_sql: str,
        *,
        limit: int,
        masked: frozenset[str] = frozenset(),
    ) -> QueryResult:
        self.tables.append(table_path)
        self.rows_in_table.append(pl.scan_parquet(table_path).select(pl.len()).collect().item())
        return super().execute(table_path, canonical_sql, limit=limit, masked=masked)


def _large_silver(
    tmp_path: Path, engine: QueryService | None = None
) -> tuple[BuilderService, Path]:
    """A built run whose Silver table is replaced by ``_ROWS`` rows of the same columns."""
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda **_: _Client(),
        query_service=engine,
    )
    built = service.build(_SPEC.format(gold=""), run_id="r1")
    assert built.status_code == 200, built.body
    table = tmp_path / "r1" / "silver" / _KEY / "table.parquet"
    small = pl.read_parquet(table)
    large = pl.DataFrame(
        {
            name: (
                [
                    None if i % 7 == 0 else ("" if i % 11 == 0 else f"010-{i:04d}-{i % 10000:04d}")
                    for i in range(_ROWS)
                ]
                if name == "siteTel"
                else [str(i) for i in range(_ROWS)]
                if small.schema[name] == pl.String
                else [None] * _ROWS
            )
            for name in small.columns
        },
        schema=small.schema,
    )
    large.write_parquet(table)
    return service, table


def _query(service: BuilderService, sql: str, limit: int = 10) -> ServiceResponse:
    return service.query(
        {
            "dataset_id": "permits.restaurant",
            "run_id": "r1",
            "stage": "silver",
            "sql": sql,
            "limit": limit,
        },
        principal=_DEV,
    )


def _rows(response: ServiceResponse) -> list[dict[str, JsonValue]]:
    assert response.status_code == 200, response.body
    return cast(list[dict[str, JsonValue]], response.body["rows"])


def test_a_small_page_of_a_large_silver_table_reads_the_stored_table(tmp_path: Path) -> None:
    """The query is handed the stored table, not a full masked copy built beforehand.

    Before #1331 the engine received a fresh copy holding every one of the table's rows,
    written in the request thread: work proportional to the table, not the page.
    """
    engine = _RecordingService()
    service, table = _large_silver(tmp_path, engine)

    rows = _rows(_query(service, "SELECT siteTel FROM dataset LIMIT 10"))

    assert len(rows) == 10
    assert engine.tables == [table], (
        f"the query read a {engine.rows_in_table[0]}-row copy for a 10-row page"
    )
    assert {r["siteTel"] for r in rows} <= {PII_MASK_TOKEN, None}


def test_no_masked_copy_is_written_in_the_request_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing table-sized lands in the temporary directory, and nothing stays there."""
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    (tmp_path / "out").mkdir()
    service, _ = _large_silver(tmp_path / "out")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))
    created: list[str] = []

    class _Recording(tempfile.TemporaryDirectory[str]):
        def __init__(self, suffix: str | None = None, prefix: str | None = None) -> None:
            super().__init__(suffix, prefix)
            created.append(self.name)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", _Recording)

    _rows(_query(service, "SELECT * FROM dataset LIMIT 5"))

    assert created == []
    assert os.listdir(scratch) == []


_EDGE_SQL = [
    "SELECT siteTel FROM dataset",
    "SELECT siteTel FROM dataset ORDER BY siteTel NULLS LAST LIMIT 20 OFFSET 40000",
    "SELECT upper(siteTel) AS siteTel FROM dataset WHERE siteTel IS NOT NULL",
    "SELECT siteTel FROM dataset WHERE siteTel = '' OR siteTel LIKE '010-%' OR siteTel IS NULL",
    "WITH t AS (SELECT * FROM dataset) SELECT siteTel FROM t LIMIT 50",
    "SELECT DISTINCT siteTel FROM dataset",
    "SELECT min(siteTel) AS siteTel FROM dataset UNION ALL SELECT max(siteTel) FROM dataset",
    "SELECT substr(siteTel, 1, 3) AS siteTel FROM dataset LIMIT 30 OFFSET 7",
]


@pytest.mark.parametrize("sql", _EDGE_SQL)
def test_every_returned_row_is_masked(tmp_path: Path, sql: str) -> None:
    """Paging, ordering, filtering, CTEs and aggregates all see only the mask."""
    service, _ = _large_silver(tmp_path)

    response = _query(service, sql, limit=500)
    rows = _rows(response)

    assert rows
    assert response.body["masked_columns"] == ["siteTel"]
    for row in rows:
        assert row["siteTel"] in (None, PII_MASK_TOKEN, PII_MASK_TOKEN.upper(), PII_MASK_TOKEN[:3])
        assert "010-" not in str(row)


def test_masking_keeps_nulls_and_masks_empty_text(tmp_path: Path) -> None:
    """A null stays null and an empty string is masked, as Gold masks them."""
    service, _ = _large_silver(tmp_path)

    rows = _rows(
        _query(service, "SELECT siteTel, count(*) AS n FROM dataset GROUP BY siteTel ORDER BY 1")
    )

    nulls = sum(1 for i in range(_ROWS) if i % 7 == 0)
    assert rows == [
        {"siteTel": PII_MASK_TOKEN, "n": _ROWS - nulls},
        {"siteTel": None, "n": nulls},
    ]


def test_a_filter_on_an_original_value_finds_nothing(tmp_path: Path) -> None:
    service, _ = _large_silver(tmp_path)

    rows = _rows(
        _query(service, "SELECT count(*) AS n FROM dataset WHERE siteTel = '010-0001-0001'")
    )

    assert rows == [{"n": 0}]


def test_concurrent_silver_reads_are_each_masked_and_leave_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    (tmp_path / "out").mkdir()
    service, _ = _large_silver(tmp_path / "out")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))
    results: list[ServiceResponse] = []

    def read() -> None:
        results.append(_query(service, "SELECT siteTel FROM dataset LIMIT 20 OFFSET 100"))

    threads = [threading.Thread(target=read) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for response in results:
        # A third concurrent read may be refused as busy; none is served unmasked.
        if response.status_code == 429:
            continue
        assert {r["siteTel"] for r in _rows(response)} <= {PII_MASK_TOKEN, None}
    assert os.listdir(scratch) == []


def test_a_failed_silver_query_leaves_nothing_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    (tmp_path / "out").mkdir()
    service, _ = _large_silver(tmp_path / "out")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))

    failed = _query(service, "SELECT CAST(siteTel AS INTEGER) AS n FROM dataset")
    refused = _query(service, "SELECT * FROM read_parquet('table.parquet')")

    assert failed.status_code == 400
    assert refused.status_code == 400
    assert os.listdir(scratch) == []


# ------------------------------------------------------------------ the sandbox views


def _sandbox_table(tmp_path: Path) -> Path:
    path = tmp_path / "table.parquet"
    pl.DataFrame(
        {"phone": ["010-1234-5678", None, ""], "age": [41, None, 7], "name": ["a", "b", "c"]}
    ).write_parquet(path)
    return path


def test_both_sandbox_views_mask_the_named_columns(tmp_path: Path) -> None:
    """``dataset`` and the internal ordered view alike: no path to an original value."""
    from kpubdata_builder.query.sandbox import DATASET, ORDERED_DATASET, open_sandbox

    with open_sandbox(str(_sandbox_table(tmp_path)), masked={"phone", "age"}) as sandbox:
        connection = sandbox.connection
        rows = connection.execute(f"SELECT phone, age, name FROM {DATASET}").fetchall()
        ordered = connection.execute(
            f"SELECT {sandbox.alias('phone')}, {sandbox.alias('age')} FROM {ORDERED_DATASET}"
        ).fetchall()
        types = dict(
            connection.execute(
                f"SELECT column_name, column_type FROM (DESCRIBE {DATASET})"
            ).fetchall()
        )

    assert rows == [(PII_MASK_TOKEN, None, "a"), (None, None, "b"), (PII_MASK_TOKEN, None, "c")]
    assert ordered == [(PII_MASK_TOKEN, None), (None, None), (PII_MASK_TOKEN, None)]
    # A masked number is null of its own type, as Gold masks it.
    assert types["age"] == "BIGINT"


def test_the_engine_sets_what_the_child_masks_and_nothing_inherited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A query given no columns masks none, whatever the parent's environment holds."""
    from kpubdata_builder.query.engine import QueryEngine
    from kpubdata_builder.query.sandbox import MASKED_COLUMNS_ENV

    table = _sandbox_table(tmp_path)
    monkeypatch.setenv(MASKED_COLUMNS_ENV, '["name"]')
    engine = QueryEngine()

    plain = engine.execute(table, "SELECT phone, name FROM dataset", limit=10)
    masked = engine.execute(
        table, "SELECT phone, name FROM dataset", limit=10, masked=frozenset({"phone"})
    )

    assert [r["name"] for r in plain.rows] == ["a", "b", "c"]
    assert [r["phone"] for r in masked.rows] == [PII_MASK_TOKEN, None, PII_MASK_TOKEN]
    assert [r["name"] for r in masked.rows] == ["a", "b", "c"]


@pytest.mark.parametrize("raw", ["phone", '{"phone": 1}', "[1]"])
def test_a_malformed_mask_list_is_refused_not_read_as_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    from kpubdata_builder.query.sandbox import MASKED_COLUMNS_ENV, open_sandbox

    monkeypatch.setenv(MASKED_COLUMNS_ENV, raw)
    with pytest.raises(ValueError), open_sandbox(str(_sandbox_table(tmp_path))):
        pass

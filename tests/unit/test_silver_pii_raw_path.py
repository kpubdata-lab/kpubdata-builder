"""SQL naming the raw Silver file is refused before it reaches a sandbox (#1331).

A Silver query's sandbox masks declared PII in its views, but the raw ``table.parquet``
stays on the locked connection's ``allowed_paths`` because the views read it. On that
connection ``read_parquet('<path>')``, ``FROM '<path>'`` or ``parquet_metadata`` would
return original values; the SQL validator is the only layer that keeps such SQL out.
These tests pin that: every raw-path query sent through ``POST /query`` on a Silver
table with masking on is refused, and no original value comes back. Keeping the file
out of the sandbox as well (#1363) was not taken for its memory cost.

They also pin the two smaller fixes from the #1361 review: the sandbox is given every
declared column, and a query child not told what to mask refuses the query.
"""

from __future__ import annotations

from multiprocessing import Pipe
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.query.engine import _query_worker
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.sandbox import MASKED_COLUMNS_ENV, masked_columns_from_env
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service import BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.gold.pii import PII_MASK_TOKEN

from .test_gold_pii_masking import _PHONES, _SPEC, _Client


class _Recording(QueryService):
    """The real engine, recording what each query was told to mask."""

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


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> tuple[BuilderService, _Recording, Path]:
    """A run whose Silver table declares ``siteTel`` (present) and ``ghost`` (absent)."""
    root = tmp_path_factory.mktemp("silver-raw-path")
    engine = _Recording()
    service = BuilderService(
        output_root=root,
        client_factory=lambda **_: _Client(pii=("siteTel", "ghost")),
        query_service=engine,
    )
    assert service.build(_SPEC.format(gold=""), run_id="r1").status_code == 200
    (table,) = sorted((root / "r1" / "silver").rglob("table.parquet"))
    return service, engine, table


def _query(service: BuilderService, sql: str) -> tuple[int, str]:
    response = service.query(
        {
            "dataset_id": "permits.restaurant",
            "run_id": "r1",
            "stage": "silver",
            "sql": sql,
            "limit": 100,
        },
        principal=Principal("dev"),
    )
    return response.status_code, repr(response.body)


#: Each names the raw Silver file — directly, through a table function, a glob, a nested
#: query, or with comments, case and quoting meant to slip past a text match.
_RAW_PATH_SQL = [
    "SELECT * FROM read_parquet('{path}')",
    "SELECT * FROM read_parquet(['{path}'])",
    "SELECT * FROM parquet_scan('{path}')",
    "FROM '{path}'",
    "SELECT * FROM '{path}'",
    'SELECT * FROM "{path}"',
    "SELECT * FROM parquet_metadata('{path}')",
    "SELECT stats_min, stats_max FROM parquet_metadata('{path}')",
    "SELECT * FROM read_parquet('{directory}/*.parquet')",
    "SELECT * FROM read_parquet('{directory}/**')",
    "SELECT * FROM glob('{directory}/*')",
    "SELECT * FROM query('SELECT * FROM read_parquet(''{path}'')')",
    "SELECT * FROM query_table('{path}')",
    "SELECT * FROM READ_PARQUET('{path}')",
    "SELECT * FROM \"read_parquet\"('{path}')",
    "SELECT * FROM/**/read_parquet/**/('{path}')",
    "SELECT * FROM -- comment\n read_parquet('{path}')",
    "/* lead */ SELECT * FROM '{path}'",
    "SELECT * FROM dataset UNION ALL SELECT * FROM read_parquet('{path}')",
    "SELECT * FROM (FROM '{path}')",
    "WITH t AS (SELECT * FROM read_parquet('{path}')) SELECT * FROM t",
    "SELECT (SELECT max(siteTel) FROM read_parquet('{path}'))",
    "SELECT * FROM dataset WHERE siteTel IN (SELECT siteTel FROM '{path}')",
    "SELECT * FROM dataset, '{path}'",
    "SELECT * FROM 'file://{path}'",
    "SELECT * FROM read_blob('{path}')",
    "SELECT * FROM read_text('{path}')",
    "SELECT * FROM read_csv('{path}')",
]


@pytest.mark.parametrize("sql", _RAW_PATH_SQL)
def test_the_validator_refuses_sql_naming_the_raw_file(
    built: tuple[BuilderService, _Recording, Path], sql: str
) -> None:
    _, _, table = built
    with pytest.raises(UnsafeQueryError):
        validate_read_only_sql(sql.format(path=table, directory=table.parent))


@pytest.mark.parametrize("sql", _RAW_PATH_SQL)
def test_a_silver_query_naming_the_raw_file_is_refused_with_no_raw_value(
    built: tuple[BuilderService, _Recording, Path], sql: str
) -> None:
    """Negative: through ``POST /query`` with masking on — refused, no phone number back."""
    service, engine, table = built
    before = len(engine.masked)

    status, body = _query(service, sql.format(path=table, directory=table.parent))

    assert status == 400, body
    assert not any(phone in body for phone in _PHONES), body
    # Refused before the engine ran: no sandbox was opened for it.
    assert len(engine.masked) == before


def test_a_silver_query_over_dataset_is_masked(
    built: tuple[BuilderService, _Recording, Path],
) -> None:
    """Positive control: the same run answers SQL over ``dataset``, masked."""
    service, _, _ = built

    status, body = _query(service, "SELECT siteTel, upper(siteTel) AS up FROM dataset")

    assert status == 200, body
    assert PII_MASK_TOKEN in body
    assert not any(phone in body for phone in _PHONES), body


def test_the_sandbox_is_given_every_declared_column(
    built: tuple[BuilderService, _Recording, Path],
) -> None:
    """The full declaration reaches the sandbox, not the part an earlier read found."""
    service, engine, _ = built
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
    assert engine.masked[-1] == frozenset({"siteTel", "ghost"})
    # What the client is told is unchanged: the masked columns the table has.
    assert response.body["masked_columns"] == ["siteTel"]
    assert {r["siteTel"] for r in cast(list[dict[str, JsonValue]], response.body["rows"])} <= {
        PII_MASK_TOKEN,
        None,
    }


def test_a_missing_mask_list_is_refused_not_read_as_none(
    built: tuple[BuilderService, _Recording, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child is always told what to mask; not being told refuses the query."""
    _, _, table = built
    monkeypatch.delenv(MASKED_COLUMNS_ENV, raising=False)

    with pytest.raises(ValueError, match=MASKED_COLUMNS_ENV):
        masked_columns_from_env()
    receiver, sender = Pipe(duplex=False)
    _query_worker(sender, str(table), "SELECT siteTel FROM dataset", 10, 0)
    assert receiver.recv() == {"ok": False}

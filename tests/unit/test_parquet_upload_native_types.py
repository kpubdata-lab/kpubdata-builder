"""A Parquet upload's native column types build end to end, or are refused by name (#979).

Parquet is the one source format that hands Bronze a ``Decimal``, a ``date``, a
``datetime`` or a ``time`` rather than JSON's text and numbers. These run the real
``BuilderService.build()`` path on such a file: unit tests of one serializer passed while
the build still failed a stage later, in the dataset card.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import duckdb
import pytest
import yaml

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.tabular_ingest import _refuse_untexted_values
from kpubdata_builder.service import BuilderService

_OWNER = "owner"

_EXPORTS = [
    {"kind": "csv", "output_path": "data.csv"},
    {"kind": "jsonl", "output_path": "data.jsonl"},
    {"kind": "markdown", "output_path": "data.md"},
]


def _parquet(tmp_path: Path, select: str) -> bytes:
    path = tmp_path / "upload.parquet"
    with duckdb.connect(":memory:") as connection:
        connection.execute("SET TimeZone = 'Asia/Seoul'")
        connection.execute(f"COPY ({select}) TO '{path}' (FORMAT parquet)")
    return path.read_bytes()


def _build(tmp_path: Path, select: str, schema: dict[str, Any] | None = None) -> Any:
    (tmp_path / "out").mkdir()
    service = BuilderService(
        output_root=tmp_path / "out",
        client_factory=lambda **_: None,
        warehouse_root=tmp_path / "wh",
    )
    upload = service._upload_repository.put(
        _OWNER,
        content=_parquet(tmp_path, select),
        format="parquet",
        encoding="utf-8",
        original_filename="upload.parquet",
    )
    source: dict[str, Any] = {
        "kind": "file",
        "upload_id": upload.upload_id,
        "format": "parquet",
        "alias": "t",
    }
    if schema is not None:
        source["schema"] = schema
    spec = {
        "dataset_id": "native.types",
        "title": "Native types",
        "description": "d",
        "sources": [source],
        "exports": _EXPORTS,
    }
    return service.build(yaml.safe_dump(spec), run_id="r1", owner_id=_OWNER)


def _only(root: Path, name: str) -> str:
    (path,) = root.rglob(name)
    return path.read_text(encoding="utf-8")


_NATIVE = """
    SELECT CAST('2.50' AS DECIMAL(38, 2)) AS amount,
           DATE '2024-01-31' AS day,
           TIMESTAMP '2024-01-31 12:30:00' AS at,
           TIMESTAMPTZ '2024-01-31 12:30:00+09:00' AS instant,
           TIME '01:02:03' AS clock
"""


def test_a_parquet_upload_with_native_types_builds(tmp_path: Path) -> None:
    response = _build(tmp_path, _NATIVE)

    assert response.status_code == 200, response.body
    assert response.body["outcomes"][0]["stages_completed"] == ["bronze", "silver", "gold"]
    run = tmp_path / "out" / "r1"
    # The persisted Bronze snapshot: each value as its one standard text.
    assert json.loads(_only(run, "raw_records.jsonl")) == {
        "amount": "2.50",
        "at": "2024-01-31T12:30:00",
        "clock": "01:02:03",
        "day": "2024-01-31",
        "instant": "2024-01-31T03:30:00+00:00",
    }
    # The dataset card's sample is where the build used to fail after Bronze.
    card = next(p for p in run.rglob("README.md") if "gold" in p.parts).read_text("utf-8")
    assert (
        "| 2.50 | 2024-01-31 | 2024-01-31T12:30:00 | 2024-01-31T03:30:00+00:00 | 01:02:03 |" in card
    )
    assert json.loads(_only(run, "data.jsonl")) == {
        "amount": "2.50",
        "day": "2024-01-31",
        "at": "2024-01-31T12:30:00",
        "instant": "2024-01-31T03:30:00+00:00",
        "clock": "01:02:03",
    }


def test_a_decimal_column_cast_to_int_truncates_toward_zero(tmp_path: Path) -> None:
    select = (
        "SELECT CAST(v AS DECIMAL(38, 2)) AS v FROM (VALUES ('2.50'), ('-0.50'), ('-1.90')) t(v)"
    )

    response = _build(tmp_path, select, {"casts": {"v": "int"}})

    assert response.status_code == 200, response.body
    rows = [json.loads(line) for line in _only(tmp_path / "out" / "r1", "data.jsonl").splitlines()]
    assert rows == [{"v": 2}, {"v": 0}, {"v": -1}]


@pytest.mark.parametrize(
    "select",
    [
        "SELECT 1 AS id, CAST('\\x00\\x01' AS BLOB) AS payload",
        "SELECT 1 AS id, [CAST('\\x00' AS BLOB)] AS payload",
    ],
)
def test_a_binary_column_is_refused_by_name(tmp_path: Path, select: str) -> None:
    response = _build(tmp_path, select)

    assert response.status_code != 200
    outcome = response.body["outcomes"][0]
    assert outcome["error"] == (
        "column 'payload' holds binary values, which a parquet upload cannot carry: "
        "write the column as text or a number before uploading"
    )
    assert outcome["stages_completed"] == []


def test_a_duration_value_is_refused_by_name() -> None:
    # Parquet has no duration type of its own, so which files give one depends on the
    # reader; the refusal is on the value.
    batch: list[dict[str, Any]] = [{"id": 1, "took": None}, {"id": 2, "took": dt.timedelta(1)}]

    with pytest.raises(IngestionError, match="column 'took' holds duration values"):
        _refuse_untexted_values(batch)


def test_an_interval_column_is_refused_by_name_not_read_as_no_rows(tmp_path: Path) -> None:
    # The Polars reader panicked on a DuckDB INTERVAL column and gave no batches, so the
    # build succeeded with an empty table (#876). DuckDB reads it as a duration.
    response = _build(tmp_path, "SELECT 1 AS id, INTERVAL 1 DAY AS payload")

    assert response.status_code != 200
    assert response.body["outcomes"][0]["error"] == (
        "column 'payload' holds duration values, which a parquet upload cannot carry: "
        "write the column as text or a number before uploading"
    )

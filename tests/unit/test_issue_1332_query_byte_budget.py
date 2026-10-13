"""The query worker keeps to its response byte budget while it reads (#1332).

A row limit does not bound bytes: a few rows with huge cells used to be fetched,
encoded and serialised in full before the 8 MiB check ran. These tests drive the
worker in this process and look at how much it read and allocated before refusing.
"""

from __future__ import annotations

import json
import time
import tracemalloc
from pathlib import Path

import pytest

import kpubdata_builder.query.engine as engine_module
import kpubdata_builder.query.result as result_module


class _CaptureConnection:
    def __init__(self) -> None:
        self.payload: object = None
        self.closed = False

    def send(self, payload: object) -> None:
        self.payload = payload

    def close(self) -> None:
        self.closed = True


def _table(tmp_path: Path, values: list[str]) -> Path:
    import polars as pl

    path = tmp_path / "table.parquet"
    pl.DataFrame({"value": values}).write_parquet(path)
    return path


def _run(table: Path, sql: str, limit: int) -> object:
    connection = _CaptureConnection()
    engine_module._query_worker(
        connection,  # type: ignore[arg-type]
        str(table),
        sql,
        limit,
        time.monotonic_ns(),
    )
    assert connection.closed is True
    return connection.payload


def _peak_bytes(table: Path, sql: str, limit: int) -> tuple[object, int]:
    tracemalloc.start()
    try:
        payload = _run(table, sql, limit)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return payload, peak


def test_a_huge_text_cell_is_refused_without_reading_it_into_python(tmp_path: Path) -> None:
    table = _table(tmp_path, ["a"])
    cell = 64 * 1024 * 1024

    payload, peak = _peak_bytes(table, f"SELECT repeat('x', {cell}) AS big FROM dataset", 1)

    assert payload == {"ok": False}
    assert peak < cell // 4, f"peak traced allocation {peak} bytes for a {cell}-byte cell"


def test_a_huge_list_cell_is_refused_without_reading_it_into_python(tmp_path: Path) -> None:
    table = _table(tmp_path, ["a"])

    payload, peak = _peak_bytes(table, "SELECT range(5000000) AS big FROM dataset", 1)

    assert payload == {"ok": False}
    # Five million Python ints alone are well over 100 MB.
    assert peak < 32 * 1024 * 1024, f"peak traced allocation {peak} bytes"


def test_rows_stop_being_encoded_once_the_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _table(tmp_path, ["y" * 600 for _ in range(200)])
    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", 2_000)
    encoded: list[object] = []
    original = result_module.encode_value

    def counting(value: object, encoding: str) -> object:
        encoded.append(value)
        return original(value, encoding)

    monkeypatch.setattr(result_module, "encode_value", counting)

    payload = _run(table, "SELECT value FROM dataset", 200)

    assert payload == {"ok": False}
    assert len(encoded) < 10, f"{len(encoded)} cells encoded for a 2000-byte budget"


def test_the_budget_counts_utf8_bytes_not_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 300 Hangul syllables: 300 characters, 900 UTF-8 bytes.
    table = _table(tmp_path, ["가" * 300])
    accepted = _run(table, "SELECT value FROM dataset", 1)
    assert isinstance(accepted, dict) and accepted["ok"] is True
    wire = {key: accepted[key] for key in accepted if key not in ("ok",)}
    size = len(json.dumps({"ok": True, **wire}, ensure_ascii=False).encode("utf-8"))
    assert size > 900

    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", 600)
    assert _run(table, "SELECT value FROM dataset", 1) == {"ok": False}


def test_a_nested_value_is_measured_as_the_wire_encodes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = _table(tmp_path, ["a"])
    sql = "SELECT [repeat('가', 100), repeat('가', 100)] AS items FROM dataset"
    accepted = _run(table, sql, 1)
    assert isinstance(accepted, dict) and accepted["ok"] is True
    assert accepted["rows"] == [{"items": ["가" * 100, "가" * 100]}]
    size = len(json.dumps(accepted, ensure_ascii=False).encode("utf-8"))

    characters = len(json.dumps(accepted, ensure_ascii=False))
    assert size - characters == 400

    # Timings vary by a digit or two between runs, hence the margin.
    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", size + 32)
    again = _run(table, sql, 1)
    assert isinstance(again, dict) and again["ok"] is True
    # Counted in characters it would fit; counted in UTF-8 bytes it does not.
    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", size - 32)
    assert _run(table, sql, 1) == {"ok": False}


def test_a_result_within_the_budget_keeps_its_rows_and_truncation(tmp_path: Path) -> None:
    table = _table(tmp_path, [str(i) for i in range(5)])

    payload = _run(table, "SELECT value FROM dataset", 3)

    assert isinstance(payload, dict)
    assert payload["ok"] is True
    assert payload["rows"] == [{"value": "0"}, {"value": "1"}, {"value": "2"}]
    assert payload["truncated"] is True
    assert payload["columns"] == ["value"]

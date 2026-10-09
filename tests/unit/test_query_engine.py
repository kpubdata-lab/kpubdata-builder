"""Query child cancellation and bounded result execution tests."""

from __future__ import annotations

import logging
import os
import sys
import time
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

import kpubdata_builder.query.engine as engine_module
from kpubdata_builder.query.engine import QueryEngine, QueryExecutionError, QueryTimeoutError

from .conftest import spawn_timeout_multiplier


def _sleeping_worker(
    connection: Connection,
    table_path: str,
    canonical_sql: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    del canonical_sql, limit, parent_started_ns
    Path(table_path).write_text(str(os.getpid()), encoding="utf-8")
    try:
        time.sleep(60)
    finally:
        connection.close()


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_leaves_child_not_alive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The timeout is reached once the child has published its pid, not seconds later.

    The subject is the child's state after the engine times out; waiting out a real
    timeout only added its length to every run (#1184). Windows spawn imports the test
    module in a fresh interpreter, so the wait for the pid keeps the spawn multiplier.
    """
    pid_file = tmp_path / "child.pid"

    def timed_out(self: Connection, timeout: float | None = 0.0) -> bool:
        deadline = time.monotonic() + 20 * spawn_timeout_multiplier()
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return False

    monkeypatch.setattr(Connection, "poll", timed_out)
    engine = QueryEngine(timeout_seconds=60, worker=_sleeping_worker)

    with pytest.raises(QueryTimeoutError):
        engine.execute(pid_file, "SELECT * FROM dataset", limit=1)

    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _pid_is_alive(pid)


def test_real_engine_executes_canonical_sql_and_hard_limit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": [3, 1, 2]}).write_parquet(table_path)
    sql = "SELECT value FROM dataset ORDER BY value"
    caplog.set_level(logging.INFO, logger=engine_module.__name__)

    result = QueryEngine(timeout_seconds=5 * spawn_timeout_multiplier()).execute(
        table_path,
        sql,
        limit=2,
    )

    assert result.columns == ("value",)
    assert result.rows == ({"value": 1}, {"value": 2})
    assert result.truncated is True
    assert result.execution_ms >= 0
    assert result.startup_ms >= 0
    assert result.engine_execution_ms >= 0
    timing_record = next(record for record in caplog.records if record.event == "query_timing")
    assert timing_record.ipc_serialization_ms >= 0
    assert sql not in timing_record.getMessage()
    assert str(table_path) not in timing_record.getMessage()


def test_real_engine_keeps_precision_and_says_how_columns_are_sent(tmp_path: Path) -> None:
    """#735: /query sends out-of-range integers and Decimals as exact text."""
    from decimal import Decimal

    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame(
        {
            "big": [9007199254740993, 1],
            "small": [9007199254740991, 2],
            "amount": [Decimal("0.10"), Decimal("12.50")],
        },
        schema={"big": pl.Int64, "small": pl.Int64, "amount": pl.Decimal(10, 2)},
    ).write_parquet(table_path)

    result = QueryEngine(timeout_seconds=5 * spawn_timeout_multiplier()).execute(
        table_path, "SELECT big, small, amount FROM dataset ORDER BY small DESC", limit=10
    )

    assert result.rows[0] == {
        "big": "9007199254740993",
        "small": 9007199254740991,
        "amount": "0.10",
    }
    assert result.column_meta == (
        {"name": "big", "logical_type": "int64", "wire_encoding": "decimal_string"},
        {"name": "small", "logical_type": "int64", "wire_encoding": "number"},
        {"name": "amount", "logical_type": "decimal", "wire_encoding": "decimal_string"},
    )


@pytest.mark.parametrize("value", [None, True, False, -1, 1.5, "1"])
def test_timing_payload_requires_nonnegative_integer(value: object) -> None:
    with pytest.raises(QueryExecutionError, match="invalid timing data"):
        engine_module._timing_from_payload({"startup_ms": value}, "startup_ms")


def test_timing_payload_accepts_zero() -> None:
    assert engine_module._timing_from_payload({"startup_ms": 0}, "startup_ms") == 0


def test_real_engine_raises_execution_error_for_unresolvable_column(tmp_path: Path) -> None:
    """Passes AST validation but fails inside Polars: a runtime error, not a syntax one."""
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": [1, 2, 3]}).write_parquet(table_path)

    with pytest.raises(QueryExecutionError):
        QueryEngine(timeout_seconds=5 * spawn_timeout_multiplier()).execute(
            table_path,
            "SELECT nonexistent_column FROM dataset",
            limit=1,
        )


class _CaptureConnection:
    def __init__(self) -> None:
        self.payload: object = None
        self.closed = False

    def send(self, payload: object) -> None:
        self.payload = payload

    def close(self) -> None:
        self.closed = True


def test_worker_rejects_result_over_response_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": ["x" * 200]}).write_parquet(table_path)
    connection = _CaptureConnection()
    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", 100)

    engine_module._query_worker(
        connection,  # type: ignore[arg-type]
        str(table_path),
        "SELECT * FROM dataset",
        1,
        time.monotonic_ns(),
    )

    assert connection.payload == {"ok": False}
    assert connection.closed is True


class _StubbornProcess:
    def __init__(self) -> None:
        self.alive = True
        self.calls: list[str] = []

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        self.calls.append(f"join:{timeout}")


def test_stop_process_uses_bounded_join_then_kill_fallback() -> None:
    process = _StubbornProcess()

    QueryEngine._stop_process(process)  # type: ignore[arg-type]

    assert process.calls == ["terminate", "join:1.0", "kill", "join:1.0"]
    assert process.is_alive() is False


def _allocating_worker(
    connection: Any, table_path: str, canonical_sql: str, limit: int, parent_started_ns: int
) -> None:
    """Stands in for an expensive sort: asks for far more memory than the cap allows."""
    del table_path, canonical_sql, limit, parent_started_ns
    hog = bytearray(2 * 1024 * 1024 * 1024)
    # A complete, valid result — so without the cap this query would succeed, and
    # the test could not pass by accident.
    connection.send(
        {
            "ok": True,
            "columns": ["size"],
            "column_meta": [],
            "rows": [{"size": len(hog)}],
            "truncated": False,
            "startup_ms": 0,
            "engine_execution_ms": 0,
        }
    )


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is POSIX")
def test_a_query_over_its_memory_cap_fails_alone(tmp_path: Path) -> None:
    """#701: the expensive query dies, the server and the next query do not."""
    import polars as pl

    capped = QueryEngine(
        timeout_seconds=5 * spawn_timeout_multiplier(),
        worker=_allocating_worker,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    with pytest.raises(QueryExecutionError):
        capped.execute(tmp_path / "unused.parquet", "SELECT * FROM dataset", limit=1)

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": [1, 2]}).write_parquet(table_path)
    after = QueryEngine(timeout_seconds=5 * spawn_timeout_multiplier()).execute(
        table_path, "SELECT value FROM dataset", limit=5
    )
    assert after.rows == ({"value": 1}, {"value": 2})


@pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is POSIX")
def test_a_generous_cap_leaves_ordinary_queries_alone(tmp_path: Path) -> None:
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": [3, 1, 2]}).write_parquet(table_path)

    result = QueryEngine(
        timeout_seconds=5 * spawn_timeout_multiplier(),
        memory_limit_bytes=16 * 1024 * 1024 * 1024,
    ).execute(table_path, "SELECT value FROM dataset ORDER BY value", limit=5)

    assert result.rows == ({"value": 1}, {"value": 2}, {"value": 3})


def test_a_non_positive_memory_cap_is_refused() -> None:
    with pytest.raises(ValueError, match="memory_limit_bytes"):
        QueryEngine(memory_limit_bytes=0)

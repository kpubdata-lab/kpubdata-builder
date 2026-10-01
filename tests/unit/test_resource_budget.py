"""Per-connection DuckDB limits and query worker cleanup (#701, ADR 0021 D9)."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from multiprocessing.connection import Connection
from pathlib import Path

import duckdb
import pytest

from kpubdata_builder.query.engine import QueryEngine, QueryExecutionError, QueryTimeoutError
from kpubdata_builder.tabular.duckdb_runtime import (
    MAX_TEMP_SIZE_ENV,
    MEMORY_LIMIT_ENV,
    THREADS_ENV,
    BuildProfile,
    build_connection,
    connect,
)

from .conftest import spawn_timeout_multiplier
from .test_query_engine import _pid_is_alive

# ---------------------------------------------------------------- DuckDB connection limits


def test_the_profile_reads_the_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    assert BuildProfile.from_env() == BuildProfile()
    monkeypatch.setenv(THREADS_ENV, "3")
    monkeypatch.setenv(MEMORY_LIMIT_ENV, "512MB")
    monkeypatch.setenv(MAX_TEMP_SIZE_ENV, "2GiB")

    assert BuildProfile.from_env() == BuildProfile(
        memory_limit="512MB", threads=3, max_temp_directory_size="2GiB"
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        (THREADS_ENV, "0"),
        (THREADS_ENV, "two"),
        (MEMORY_LIMIT_ENV, "lots"),
        (MAX_TEMP_SIZE_ENV, "1 GB'; --"),
    ],
)
def test_a_bad_setting_names_its_variable(
    name: str, value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        BuildProfile.from_env()


def test_a_build_connection_takes_the_deployment_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(THREADS_ENV, "1")
    monkeypatch.setenv(MEMORY_LIMIT_ENV, "256MB")
    monkeypatch.setenv(MAX_TEMP_SIZE_ENV, "64MB")

    with build_connection(tmp_path, "src") as connection:
        ((threads, temp),) = connection.execute(
            "SELECT current_setting('threads'), current_setting('max_temp_directory_size')"
        ).fetchall()

    assert threads == 1
    assert temp.startswith("61.0 MiB")  # 64 MB as DuckDB reports it


# A sort bigger than 64MB of buffer memory: it has to spill. (A window, not DISTINCT,
# because DuckDB 1.2's hash aggregate runs out of memory before it spills.)
_SPILLING = (
    "SELECT max(rn) FROM (SELECT row_number() OVER (ORDER BY md5(i::VARCHAR)) AS rn "
    "FROM range(3000000) t(i))"
)


def _spill_bytes(directory: Path) -> int:
    return sum(f.stat().st_blocks * 512 for f in directory.rglob("*") if f.is_file())


def test_spilling_stops_at_the_quota(tmp_path: Path) -> None:
    """Negative: past max_temp_directory_size a query fails; it never fills the disk.

    Given in the connect config the quota was reported but not enforced (#701) — a
    spilling query wrote over 400 MB past a 4 MB quota.
    """
    spill = tmp_path / "spill"
    spill.mkdir()
    connection = connect(
        BuildProfile(memory_limit="64MB", threads=1, max_temp_directory_size="4MB"), spill
    )
    try:
        with pytest.raises(duckdb.OutOfMemoryException, match="failed to offload"):
            connection.execute(_SPILLING).fetchall()
        assert _spill_bytes(spill) <= 8 * 2**20
    finally:
        connection.close()


def test_a_query_within_the_quota_spills_and_finishes(tmp_path: Path) -> None:
    spill = tmp_path / "spill"
    spill.mkdir()
    connection = connect(
        BuildProfile(memory_limit="64MB", threads=1, max_temp_directory_size="4GB"), spill
    )
    try:
        assert connection.execute(_SPILLING).fetchall() == [(3_000_000,)]
    finally:
        connection.close()


# ---------------------------------------------------------------- query worker cleanup


def _publish_pid(table_path: str) -> None:
    Path(table_path).write_text(str(os.getpid()), encoding="utf-8")


def _answering_worker(
    connection: Connection, table_path: str, sql: str, limit: int, started: int
) -> None:
    del sql, limit, started
    _publish_pid(table_path)
    connection.send(
        {
            "ok": True,
            "columns": [],
            "column_meta": [],
            "rows": [],
            "truncated": False,
            "startup_ms": 0,
            "engine_execution_ms": 0,
        }
    )
    connection.close()


def _failing_then_lingering_worker(
    connection: Connection, table_path: str, sql: str, limit: int, started: int
) -> None:
    del sql, limit, started
    _publish_pid(table_path)
    connection.send({"ok": False})
    time.sleep(60)  # a failed query whose child does not exit on its own


def _crashing_worker(
    connection: Connection, table_path: str, sql: str, limit: int, started: int
) -> None:
    del connection, sql, limit, started
    _publish_pid(table_path)
    os._exit(3)


def _stubborn_worker(
    connection: Connection, table_path: str, sql: str, limit: int, started: int
) -> None:
    del connection, sql, limit, started
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    _publish_pid(table_path)
    time.sleep(60)


def _assert_no_child_left(pid_file: Path) -> None:
    assert not _pid_is_alive(int(pid_file.read_text(encoding="utf-8")))
    assert multiprocessing.active_children() == []


def test_success_leaves_no_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    engine = QueryEngine(timeout_seconds=10 * spawn_timeout_multiplier(), worker=_answering_worker)

    engine.execute(pid_file, "SELECT 1", limit=1)

    _assert_no_child_left(pid_file)


def test_a_failed_query_leaves_no_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    engine = QueryEngine(
        timeout_seconds=10 * spawn_timeout_multiplier(), worker=_failing_then_lingering_worker
    )

    with pytest.raises(QueryExecutionError):
        engine.execute(pid_file, "SELECT 1", limit=1)

    _assert_no_child_left(pid_file)


def test_a_crashed_child_is_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    engine = QueryEngine(timeout_seconds=10 * spawn_timeout_multiplier(), worker=_crashing_worker)

    with pytest.raises(QueryExecutionError):
        engine.execute(pid_file, "SELECT 1", limit=1)

    _assert_no_child_left(pid_file)


@pytest.mark.skipif(os.name == "nt", reason="SIGTERM cannot be ignored on Windows")
def test_a_child_that_ignores_terminate_is_killed_at_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    engine = QueryEngine(timeout_seconds=5 * spawn_timeout_multiplier(), worker=_stubborn_worker)

    with pytest.raises(QueryTimeoutError):
        engine.execute(pid_file, "SELECT 1", limit=1)

    _assert_no_child_left(pid_file)


def test_a_cancelled_request_stops_its_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request's thread is interrupted while waiting: the child goes with it."""
    pid_file = tmp_path / "pid"

    def interrupted(self: Connection, timeout: float | None = 0.0) -> bool:
        deadline = time.monotonic() + 20 * spawn_timeout_multiplier()
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        raise KeyboardInterrupt

    monkeypatch.setattr(Connection, "poll", interrupted)
    engine = QueryEngine(timeout_seconds=60, worker=_stubborn_worker)

    with pytest.raises(KeyboardInterrupt):
        engine.execute(pid_file, "SELECT 1", limit=1)

    _assert_no_child_left(pid_file)

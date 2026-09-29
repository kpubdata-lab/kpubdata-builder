"""Bounded query capacity and permit lifecycle tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.service import (
    QueryBusyError,
    QueryService,
    query_max_concurrency_from_env,
)


class _BlockingEngine:
    def __init__(self) -> None:
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0
        self.started = threading.Event()

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del table_path, canonical_sql, limit
        with self._lock:
            self.calls += 1
            if self.calls >= 2:
                self.started.set()
        self.release.wait(timeout=5)
        return QueryResult((), (), False, 0, 0, 0)


def test_capacity_plus_one_is_rejected_without_engine_call() -> None:
    engine = _BlockingEngine()
    service = QueryService(engine=engine, max_concurrency=2)  # type: ignore[arg-type]
    threads = [
        threading.Thread(
            target=service.execute,
            args=(Path("unused"), "SELECT * FROM dataset"),
            kwargs={"limit": 1},
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert engine.started.wait(timeout=2)

    started = time.monotonic()
    with pytest.raises(QueryBusyError):
        service.execute(Path("unused"), "SELECT * FROM dataset", limit=1)
    assert time.monotonic() - started < 0.25
    assert engine.calls == 2

    engine.release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    # Both permits were returned after success.
    service.execute(Path("unused"), "SELECT * FROM dataset", limit=1)
    assert engine.calls == 3


class _FailingEngine:
    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del table_path, canonical_sql, limit
        raise RuntimeError("boom")


def test_permit_is_returned_after_engine_error() -> None:
    service = QueryService(engine=_FailingEngine(), max_concurrency=1)  # type: ignore[arg-type]
    for _ in range(2):
        with pytest.raises(RuntimeError):
            service.execute(Path("unused"), "SELECT * FROM dataset", limit=1)


@pytest.mark.parametrize("value", ["0", "-1", "not-an-integer"])
def test_query_concurrency_env_requires_positive_integer(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KPUBDATA_QUERY_MAX_CONCURRENCY", value)
    with pytest.raises(ValueError, match="positive integer"):
        query_max_concurrency_from_env()


def test_explicit_zero_concurrency_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        QueryService(max_concurrency=0)


def test_the_memory_cap_is_opt_in_and_given_in_megabytes(monkeypatch: pytest.MonkeyPatch) -> None:
    from kpubdata_builder.query.service import query_memory_limit_from_env

    monkeypatch.delenv("KPUBDATA_QUERY_MAX_MEMORY_MB", raising=False)
    assert query_memory_limit_from_env() is None
    monkeypatch.setenv("KPUBDATA_QUERY_MAX_MEMORY_MB", "2048")
    assert query_memory_limit_from_env() == 2048 * 1024 * 1024
    for bad in ("0", "-1", "lots"):
        monkeypatch.setenv("KPUBDATA_QUERY_MAX_MEMORY_MB", bad)
        with pytest.raises(ValueError, match="KPUBDATA_QUERY_MAX_MEMORY_MB"):
            query_memory_limit_from_env()

"""Queries are admitted by memory budget, and a reservation never leaks (#701, D3)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from kpubdata_builder.query.engine import QueryExecutionError, QueryTimeoutError
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.service import MemoryBudget, QueryBusyError, QueryService

_MB = 1024 * 1024


def _result() -> QueryResult:
    return QueryResult(
        columns=(), rows=(), truncated=False, execution_ms=0, startup_ms=0, engine_execution_ms=0
    )


class _Engine:
    """Stands in for QueryEngine: raises, blocks, or returns, as told."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def execute(self, table_path: Path, plan: str, *, limit: int) -> QueryResult:
        self.started.set()
        if self.gate is not None:
            self.gate.wait(timeout=10)
        if self.raises is not None:
            raise self.raises
        return _result()


def _service(
    engine: _Engine, *, cap_mb: int | None, budget_mb: int, monkeypatch: pytest.MonkeyPatch
) -> QueryService:
    if cap_mb is None:
        monkeypatch.delenv("KPUBDATA_QUERY_MAX_MEMORY_MB", raising=False)
    else:
        monkeypatch.setenv("KPUBDATA_QUERY_MAX_MEMORY_MB", str(cap_mb))
    return QueryService(
        engine=engine,  # type: ignore[arg-type]
        rows_engine=engine,  # type: ignore[arg-type]
        aggregate_engine=engine,  # type: ignore[arg-type]
        export_engine=engine,  # type: ignore[arg-type]
        profile_engine=engine,  # type: ignore[arg-type]
        max_concurrency=8,
        memory_budget_bytes=budget_mb * _MB,
    )


def test_the_budget_counts_reservations() -> None:
    budget = MemoryBudget(100)

    assert budget.reserve(60)
    assert not budget.reserve(50)
    budget.release(60)
    assert budget.reserve(100)


def test_a_query_beyond_the_budget_is_refused_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: the second query does not hang; it is a structured busy error."""
    engine = _Engine()
    engine.gate = threading.Event()
    service = _service(engine, cap_mb=600, budget_mb=1000, monkeypatch=monkeypatch)
    first = threading.Thread(target=lambda: service.execute(Path("t"), "SELECT 1", limit=1))
    first.start()
    try:
        assert engine.started.wait(5)
        with pytest.raises(QueryBusyError, match="memory budget"):
            service.execute_rows(Path("t"), "{}", limit=1)
    finally:
        engine.gate.set()
        first.join(5)

    assert service.memory_budget is not None and service.memory_budget.reserved == 0
    service.execute_aggregate(Path("t"), "{}", limit=1)


@pytest.mark.parametrize(
    "raises",
    [
        None,
        QueryExecutionError("failed"),
        QueryTimeoutError("timed out"),
        KeyboardInterrupt(),
    ],
    ids=["success", "failure", "timeout", "cancelled"],
)
def test_every_ending_returns_the_reservation(
    monkeypatch: pytest.MonkeyPatch, raises: BaseException | None
) -> None:
    service = _service(_Engine(raises), cap_mb=500, budget_mb=1000, monkeypatch=monkeypatch)
    calls = (
        lambda: service.execute(Path("t"), "SELECT 1", limit=1),
        lambda: service.execute_rows(Path("t"), "{}", limit=1),
        lambda: service.execute_aggregate(Path("t"), "{}", limit=1),
        lambda: service.execute_export(Path("t"), "{}"),
        lambda: service.execute_profile(Path("t"), "{}"),
    )

    for call in calls:
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - each ending is the point of the test
            assert raises is not None and type(exc) is type(raises)
        assert service.memory_budget is not None
        assert service.memory_budget.reserved == 0


def test_without_a_cap_queries_run_one_at_a_time(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _Engine()
    engine.gate = threading.Event()
    service = _service(engine, cap_mb=None, budget_mb=1000, monkeypatch=monkeypatch)
    first = threading.Thread(target=lambda: service.execute(Path("t"), "SELECT 1", limit=1))
    first.start()
    try:
        assert engine.started.wait(5)
        with pytest.raises(QueryBusyError):
            service.execute(Path("t"), "SELECT 1", limit=1)
    finally:
        engine.gate.set()
        first.join(5)


def test_without_a_budget_only_the_concurrency_limit_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an unset budget changes nothing."""
    monkeypatch.delenv("KPUBDATA_QUERY_MEMORY_BUDGET_MB", raising=False)
    service = QueryService(engine=_Engine(), max_concurrency=1)  # type: ignore[arg-type]

    assert service.memory_budget is None
    service.execute(Path("t"), "SELECT 1", limit=1)


@pytest.mark.parametrize("raw", ["0", "-1", "lots"])
def test_a_bad_budget_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("KPUBDATA_QUERY_MEMORY_BUDGET_MB", raw)

    with pytest.raises(ValueError):
        QueryService(engine=_Engine())  # type: ignore[arg-type]

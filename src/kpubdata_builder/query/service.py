"""Bounded query execution service."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .aggregate import aggregate_worker
from .engine import QueryEngine
from .export import export_worker
from .models import QueryResult
from .profile import profile_worker
from .rows import rows_worker

DEFAULT_QUERY_MAX_CONCURRENCY = 2
#: An export reads the whole result and writes it, so it gets longer than a query (#819).
EXPORT_TIMEOUT_SECONDS = 60.0
_QUERY_CONCURRENCY_ENV = "KPUBDATA_QUERY_MAX_CONCURRENCY"
_QUERY_MEMORY_ENV = "KPUBDATA_QUERY_MAX_MEMORY_MB"
_QUERY_BUDGET_ENV = "KPUBDATA_QUERY_MEMORY_BUDGET_MB"


class QueryBusyError(RuntimeError):
    pass


def query_memory_budget_from_env() -> int | None:
    """The deployment's memory budget for concurrent queries in bytes, or None (#701)."""
    raw = os.environ.get(_QUERY_BUDGET_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        megabytes = int(raw)
    except ValueError as exc:
        raise ValueError(f"{_QUERY_BUDGET_ENV} must be a positive integer") from exc
    if megabytes < 1:
        raise ValueError(f"{_QUERY_BUDGET_ENV} must be a positive integer")
    return megabytes * 1024 * 1024


class MemoryBudget:
    """Admission by memory (#701, owner decision D3).

    A query runs only when the memory caps of the queries already running, plus its
    own, fit in the deployment's budget. Its cap is the child's ``RLIMIT_AS`` limit
    (#780) — the most it can take — so the sum bounds what queries can use together.
    A reservation is returned when the query ends, however it ends.
    """

    def __init__(self, total_bytes: int) -> None:
        if total_bytes < 1:
            raise ValueError("a memory budget must be positive")
        self._total = total_bytes
        self._reserved = 0
        self._lock = threading.Lock()

    @property
    def reserved(self) -> int:
        with self._lock:
            return self._reserved

    def reserve(self, amount: int) -> bool:
        with self._lock:
            if self._reserved + amount > self._total:
                return False
            self._reserved += amount
            return True

    def release(self, amount: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - amount)


def query_max_concurrency_from_env() -> int:
    raw = os.environ.get(_QUERY_CONCURRENCY_ENV)
    if raw is None:
        return DEFAULT_QUERY_MAX_CONCURRENCY
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{_QUERY_CONCURRENCY_ENV} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{_QUERY_CONCURRENCY_ENV} must be a positive integer")
    return value


def query_memory_limit_from_env() -> int | None:
    """Per-query child address-space cap in bytes, or None when unset (#701)."""
    raw = os.environ.get(_QUERY_MEMORY_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        megabytes = int(raw)
    except ValueError as exc:
        raise ValueError(f"{_QUERY_MEMORY_ENV} must be a positive integer") from exc
    if megabytes < 1:
        raise ValueError(f"{_QUERY_MEMORY_ENV} must be a positive integer")
    return megabytes * 1024 * 1024


class QueryService:
    def __init__(
        self,
        *,
        engine: QueryEngine | None = None,
        max_concurrency: int | None = None,
        rows_engine: QueryEngine | None = None,
        aggregate_engine: QueryEngine | None = None,
        export_engine: QueryEngine | None = None,
        profile_engine: QueryEngine | None = None,
        memory_budget_bytes: int | None = None,
    ) -> None:
        """Args:
        rows_engine: Runs paged row reads (#815). Defaults to a child-process engine
            with the same memory cap, and shares this service's concurrency limit:
            a page read costs a query slot like any query.
        aggregate_engine: Runs validated aggregates (#818), on the same terms.
        export_engine: Writes query exports (#819): the same memory cap and slot, and
            ``EXPORT_TIMEOUT_SECONDS`` rather than the query timeout.
        profile_engine: Computes column profiles (#817), on the same terms as a query.
        memory_budget_bytes: The deployment's memory budget for queries running at once
            (#701). Each query reserves its per-query cap from it; without a cap, a
            query reserves the whole budget, so queries run one at a time. None reads
            ``KPUBDATA_QUERY_MEMORY_BUDGET_MB``; unset, only the concurrency limit
            applies, as before.
        """
        capacity = query_max_concurrency_from_env() if max_concurrency is None else max_concurrency
        if capacity < 1:
            raise ValueError("max_concurrency must be positive")
        memory_limit = query_memory_limit_from_env()
        self._engine = engine or QueryEngine(memory_limit_bytes=memory_limit)
        self._rows_engine = rows_engine or QueryEngine(
            worker=rows_worker, memory_limit_bytes=memory_limit
        )
        self._aggregate_engine = aggregate_engine or QueryEngine(
            worker=aggregate_worker, memory_limit_bytes=memory_limit
        )
        self._export_engine = export_engine or QueryEngine(
            worker=export_worker,
            memory_limit_bytes=memory_limit,
            timeout_seconds=EXPORT_TIMEOUT_SECONDS,
        )
        self._profile_engine = profile_engine or QueryEngine(
            worker=profile_worker, memory_limit_bytes=memory_limit
        )
        self._capacity = threading.BoundedSemaphore(capacity)
        budget = (
            memory_budget_bytes
            if memory_budget_bytes is not None
            else query_memory_budget_from_env()
        )
        self._budget = MemoryBudget(budget) if budget is not None else None
        # What one query reserves: its RLIMIT_AS cap, or the whole budget without one.
        self._reservation = memory_limit if memory_limit is not None else (budget or 0)

    @contextmanager
    def _admitted(self) -> Iterator[None]:
        """A concurrency slot and a memory reservation, both returned on every exit.

        Success, a failed query, a timeout and a cancelled request all leave through
        ``finally`` — the engine raises for each — so the budget cannot leak.
        """
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        reserved = False
        try:
            if self._budget is not None:
                if not self._budget.reserve(self._reservation):
                    raise QueryBusyError("the query memory budget is exhausted")
                reserved = True
            yield
        finally:
            if reserved and self._budget is not None:
                self._budget.release(self._reservation)
            self._capacity.release()

    @property
    def memory_budget(self) -> MemoryBudget | None:
        """The admission budget, when one is configured — for monitoring and tests."""
        return self._budget

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        with self._admitted():
            return self._engine.execute(table_path, canonical_sql, limit=limit)

    def execute_rows(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        """Read one page of rows by a validated plan (#815), under the same limits."""
        with self._admitted():
            return self._rows_engine.execute(table_path, plan_json, limit=limit)

    def execute_aggregate(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        """Run a validated aggregate (#818), under the same limits as a query."""
        with self._admitted():
            return self._aggregate_engine.execute(table_path, plan_json, limit=limit)

    def execute_profile(self, table_path: Path, plan_json: str) -> QueryResult:
        """Profile a snapshot's columns (#817), under a query slot and its limits."""
        with self._admitted():
            return self._profile_engine.execute(table_path, plan_json, limit=0)

    def execute_export(self, table_path: Path, plan_json: str) -> QueryResult:
        """Write a query's result to the plan's file (#819), under a query slot."""
        with self._admitted():
            return self._export_engine.execute(table_path, plan_json, limit=0)


__all__ = [
    "DEFAULT_QUERY_MAX_CONCURRENCY",
    "EXPORT_TIMEOUT_SECONDS",
    "MemoryBudget",
    "QueryBusyError",
    "QueryService",
    "query_max_concurrency_from_env",
    "query_memory_budget_from_env",
    "query_memory_limit_from_env",
]

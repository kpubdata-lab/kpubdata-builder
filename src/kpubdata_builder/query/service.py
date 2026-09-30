"""Bounded query execution service."""

from __future__ import annotations

import os
import threading
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


class QueryBusyError(RuntimeError):
    pass


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
    ) -> None:
        """Args:
        rows_engine: Runs paged row reads (#815). Defaults to a child-process engine
            with the same memory cap, and shares this service's concurrency limit:
            a page read costs a query slot like any query.
        aggregate_engine: Runs validated aggregates (#818), on the same terms.
        export_engine: Writes query exports (#819): the same memory cap and slot, and
            ``EXPORT_TIMEOUT_SECONDS`` rather than the query timeout.
        profile_engine: Computes column profiles (#817), on the same terms as a query.
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

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        try:
            return self._engine.execute(table_path, canonical_sql, limit=limit)
        finally:
            self._capacity.release()

    def execute_rows(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        """Read one page of rows by a validated plan (#815), under the same limits."""
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        try:
            return self._rows_engine.execute(table_path, plan_json, limit=limit)
        finally:
            self._capacity.release()

    def execute_aggregate(self, table_path: Path, plan_json: str, *, limit: int) -> QueryResult:
        """Run a validated aggregate (#818), under the same limits as a query."""
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        try:
            return self._aggregate_engine.execute(table_path, plan_json, limit=limit)
        finally:
            self._capacity.release()

    def execute_profile(self, table_path: Path, plan_json: str) -> QueryResult:
        """Profile a snapshot's columns (#817), under a query slot and its limits."""
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        try:
            return self._profile_engine.execute(table_path, plan_json, limit=0)
        finally:
            self._capacity.release()

    def execute_export(self, table_path: Path, plan_json: str) -> QueryResult:
        """Write a query's result to the plan's file (#819), under a query slot."""
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        try:
            return self._export_engine.execute(table_path, plan_json, limit=0)
        finally:
            self._capacity.release()


__all__ = [
    "DEFAULT_QUERY_MAX_CONCURRENCY",
    "EXPORT_TIMEOUT_SECONDS",
    "QueryBusyError",
    "QueryService",
    "query_max_concurrency_from_env",
    "query_memory_limit_from_env",
]

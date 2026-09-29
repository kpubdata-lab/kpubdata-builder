"""Bounded query execution service."""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .engine import QueryEngine
from .models import QueryResult

DEFAULT_QUERY_MAX_CONCURRENCY = 2
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
    ) -> None:
        capacity = query_max_concurrency_from_env() if max_concurrency is None else max_concurrency
        if capacity < 1:
            raise ValueError("max_concurrency must be positive")
        self._engine = engine or QueryEngine(memory_limit_bytes=query_memory_limit_from_env())
        self._capacity = threading.BoundedSemaphore(capacity)

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        if not self._capacity.acquire(blocking=False):
            raise QueryBusyError("query capacity is exhausted")
        try:
            return self._engine.execute(table_path, canonical_sql, limit=limit)
        finally:
            self._capacity.release()


__all__ = [
    "DEFAULT_QUERY_MAX_CONCURRENCY",
    "QueryBusyError",
    "QueryService",
    "query_max_concurrency_from_env",
    "query_memory_limit_from_env",
]

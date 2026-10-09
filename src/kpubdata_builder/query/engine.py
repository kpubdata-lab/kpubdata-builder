"""SQL execution in a locked DuckDB connection, isolated in a cancellable child process.

The child opens ``query.sandbox`` — one file, its own spill directory, no external
access, configuration locked (#874) — so the three layers of ADR 0021 D6 are the
validator (``security``), the locked connection and this process boundary.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import shutil
import tempfile
import time
from collections.abc import Callable
from contextlib import suppress
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import cast

from ..spec import JsonValue
from ..tabular.duckdb_runtime import RESOURCE_LIMIT_MESSAGE
from .models import QueryResult

logger = logging.getLogger(__name__)


class QueryExecutionError(RuntimeError):
    pass


class QueryTimeoutError(QueryExecutionError):
    pass


class QueryResourceLimitError(QueryExecutionError):
    """The query needed more memory or spill disk than the deployment allows (#961).

    Its message is always ``RESOURCE_LIMIT_MESSAGE`` — the same words a build or a
    preview over its limits gives — never DuckDB's, which can name sizes and the spill
    directory.
    """


MAX_QUERY_RESPONSE_BYTES = 8 * 1024 * 1024

WorkerFn = Callable[[Connection, str, str, int, int], None]


def _bounded_worker(
    memory_limit_bytes: int | None,
    temp_dir: str,
    worker: WorkerFn,
    connection: Connection,
    table_path: str,
    canonical_sql: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    """Run ``worker`` in the child after capping the child's address space (#701).

    An expensive sort must cost the query, not the server. The cap is applied inside
    the child, before Polars is imported, so exceeding it fails this process — the
    parent sees a closed pipe or ``ok: False`` and answers 400 while every other
    request carries on. Where the platform has no ``resource`` module the cap is not
    applied, and nothing else changes.
    """
    import os

    from .sandbox import TEMP_DIR_ENV

    # The parent made this directory and removes it, also after a kill.
    os.environ[TEMP_DIR_ENV] = temp_dir
    if memory_limit_bytes is not None:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
        except (ImportError, ValueError, OSError):
            pass
    worker(connection, table_path, canonical_sql, limit, parent_started_ns)


#: The ``reason`` a worker sends with ``ok: False`` when the query ran out of memory or
#: spill disk. Only this fixed word crosses the process boundary, never the exception.
RESOURCE_LIMIT_REASON = "resource_limit"


def failure_payload(error: BaseException) -> dict[str, object]:
    """What a worker sends after ``error``: ``ok: False``, plus why when it was a limit.

    DuckDB's out-of-memory or spill-quota error, Builder's ``ResourceLimitError`` and a
    ``MemoryError`` from the child's address-space cap (#701) are the deployment's
    limits; anything else is a failure the client is not told more about.
    """
    import duckdb

    from ..tabular.duckdb_runtime import ResourceLimitError

    if isinstance(error, (duckdb.OutOfMemoryException, ResourceLimitError, MemoryError)):
        return {"ok": False, "reason": RESOURCE_LIMIT_REASON}
    return {"ok": False}


def _elapsed_ms(started_ns: int, ended_ns: int | None = None) -> int:
    end = time.monotonic_ns() if ended_ns is None else ended_ns
    return max(0, (end - started_ns) // 1_000_000)


def _timing_from_payload(payload: dict[object, object], field: str) -> int:
    value = payload.get(field)
    if type(value) is not int or value < 0:
        raise QueryExecutionError("query returned invalid timing data")
    return value


def _query_worker(
    connection: Connection,
    table_path: str,
    canonical_sql: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    try:
        from .result import to_wire
        from .sandbox import open_sandbox
        from .stored_columns import stored_null_outputs

        bounded_sql = f"SELECT * FROM ({canonical_sql}) AS _kpubdata_result LIMIT {limit + 1}"
        with open_sandbox(table_path) as sandbox:
            # Startup ends after spawn, imports and the locked connection, before the query.
            startup_ms = _elapsed_ms(parent_started_ns)
            engine_started_ns = time.monotonic_ns()
            relation = sandbox.connection.sql(bounded_sql)
            # A stored Null column read as it is stays Null, as a page of rows reports
            # it: DuckDB types the sandbox's NULL as an INTEGER.
            null = stored_null_outputs(
                canonical_sql,
                relation.columns,
                columns=sandbox.columns,
                null_columns=[n for n, dtype in sandbox.dtypes.items() if dtype == "Null"],
            )
            stored = None if null is None else ["Null" if flag else None for flag in null]
            result = to_wire(relation, stored=stored)
            engine_execution_ms = _elapsed_ms(engine_started_ns)
        # Wire-encoded by column (#735): a Decimal or an out-of-range integer arrives as
        # its exact decimal text, and `column_meta` says which columns that applies to.
        rows = result.rows
        payload = {
            "ok": True,
            "columns": result.columns,
            "column_meta": result.column_meta,
            "rows": rows[:limit],
            "truncated": len(rows) > limit,
            "startup_ms": startup_ms,
            "engine_execution_ms": engine_execution_ms,
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_QUERY_RESPONSE_BYTES:
            connection.send({"ok": False})
        else:
            connection.send(payload)
    except BaseException as exc:
        # Engine messages can contain absolute parquet paths. Never cross the
        # process boundary with raw exceptions or tracebacks.
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send(failure_payload(exc))
    finally:
        connection.close()


class QueryEngine:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        worker: WorkerFn = _query_worker,
        memory_limit_bytes: int | None = None,
    ) -> None:
        """Args:
        memory_limit_bytes: Address-space cap for each query's child process, or
            None for no cap (#701). Opt-in: which budget a deployment has is its own
            decision, and a cap below what Polars reserves would fail every query.
        """
        if memory_limit_bytes is not None and memory_limit_bytes < 1:
            raise ValueError("memory_limit_bytes must be positive")
        self._timeout_seconds = timeout_seconds
        self._worker = worker
        self._memory_limit_bytes = memory_limit_bytes

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        started_ns = time.monotonic_ns()
        temp_dir = tempfile.mkdtemp(prefix="kpubdata-query-")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_bounded_worker,
            args=(
                self._memory_limit_bytes,
                temp_dir,
                self._worker,
                child,
                str(table_path),
                canonical_sql,
                limit,
                started_ns,
            ),
            daemon=True,
        )
        process_started = False
        try:
            process.start()
            process_started = True
            child.close()
            if not parent.poll(self._timeout_seconds):
                self._stop_process(process)
                raise QueryTimeoutError("query execution timed out")
            try:
                payload = parent.recv()
            except (EOFError, OSError) as exc:
                raise QueryExecutionError("query execution failed") from exc
            process.join(timeout=1.0)
            if process.is_alive():
                self._stop_process(process)
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                if isinstance(payload, dict) and payload.get("reason") == RESOURCE_LIMIT_REASON:
                    raise QueryResourceLimitError(RESOURCE_LIMIT_MESSAGE)
                raise QueryExecutionError("query execution failed")
            columns = payload.get("columns")
            meta = payload.get("column_meta")
            rows = payload.get("rows")
            truncated = payload.get("truncated")
            if (
                not isinstance(columns, list)
                or not isinstance(meta, list)
                or not isinstance(rows, list)
                or not isinstance(truncated, bool)
            ):
                raise QueryExecutionError("query returned an invalid result")
            startup_ms = _timing_from_payload(payload, "startup_ms")
            engine_execution_ms = _timing_from_payload(payload, "engine_execution_ms")
            extra = payload.get("meta")
            execution_ms = _elapsed_ms(started_ns)
            result = QueryResult(
                columns=tuple(str(column) for column in columns),
                column_meta=tuple(cast(dict[str, JsonValue], item) for item in meta),
                rows=tuple(cast(dict[str, JsonValue], row) for row in rows),
                truncated=truncated,
                execution_ms=execution_ms,
                startup_ms=startup_ms,
                engine_execution_ms=engine_execution_ms,
                meta=cast(dict[str, JsonValue], extra) if isinstance(extra, dict) else {},
            )
            logger.info(
                "query timing",
                extra={
                    "event": "query_timing",
                    "execution_ms": execution_ms,
                    "startup_ms": startup_ms,
                    "engine_execution_ms": engine_execution_ms,
                    "ipc_serialization_ms": max(0, execution_ms - startup_ms - engine_execution_ms),
                    "row_count": len(result.rows),
                    "column_count": len(result.columns),
                    "truncated": result.truncated,
                },
            )
            return result
        finally:
            child.close()
            parent.close()
            if process_started:
                if process.is_alive():
                    self._stop_process(process)
                if not process.is_alive():
                    process.close()
            else:
                with suppress(ValueError):
                    process.close()
            # The child's spill files go with it, however it ended.
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _stop_process(process: BaseProcess) -> None:
        if not process.is_alive():
            process.join(timeout=0)
            return
        process.terminate()
        process.join(timeout=1.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=1.0)
        if process.is_alive():
            raise QueryExecutionError("query process could not be stopped")


__all__ = [
    "MAX_QUERY_RESPONSE_BYTES",
    "QueryEngine",
    "QueryExecutionError",
    "QueryResourceLimitError",
    "QueryTimeoutError",
]

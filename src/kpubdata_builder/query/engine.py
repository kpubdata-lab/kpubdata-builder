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
from typing import TYPE_CHECKING, cast

from ..spec import JsonValue
from ..tabular.duckdb_runtime import RESOURCE_LIMIT_MESSAGE
from . import cancellation
from .models import QueryResult

if TYPE_CHECKING:
    from .result import StreamedTable

logger = logging.getLogger(__name__)


class QueryExecutionError(RuntimeError):
    pass


class QueryTimeoutError(QueryExecutionError):
    pass


class QueryCancelledError(QueryExecutionError):
    """The client that asked for the query closed the connection (#1245).

    A subclass of ``QueryExecutionError`` so that every route already handles it; the
    HTTP layer sends nothing, because nobody is there to read it.
    """


class QueryResourceLimitError(QueryExecutionError):
    """The query needed more memory or spill disk than the deployment allows (#961).

    Its message is always ``RESOURCE_LIMIT_MESSAGE`` — the same words a build or a
    preview over its limits gives — never DuckDB's, which can name sizes and the spill
    directory.
    """


MAX_QUERY_RESPONSE_BYTES = 8 * 1024 * 1024
#: How often a query inside a request looks whether its requester has gone (#1245).
CANCEL_CHECK_SECONDS = 0.1

WorkerFn = Callable[[Connection, str, str, int, int], None]


def _bounded_worker(
    memory_limit_bytes: int | None,
    temp_dir: str,
    masked: list[str],
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

    from .sandbox import MASKED_COLUMNS_ENV, TEMP_DIR_ENV

    # The parent made this directory and removes it, also after a kill.
    os.environ[TEMP_DIR_ENV] = temp_dir
    # Always set, so the sandbox masks what this query was given and nothing inherited.
    os.environ[MASKED_COLUMNS_ENV] = json.dumps(masked)
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


#: Rows fetched from the result table at a time while the byte budget is counted.
_FETCH_BATCH_ROWS = 256


def _rows_within_budget(result: StreamedTable, limit: int) -> list[dict[str, JsonValue]] | None:
    """The first ``limit`` rows encoded for the wire, or None once they cannot fit (#1332).

    The SQL floor refuses a huge text, binary or list cell before it is fetched; after
    it, rows are read a batch at a time and each one's exact UTF-8 JSON size is added
    up, so reading stops at the first row past the budget instead of after the last.
    """
    budget = MAX_QUERY_RESPONSE_BYTES
    if result.wire_bytes_floor(limit) > budget:
        return None
    rows: list[dict[str, JsonValue]] = []
    spent = len("[]")
    for _, row in result.rows(batch_size=_FETCH_BATCH_ROWS, count=limit):
        spent += len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + len(", ")
        if spent > budget:
            return None
        rows.append(row)
    return rows


def _query_worker(
    connection: Connection,
    table_path: str,
    canonical_sql: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    try:
        from .result import stream_result
        from .sandbox import masked_columns_from_env, open_sandbox
        from .stored_columns import stored_null_outputs

        # Passed explicitly: a child not told what to mask refuses rather than masks none.
        with open_sandbox(table_path, masked=masked_columns_from_env()) as sandbox:
            # Startup ends after spawn, imports and the locked connection, before the query.
            startup_ms = _elapsed_ms(parent_started_ns)
            engine_started_ns = time.monotonic_ns()
            # A stored Null column read as it is stays Null, as a page of rows reports
            # it: DuckDB types the sandbox's NULL as an INTEGER.
            null = stored_null_outputs(
                canonical_sql,
                sandbox.connection.sql(canonical_sql).columns,
                columns=sandbox.columns,
                null_columns=[n for n, dtype in sandbox.dtypes.items() if dtype == "Null"],
            )
            stored = None if null is None else ["Null" if flag else None for flag in null]
            # Held in a table that spills within the quota and read back a batch at a
            # time, so the byte budget is spent while reading, not after (#1332).
            result = stream_result(
                sandbox.connection, canonical_sql, limit=limit + 1, stored=stored
            )
            rows = _rows_within_budget(result, limit)
            engine_execution_ms = _elapsed_ms(engine_started_ns)
        if rows is None:
            connection.send({"ok": False})
            return
        # Wire-encoded by column (#735): a Decimal or an out-of-range integer arrives as
        # its exact decimal text, and `column_meta` says which columns that applies to.
        payload = {
            "ok": True,
            "columns": result.columns,
            "column_meta": result.column_meta,
            "rows": rows,
            "truncated": result.row_count > limit,
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

    def execute(
        self,
        table_path: Path,
        canonical_sql: str,
        *,
        limit: int,
        masked: frozenset[str] = frozenset(),
    ) -> QueryResult:
        """Run ``canonical_sql`` in a child process and return its result.

        Args:
            masked: Columns the sandbox's views show masked (#1331): a Silver read's
                declared PII, masked as the query reads it rather than copied first.
        """
        started_ns = time.monotonic_ns()
        temp_dir = tempfile.mkdtemp(prefix="kpubdata-query-")
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=_bounded_worker,
            args=(
                self._memory_limit_bytes,
                temp_dir,
                sorted(masked),
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
            if not self._wait_for_answer(parent):
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

    def _wait_for_answer(self, parent: Connection) -> bool:
        """Whether the child answered before the timeout.

        Inside a request's watch (#1245) the wait is cut into short ones, and between
        them the watch is asked whether the requester has gone. Outside one it is one
        wait, as before.

        Raises:
            QueryCancelledError: The requester has gone; the caller stops the child.
        """
        watch = cancellation.current()
        if watch is None:
            return parent.poll(self._timeout_seconds)
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if parent.poll(min(remaining, CANCEL_CHECK_SECONDS)):
                return True
            if watch.requester_gone():
                raise QueryCancelledError("the requester closed the connection")

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
    "CANCEL_CHECK_SECONDS",
    "MAX_QUERY_RESPONSE_BYTES",
    "QueryCancelledError",
    "QueryEngine",
    "QueryExecutionError",
    "QueryResourceLimitError",
    "QueryTimeoutError",
]

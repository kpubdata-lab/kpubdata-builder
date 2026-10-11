#!/usr/bin/env python3
"""Measure what the query limits bound, on the machine this runs on (#1245).

The limits in the deployment guide were chosen without measurement. This script puts a
load on the query path and prints what it saw, with the machine it saw it on, so that a
number chosen for ``KPUBDATA_QUERY_MAX_CONCURRENCY`` or
``KPUBDATA_BUILDER_QUERY_RATE_PER_MINUTE`` can point at a measurement.

What it runs, in process (no HTTP, no proxy, no network):

1. A Parquet table of ``--rows`` synthetic rows in a temporary directory.
2. For each client count in ``--clients``: that many threads, each sending an aggregate
   query through ``QueryService`` — the same child process, sandbox, concurrency slots
   and memory reservation a request gets — back to back for ``--seconds``. A query
   refused with ``query_busy`` is counted and retried after ``--backoff`` seconds.
3. The cost of one admission of the per-user rate limiter (``WindowLimiter.admit``).

It prints one JSON document. Its numbers describe this machine only: a deployment's
figures need the same run on the deployment's hardware, with its settings.

Usage:
    uv run python scripts/measure_query_load.py
    uv run python scripts/measure_query_load.py --rows 1000000 --clients 1 2 4 8 --seconds 20
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

import duckdb

from kpubdata_builder.query.engine import QueryExecutionError
from kpubdata_builder.query.security import validate_read_only_sql
from kpubdata_builder.query.service import QueryBusyError, QueryService
from kpubdata_builder.rate_limit import WindowLimiter

_SQL = (
    "SELECT station, COUNT(*) AS n, AVG(value) AS mean "
    "FROM dataset GROUP BY station ORDER BY n DESC LIMIT 50"
)


def _machine() -> dict[str, object]:
    memory_bytes: int | None = None
    try:
        memory_bytes = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        memory_bytes = None
    cpu_model = None
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        for line in cpuinfo.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("model name"):
                cpu_model = line.split(":", 1)[1].strip()
                break
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "memory_bytes": memory_bytes,
        "python": sys.version.split()[0],
        "duckdb": duckdb.__version__,
    }


def _table(directory: Path, rows: int) -> Path:
    path = directory / "load.parquet"
    connection = duckdb.connect()
    try:
        connection.execute(
            f"COPY (SELECT 'st' || (i % 200)::VARCHAR AS station, "  # noqa: S608 - int only
            f"(i * 7 % 1000)::DOUBLE AS value FROM range({int(rows)}) t(i)) "
            f"TO '{path.as_posix()}' (FORMAT parquet)"
        )
    finally:
        connection.close()
    return path


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _run(
    service: QueryService, table: Path, clients: int, seconds: float, backoff: float
) -> dict[str, object]:
    canonical = validate_read_only_sql(_SQL).canonical_sql
    latencies: list[float] = []
    busy = 0
    failed = 0
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def client() -> None:
        nonlocal busy, failed
        while time.monotonic() < deadline:
            started = time.perf_counter()
            try:
                service.execute(table, canonical, limit=50)
            except QueryBusyError:
                with lock:
                    busy += 1
                time.sleep(backoff)
                continue
            except QueryExecutionError:
                with lock:
                    failed += 1
                continue
            elapsed_ms = (time.perf_counter() - started) * 1000
            with lock:
                latencies.append(elapsed_ms)

    threads = [threading.Thread(target=client) for _ in range(clients)]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.monotonic() - started
    return {
        "clients": clients,
        "seconds": round(wall, 1),
        "completed": len(latencies),
        "query_busy": busy,
        "failed": failed,
        "queries_per_minute": round(len(latencies) / wall * 60, 1),
        "latency_ms_p50": _percentile(latencies, 0.5),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "latency_ms_mean": round(statistics.fmean(latencies), 1) if latencies else None,
    }


def _admission_cost(rounds: int) -> dict[str, object]:
    limiter = WindowLimiter(lambda: rounds + 1)
    started = time.perf_counter()
    for index in range(rounds):
        limiter.admit(f"owner-{index % 50}")
    elapsed = time.perf_counter() - started
    return {"admissions": rounds, "microseconds_each": round(elapsed / rounds * 1e6, 2)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--rows", type=int, default=200_000)
    parser.add_argument("--clients", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--concurrency", type=int, default=2, help="query slots (default 2)")
    parser.add_argument("--backoff", type=float, default=0.05, help="wait after query_busy")
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="kpubdata-load-") as directory:
        table = _table(Path(directory), args.rows)
        service = QueryService(max_concurrency=args.concurrency)
        runs = [
            _run(service, table, clients, args.seconds, args.backoff) for clients in args.clients
        ]
    report = {
        "scope": "local only; not representative of a deployment",
        "machine": _machine(),
        "settings": {
            "rows": args.rows,
            "query_max_concurrency": args.concurrency,
            "query_max_memory_mb": os.environ.get("KPUBDATA_QUERY_MAX_MEMORY_MB") or None,
            "duckdb_threads": os.environ.get("KPUBDATA_DUCKDB_THREADS") or "2 (default)",
            "sql": _SQL,
        },
        "runs": runs,
        "rate_limiter": _admission_cost(100_000),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

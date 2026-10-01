#!/usr/bin/env python3
"""Measure what the data checksums cost (#917).

Every method runs in a fresh child process, so its peak RSS is its own, and is timed
``--repeat`` times; the table reports the median wall and CPU time, the largest peak
RSS, and the largest temporary disk use seen while it ran (sampled every 10 ms).

The input is deterministic: ``--records`` records shaped like the ones in #917,

    {"id": i, "name": f"\\uc11c\\uc6b8-{i % 57}", "value": i % 137}  # Hangul names

written once as a canonical Bronze JSONL file (sorted keys, ``ensure_ascii=False``) in
``--work-dir``. Measurement boundaries are kept apart:

``*-records``   records in memory -> checksum; serialisation included
``*-lines``     serialised lines in memory -> checksum; serialisation excluded
``*-jsonl``     the JSONL file -> checksum; file reading included

Methods:

``v1-*``                ``canonical-json-sort-v1``; ``v1-jsonl`` is the external sort
                        (runs of ``--run-bytes`` spilled beside the file, then merged)
``v2-reference-*``      ``canonical-multiset-v2`` as first written (#867): a general
                        ``% modulus`` after every multiplication
``v2-*``                ``canonical-multiset-v2`` as implemented now (#917); same values
``duckdb-sort-jsonl``   DuckDB sorts the lines (spilling past ``--duckdb-memory-limit``)
                        and they are streamed into SHA-256 as v1 does; it reproduces the
                        v1 value, which the table checks

The checksum column shows whether each method agrees with the others of its algorithm.
Methods of different algorithms are never compared with each other.

Usage:
    uv run python scripts/bench_checksums.py                       # #917's 50,000 case
    uv run python scripts/bench_checksums.py --records 2000000 \\
        --run-bytes 16000000 --duckdb-memory-limit 64MB          # spilling case
    uv run python scripts/bench_checksums.py --methods v2-jsonl v2-reference-jsonl
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import resource
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

METHODS = (
    "v1-records",
    "v1-jsonl",
    "v2-reference-records",
    "v2-reference-lines",
    "v2-reference-jsonl",
    "v2-records",
    "v2-lines",
    "v2-jsonl",
    "duckdb-sort-jsonl",
)


def _record(i: int) -> dict[str, Any]:
    return {"id": i, "name": f"\uc11c\uc6b8-{i % 57}", "value": i % 137}


def _write_input(path: Path, count: int) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for i in range(count):
            handle.write(json.dumps(_record(i), ensure_ascii=False, sort_keys=True))
            handle.write("\n")


# ------------------------------------------------------------------ methods


def _reference_v2(lines: Iterator[str]) -> str:
    """``canonical-multiset-v2`` exactly as #867 first computed it."""
    from kpubdata_builder.manifest.checksums import _DOMAIN, _ELEMENT_BYTES, _MODULUS

    product, count = 1, 0
    for line in lines:
        element = hashlib.shake_256(_DOMAIN + line.encode("utf-8")).digest(_ELEMENT_BYTES)
        product = product * (int.from_bytes(element, "big") % _MODULUS or 1) % _MODULUS
        count += 1
    final = hashlib.sha256(_DOMAIN)
    final.update(count.to_bytes(8, "big"))
    final.update(product.to_bytes(_ELEMENT_BYTES, "big"))
    return f"sha256:{final.hexdigest()}"


def _current_v2(lines: Iterator[str]) -> str:
    from kpubdata_builder.manifest.checksums import MultisetChecksum

    checksum = MultisetChecksum()
    for line in lines:
        checksum.add_line(line)
    return checksum.hexdigest()


def _jsonl_lines(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\n")
            if line:
                yield line


def _duckdb_sort(path: Path, spill: Path, memory_limit: str) -> str:
    import duckdb

    connection = duckdb.connect(
        config={
            "memory_limit": memory_limit,
            "temp_directory": str(spill),
            "threads": 1,
            "preserve_insertion_order": False,
        }
    )
    try:
        # Canonical lines never hold a raw control character (JSON escapes them), so
        # \x01 as the delimiter keeps each line one column. VARCHAR sorts by UTF-8
        # bytes, which is code point order, the order Python sorts str in. The sorted
        # lines are written to a file and streamed back: a Python result set would be
        # held in memory whole, which is what the memory limit is there to prevent.
        sorted_path = spill / "sorted.txt"
        connection.execute(
            f"COPY (SELECT line FROM read_csv('{path}', columns={{'line': 'VARCHAR'}}, "
            "delim='\x01', quote='', escape='', header=false, auto_detect=false) "
            f"WHERE line <> '' ORDER BY line) TO '{sorted_path}' "
            "(FORMAT csv, delim '\x01', quote '', escape '', header false)"
        )
        digest = hashlib.sha256(b"[")
        first = True
        for line in _jsonl_lines(sorted_path):
            if not first:
                digest.update(b",")
            digest.update(line.encode("utf-8"))
            first = False
        digest.update(b"]")
        return f"sha256:{digest.hexdigest()}"
    finally:
        connection.close()


def _prepare(method: str, args: argparse.Namespace, spill: Path) -> Callable[[], str]:
    """The timed call for ``method``, with its input already in memory."""
    from kpubdata_builder.manifest.checksums import multiset_checksum, record_line
    from kpubdata_builder.manifest.provenance import (
        compute_data_checksum,
        compute_data_checksum_from_jsonl,
    )

    path = Path(args.input)
    if method.endswith("-records"):
        records = [_record(i) for i in range(args.records)]
        if method == "v1-records":
            return lambda: compute_data_checksum(records)
        if method == "v2-records":
            return lambda: multiset_checksum(records)
        return lambda: _reference_v2(record_line(r) for r in records)
    if method.endswith("-lines"):
        lines = list(_jsonl_lines(path))
        if method == "v2-lines":
            return lambda: _current_v2(iter(lines))
        return lambda: _reference_v2(iter(lines))
    if method == "v1-jsonl":
        return lambda: compute_data_checksum_from_jsonl(path, run_bytes=args.run_bytes)
    if method == "v2-jsonl":
        from kpubdata_builder.manifest.checksums import multiset_checksum_of_jsonl

        return lambda: multiset_checksum_of_jsonl(path)
    if method == "v2-reference-jsonl":
        return lambda: _reference_v2(_jsonl_lines(path))
    if method == "duckdb-sort-jsonl":
        return lambda: _duckdb_sort(path, spill, args.duckdb_memory_limit)
    raise SystemExit(f"unknown method {method!r}")


def _tree_bytes(directory: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            # A spill file can be deleted between listing and stat.
            with contextlib.suppress(FileNotFoundError):
                total += os.stat(os.path.join(root, name)).st_size
    return total


def _child(method: str, args: argparse.Namespace) -> None:
    """Run one method once and print its measurements as JSON."""
    input_dir = Path(args.input).parent
    with tempfile.TemporaryDirectory(dir=args.work_dir, prefix="duckdb-spill-") as spill_dir:
        spill = Path(spill_dir)
        call = _prepare(method, args, spill)
        peak_disk = 0
        done = threading.Event()

        def sample() -> None:
            nonlocal peak_disk
            while not done.wait(0.01):
                # v1 spills beside the input; DuckDB into its temp_directory.
                peak_disk = max(peak_disk, _tree_bytes(spill) + _spill_beside(input_dir))

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        wall, cpu = time.perf_counter(), time.process_time()
        checksum = call()
        wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
        done.set()
        sampler.join()
    rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "wall": wall,
                "cpu": cpu,
                "peak_rss_mib": rss_kib / 1024,
                "peak_disk_mib": peak_disk / 2**20,
                "checksum": checksum,
            }
        )
    )


def _spill_beside(directory: Path) -> int:
    return sum(
        _tree_bytes(entry) for entry in directory.iterdir() if entry.name.startswith(".checksum-")
    )


# ------------------------------------------------------------------ driver


def _run(method: str, args: argparse.Namespace) -> dict[str, Any]:
    command = [
        sys.executable,
        __file__,
        "--child",
        method,
        "--input",
        args.input,
        "--records",
        str(args.records),
        "--run-bytes",
        str(args.run_bytes),
        "--duckdb-memory-limit",
        args.duckdb_memory_limit,
        "--work-dir",
        args.work_dir,
    ]
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    if completed.returncode:
        raise SystemExit(f"{method} failed:\n{completed.stderr}")
    output = completed.stdout
    result: dict[str, Any] = json.loads(output.strip().splitlines()[-1])
    return result


def main(argv: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT / "src"))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--records", type=int, default=50_000)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--methods", nargs="+", choices=METHODS, default=list(METHODS))
    parser.add_argument("--run-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--duckdb-memory-limit", default="1GB")
    parser.add_argument("--work-dir", default=None, help="where the input and spills go")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--input", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.child:
        _child(args.child, args)
        return 0

    with tempfile.TemporaryDirectory(dir=args.work_dir, prefix="bench-checksums-") as work:
        args.work_dir = work
        args.input = str(Path(work) / "input" / "raw_records.jsonl")
        Path(args.input).parent.mkdir()
        _write_input(Path(args.input), args.records)
        size_mib = Path(args.input).stat().st_size / 2**20
        print(
            f"records={args.records} input={size_mib:.1f} MiB repeat={args.repeat} "
            f"run_bytes={args.run_bytes} duckdb_memory_limit={args.duckdb_memory_limit}"
        )
        print(f"python={platform.python_version()} platform={platform.platform()}")
        print()
        header = "| method | wall s (median) | CPU s (median) | peak RSS MiB | temp disk MiB |"
        print(header + " checksum |")
        print("|---|---:|---:|---:|---:|---|")
        for method in args.methods:
            runs = [_run(method, args) for _ in range(args.repeat)]
            checksums = {str(run["checksum"]) for run in runs}
            print(
                f"| {method} "
                f"| {statistics.median(float(r['wall']) for r in runs):.4f} "
                f"| {statistics.median(float(r['cpu']) for r in runs):.4f} "
                f"| {max(float(r['peak_rss_mib']) for r in runs):.1f} "
                f"| {max(float(r['peak_disk_mib']) for r in runs):.1f} "
                f"| {'/'.join(sorted(c[7:19] for c in checksums))} |",
                flush=True,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

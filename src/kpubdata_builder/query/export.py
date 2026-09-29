"""Write a pinned query's full result to a file, in the query child process (#819).

A client that turns rows it received into a CSV itself skips every policy the rows
passed through: it cannot know the licence, it does not record which snapshot the file
came from, and a result cut at a page or response limit becomes a file that looks
complete. This module is the worker that writes an export file from one snapshot:

- **All or nothing.** The query is bounded at ``max_rows + 1`` rows; one more than the
  limit means the result does not fit, and no file is written. A file that grows past
  ``max_bytes`` is removed. Neither is ever handed back as a complete result.
- **Two profiles.** ``machine`` writes every value exactly as the wire encoding sends it
  (#735): codes keep their leading zeros, Decimals and large integers are their exact
  text, dates ISO 8601, and nothing is altered. ``spreadsheet`` is for opening in a
  spreadsheet: UTF-8 with a BOM, and a text cell that starts with a formula trigger
  (``= + - @`` tab, CR) gets a leading apostrophe so it is not run (#221, CWE-1236).
  That alters the value, so the worker counts every altered cell per column for the
  export's manifest. The BOM only tells a spreadsheet the encoding: it does not stop it
  from dropping leading zeros, rounding large integers or turning text into dates.
- **PII evidence.** The result's text values are scanned with the build's value
  patterns; the findings (column, kind, count — never a value) go back to the caller,
  which decides by the source's PII policy whether the file may be kept.
- **Same limits as a query.** Child process, timeout, memory cap, concurrency slot.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import os
import time
from collections.abc import Iterable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import IO, Literal, cast

from ..spec import JsonValue

ExportFormat = Literal["csv", "jsonl"]
ExportProfile = Literal["machine", "spreadsheet"]

DEFAULT_EXPORT_MAX_ROWS = 100_000
MAX_EXPORT_ROWS = 1_000_000
MAX_EXPORT_BYTES = 256 * 1024 * 1024

#: Leading characters a spreadsheet reads as a formula. Kept equal to the CSV exporter's
#: (``exporters/csv.py``), which applies the same guard to build artifacts.
FORMULA_TRIGGER_CHARS = frozenset("=+-@\t\r")
#: Logical types whose cells are free text a formula could hide in. A negative number
#: or a Decimal sent as text is not text, and is never prefixed.
_TEXT_TYPES = frozenset({"string", "categorical", "enum"})


class ExportLimitExceeded(Exception):
    """The result does not fit the export's row or byte limit."""

    def __init__(self, code: str, limit: int) -> None:
        super().__init__(code)
        self.code = code
        self.limit = limit


@dataclass(frozen=True)
class ExportPlan:
    canonical_sql: str
    output_path: str
    format: ExportFormat = "csv"
    profile: ExportProfile = "machine"
    max_rows: int = DEFAULT_EXPORT_MAX_ROWS
    max_bytes: int = MAX_EXPORT_BYTES

    def to_json(self) -> str:
        return json.dumps(
            {
                "canonical_sql": self.canonical_sql,
                "output_path": self.output_path,
                "format": self.format,
                "profile": self.profile,
                "max_rows": self.max_rows,
                "max_bytes": self.max_bytes,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> ExportPlan:
        data = json.loads(raw)
        return ExportPlan(
            canonical_sql=data["canonical_sql"],
            output_path=data["output_path"],
            format=data["format"],
            profile=data["profile"],
            max_rows=int(data["max_rows"]),
            max_bytes=int(data["max_bytes"]),
        )


class _CountingWriter:
    """A text sink that counts encoded bytes and refuses to grow past a limit."""

    def __init__(self, handle: IO[bytes], encoding: str, limit: int) -> None:
        self._handle = handle
        self._encoding = encoding
        self._limit = limit
        self.written = 0

    def write(self, text: str) -> int:
        data = text.encode(self._encoding)
        self.written += len(data)
        if self.written > self._limit:
            raise ExportLimitExceeded("byte_limit_exceeded", self._limit)
        self._handle.write(data)
        return len(text)


def csv_cell(value: JsonValue) -> str:
    """A wire-encoded value as CSV text: null is empty, nested values are JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def write_rows(
    path: Path,
    columns: Sequence[str],
    column_meta: Sequence[dict[str, JsonValue]],
    rows: Iterable[dict[str, JsonValue]],
    *,
    fmt: ExportFormat,
    profile: ExportProfile,
    max_bytes: int,
) -> dict[str, int]:
    """Write ``rows`` to ``path``; return how many cells were altered, per column.

    Raises ExportLimitExceeded, after removing the partial file, when it outgrows
    ``max_bytes``.
    """
    spreadsheet = profile == "spreadsheet"
    guarded = {
        str(meta.get("name"))
        for meta in column_meta
        if spreadsheet and meta.get("logical_type") in _TEXT_TYPES
    }
    altered: dict[str, int] = {}
    try:
        with path.open("wb") as handle:
            sink = _CountingWriter(handle, "utf-8", max_bytes)
            if spreadsheet:
                # The BOM counts toward the limit like any other byte.
                sink.write("﻿")
            if fmt == "jsonl":
                for row in rows:
                    sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                return altered
            writer = csv.writer(sink, lineterminator="\n")
            writer.writerow(columns)
            for row in rows:
                cells: list[str] = []
                for column in columns:
                    text = csv_cell(row.get(column))
                    if column in guarded and text and text[0] in FORMULA_TRIGGER_CHARS:
                        text = "'" + text
                        altered[column] = altered.get(column, 0) + 1
                    cells.append(text)
                writer.writerow(cells)
        return altered
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _elapsed_ms(started_ns: int) -> int:
    return max(0, (time.monotonic_ns() - started_ns) // 1_000_000)


def export_worker(
    connection: Connection,
    table_path: str,
    plan_json: str,
    limit: int,
    parent_started_ns: int,
) -> None:
    """``QueryEngine`` worker for an export. Writes the file; sends what it wrote."""
    del limit  # the plan carries max_rows
    try:
        import polars as pl

        plan = ExportPlan.from_json(plan_json)
        startup_ms = _elapsed_ms(parent_started_ns)
        engine_started_ns = time.monotonic_ns()
        bounded_sql = (
            f"SELECT * FROM ({plan.canonical_sql}) AS _kpubdata_result LIMIT {plan.max_rows + 1}"
        )
        frame = pl.scan_parquet(table_path)
        context = pl.SQLContext({"dataset": frame}, eager=False, register_globals=False)
        result = context.execute(bounded_sql).collect()
        meta: dict[str, JsonValue] = {"refusal": None}
        if result.height > plan.max_rows:
            meta["refusal"] = {"code": "row_limit_exceeded", "limit": plan.max_rows}
        else:
            from ..stages.silver.pii import scan_pii_values
            from ..tabular.polars_engine import infer_schema
            from ..tabular.wire import column_meta, encode_rows

            columns = infer_schema(result).columns
            wire_meta = column_meta(columns)
            path = Path(plan.output_path)
            try:
                altered = write_rows(
                    path,
                    list(result.columns),
                    wire_meta,
                    encode_rows(result.to_dicts(), columns),
                    fmt=plan.format,
                    profile=plan.profile,
                    max_bytes=plan.max_bytes,
                )
            except ExportLimitExceeded as exc:
                meta["refusal"] = {"code": exc.code, "limit": exc.limit}
            else:
                meta.update(
                    row_count=result.height,
                    bytes=path.stat().st_size,
                    sha256=sha256_file(path),
                    altered=cast(JsonValue, altered),
                    pii=[
                        {"column": f.column, "kind": f.kind, "count": f.count}
                        for f in scan_pii_values(result)
                    ],
                    column_meta=cast(JsonValue, wire_meta),
                )
        connection.send(
            {
                "ok": True,
                "columns": list(result.columns),
                "column_meta": [],
                "rows": [],
                "truncated": False,
                "startup_ms": startup_ms,
                "engine_execution_ms": _elapsed_ms(engine_started_ns),
                "meta": meta,
            }
        )
    except BaseException:
        # Engine messages can contain absolute parquet paths; never send them across.
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send({"ok": False})
    finally:
        connection.close()


__all__ = [
    "DEFAULT_EXPORT_MAX_ROWS",
    "FORMULA_TRIGGER_CHARS",
    "MAX_EXPORT_BYTES",
    "MAX_EXPORT_ROWS",
    "ExportFormat",
    "ExportLimitExceeded",
    "ExportPlan",
    "ExportProfile",
    "csv_cell",
    "export_worker",
    "sha256_file",
    "write_rows",
]

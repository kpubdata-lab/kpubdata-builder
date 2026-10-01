"""Engine-neutral canonical forms for the DuckDB parity baseline (#865).

A baseline pins **what** a stage produced, not **how** the bytes came out: column names,
Builder-canonical dtypes, nullability and values, typed so that ``1`` and ``1.0``,
``Decimal("1.50")`` and ``1.5``, a date and its ISO string stay different. Parquet bytes,
timestamps, run ids, paths and timings are never part of it (ADR 0021 D5).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl

from kpubdata_builder.tabular.builder_parquet import read_builder_parquet

#: Keys whose values vary from run to run and are never compared.
VOLATILE_KEYS = frozenset(
    {
        "started_at",
        "finished_at",
        "fetched_at",
        "collected_at",
        "created_at",
        "updated_at",
        "generated_at",
        "computed_at",
        "checked_at",
        "committed_at",
        "recorded_at",
        "observed_at",
        "build_environment",
        "build_id",
        "run_id",
        "snapshot_id",
        "table_id",
        "export_id",
        "execution_ms",
        "startup_ms",
        "engine_execution_ms",
        "artifact_id",
        "artifact_digest",
        "bytes",
        "sha256",
        "digest",
        "outputs",
        "inputs_fingerprint",
        "path",
        "paths",
        "download_url",
        "expires_at",
        "revision",
        "bronze_path",
        "table_path",
    }
)


def canonical_value(value: Any) -> Any:
    """A JSON value that keeps the type distinctions a comparison must see."""
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return {"float": repr(value)}
        return value
    if isinstance(value, Decimal):
        return {"decimal": format(value, "f")}
    if isinstance(value, dt.datetime):
        return {"datetime": value.isoformat()}
    if isinstance(value, dt.date):
        return {"date": value.isoformat()}
    if isinstance(value, dt.time):
        return {"time": value.isoformat()}
    if isinstance(value, dt.timedelta):
        return {"duration_us": value // dt.timedelta(microseconds=1)}
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, dict):
        return {str(k): canonical_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_value(v) for v in value]
    return {"repr": repr(value)}


def canonical_dtype(dtype: pl.DataType) -> str:
    """The Builder-canonical dtype string (ADR 0021 D3) — today spelt as Polars spells it."""
    return str(dtype)


def canonical_frame(frame: pl.DataFrame) -> dict[str, Any]:
    return {
        "columns": list(frame.columns),
        "dtypes": [canonical_dtype(t) for t in frame.dtypes],
        "nullable": [frame.get_column(c).null_count() > 0 for c in frame.columns],
        "rows": [[canonical_value(v) for v in row] for row in frame.iter_rows()],
    }


def canonical_parquet(path: Path) -> dict[str, Any]:
    """A Parquet file's logical schema and values — never its bytes (R12).

    Read as Builder's readers read it (``builder_parquet``): with the Builder dtypes the
    file records given back, so a file DuckDB wrote compares by what users see.
    """
    return canonical_frame(read_builder_parquet(path))


#: Generated identifiers that show up inside text, such as a NOTICE naming its snapshot.
_VOLATILE_TEXT = (
    (re.compile(r"\bsnap_[0-9a-f]{8,}\b"), "snap_<id>"),
    (re.compile(r"\bexp_[0-9a-f]{8,}\b"), "exp_<id>"),
    (re.compile(r"\bupl_[0-9a-f]{8,}\b"), "upl_<id>"),
    # A dataset card's collection time (#694): when the source was fetched.
    (re.compile(r"Collected: \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC"), "Collected: <time>"),
    (
        re.compile(r'"collected_at": "\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC"'),
        '"collected_at": "<time>"',
    ),
)


def strip_text(text: str) -> str:
    """``text`` with generated identifiers replaced by placeholders."""
    for pattern, placeholder in _VOLATILE_TEXT:
        text = pattern.sub(placeholder, text)
    return text


def strip_volatile(value: Any) -> Any:
    """``value`` without the keys that vary between runs, recursively."""
    if isinstance(value, dict):
        return {k: strip_volatile(v) for k, v in value.items() if k not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(v) for v in value]
    if isinstance(value, str):
        return strip_text(value)
    return value


def canonical_json_file(path: Path) -> Any:
    return strip_volatile(canonical_value(json.loads(path.read_text(encoding="utf-8"))))


def canonical_jsonl(path: Path) -> list[Any]:
    return [
        canonical_value(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def canonical_csv(path: Path) -> list[list[str]]:
    """CSV logical rows: the cells as text, header first."""
    text = path.read_text(encoding="utf-8-sig")
    return [row for row in csv.reader(io.StringIO(text))]


def to_json(value: Any) -> str:
    """The stable text a golden file holds."""
    return json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


__all__ = [
    "VOLATILE_KEYS",
    "canonical_csv",
    "canonical_dtype",
    "canonical_frame",
    "canonical_json_file",
    "canonical_jsonl",
    "canonical_parquet",
    "canonical_value",
    "strip_text",
    "strip_volatile",
    "to_json",
]

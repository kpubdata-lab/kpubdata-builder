"""Parse File/URL raw bytes to Bronze records (#498).

File upload and URL fetch obtain bytes differently, but rules for converting bytes
to records must be identical. Same parsing result regardless of source.

Supported formats are CSV/JSON/JSONL/Parquet (#498 P0 scope). Excel/ZIP out of scope
loader/validator already rejects those values.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping
from typing import cast

import polars as pl

from ..spec import JsonValue
from ..tabular.convert import dataframe_to_records
from .errors import IngestionError

_TEXT_FORMATS = frozenset({"csv", "json", "jsonl"})


def parse_tabular_bytes(
    raw: bytes,
    *,
    format: str,  # noqa: A002 - matches contract field name
    encoding: str = "utf-8",
    read_as: Mapping[str, str] | None = None,
) -> tuple[dict[str, JsonValue], ...]:
    """Parse raw bytes by ``format`` rules and return record tuple.

    Args:
        raw: Raw bytes from file or HTTP response.
        format: ``"csv"`` | ``"json"`` | ``"jsonl"`` | ``"parquet"``.
        encoding: Encoding for text format (csv/json/jsonl) decoding. Parquet is
            binary format, ignored.
        read_as: ``sources[].schema.read_as`` declaration. CSV uses lexeme (original string)
            remains only in parse step — ``pl.read_csv`` infers ``00123`` as integer
            ``123``; after that, even if converted back to string in Silver, leading 0
            can't be recovered. So declared columns read as strings from here.

    Returns:
        Record tuples. Same as consumed by Bronze pipeline
        ``dict[str, JsonValue]`` format.

    Raises:
        IngestionError: Empty content, unsupported format, decode/parse failure.
    """
    if not raw:
        raise IngestionError("source content is empty")

    if format == "parquet":
        return _parse_parquet(raw)
    if format in _TEXT_FORMATS:
        text = _decode(raw, encoding)
        if format == "csv":
            return _parse_csv(text, read_as=read_as)
        if format == "json":
            return _parse_json(text)
        return _parse_jsonl(text)
    raise IngestionError(f"unsupported format: {format!r}")


def _decode(raw: bytes, encoding: str) -> str:
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise IngestionError(f"failed to decode content as {encoding!r}: {exc}") from exc


def _parse_parquet(raw: bytes) -> tuple[dict[str, JsonValue], ...]:
    try:
        frame = pl.read_parquet(io.BytesIO(raw))
    except Exception as exc:  # Polars throws various exception types, so catch broadly
        raise IngestionError(f"failed to parse parquet content: {exc}") from exc
    return tuple(dataframe_to_records(frame))


def _parse_csv(
    text: str, *, read_as: Mapping[str, str] | None = None
) -> tuple[dict[str, JsonValue], ...]:
    # Fix only declared columns to Utf8. Leave inference for undeclared columns unchanged,
    # so specs not using read_as don't change behavior.
    overrides = {column: pl.Utf8 for column, dtype in (read_as or {}).items() if dtype == "str"}
    try:
        frame = pl.read_csv(
            io.StringIO(text),
            infer_schema_length=None,
            schema_overrides=overrides or None,
        )
    except Exception as exc:
        raise IngestionError(f"failed to parse csv content: {exc}") from exc
    return tuple(dataframe_to_records(frame))


def _parse_json(text: str) -> tuple[dict[str, JsonValue], ...]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestionError(f"failed to parse json content: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        # Explicitly allow only array-of-object instead of free-form
        # key guessing (e.g. {"data": [...]})
        # — same "no free-form eval/guessing" principle already enforced in
        # quality.compare_columns etc.
        raise IngestionError(
            'json content must be a top-level array of objects (e.g. [{"col": "value"}, ...])'
        )
    return tuple(cast(list[dict[str, JsonValue]], data))


def _parse_jsonl(text: str) -> tuple[dict[str, JsonValue], ...]:
    records: list[dict[str, JsonValue]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise IngestionError(f"failed to parse jsonl line {line_number}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IngestionError(f"jsonl line {line_number} must be a JSON object")
        records.append(parsed)
    if not records:
        raise IngestionError("jsonl content has no non-empty lines")
    return tuple(records)


__all__ = ["parse_tabular_bytes"]

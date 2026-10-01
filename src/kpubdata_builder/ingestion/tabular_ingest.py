"""Parse File/URL content to Bronze records (#498), a batch at a time (#622).

File upload and URL fetch obtain content differently, but rules for converting it to
records must be identical. Same parsing result regardless of source.

Supported formats are CSV/JSON/JSONL/Parquet (#498 P0 scope). Excel/ZIP out of scope
loader/validator already rejects those values.

Content is read as a stream and records come out in batches, so a file larger than
memory is never held whole — not as bytes, not as text, not as a record list:

- **JSONL** is read line by line.
- **JSON** (a top-level array of objects) is read element by element. A malformed
  element is refused where it is found, and one element may span at most
  :data:`MAX_JSON_ELEMENT_CHARS` characters (#920).
- **CSV** is decoded to UTF-8 into a spill file, then read in batches by Polars. The
  column types are still inferred from the whole file (``infer_schema_length=None``),
  so a batch never decides a type the next batch contradicts.
- **Parquet** is spilled to a file and read in batches.

:func:`parse_tabular_bytes` remains for callers holding small content in memory; it
gives the same records the stream gives.
"""

from __future__ import annotations

import codecs
import io
import json
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

import polars as pl

from ..spec import JsonValue
from ..tabular.convert import dataframe_to_records
from .errors import IngestionError

_TEXT_FORMATS = frozenset({"csv", "json", "jsonl"})
_FORMATS = _TEXT_FORMATS | {"parquet"}

#: Records per batch handed to the caller.
BATCH_RECORDS = 10_000

#: Bytes read from the stream at a time.
_CHUNK_BYTES = 1024 * 1024

#: Most characters one top-level JSON value — an element of the array, or a non-array
#: document being classified — may span. This bounds a single element, not the
#: document: an array of any size streams as long as each element fits. Content larger
#: than this as one value is refused with an error naming the limit rather than buffered
#: without bound (#920). It is above the default upload limit
#: (``KPUBDATA_BUILDER_MAX_UPLOAD_BYTES``, 20 MiB), so it only matters where that limit
#: is raised or for fetched URLs.
MAX_JSON_ELEMENT_CHARS = 32 * 1024 * 1024

_ARRAY_OF_OBJECTS = (
    'json content must be a top-level array of objects (e.g. [{"col": "value"}, ...])'
)

Batch = list[dict[str, JsonValue]]


def iter_tabular_batches(
    source: BinaryIO,
    *,
    format: str,  # noqa: A002 - matches contract field name
    encoding: str = "utf-8",
    read_as: Mapping[str, str] | None = None,
    workdir: Path | None = None,
    batch_records: int = BATCH_RECORDS,
) -> Iterator[Batch]:
    """Parse ``source`` by ``format`` rules, yielding records in order, a batch at a time.

    Args:
        source: The content, read from its current position to the end.
        format: ``"csv"`` | ``"json"`` | ``"jsonl"`` | ``"parquet"``.
        encoding: Encoding for text format (csv/json/jsonl) decoding. Parquet is
            binary format, ignored.
        read_as: ``sources[].schema.read_as`` declaration. CSV uses lexeme (original string)
            remains only in parse step — Polars infers ``00123`` as integer ``123``;
            after that, even if converted back to string in Silver, leading 0 can't be
            recovered. So declared columns read as strings from here.
        workdir: Where CSV and Parquet content is spilled to be read in batches; a
            private temporary directory if omitted. The spill file is removed when the
            iterator finishes or is closed.
        batch_records: Records per batch (the last may hold fewer).

    Raises:
        IngestionError: Empty content, unsupported format, decode/parse failure.
    """
    if format not in _FORMATS:
        raise IngestionError(f"unsupported format: {format!r}")
    stream = _byte_chunks(source)
    if format == "parquet":
        with _spill(workdir) as path:
            _copy(stream, path)
            yield from _parquet_batches(path, batch_records)
        return
    decoder = _decoder(encoding)
    if format == "csv":
        with _spill(workdir) as path:
            _transcode(stream, decoder, encoding, path)
            yield from _csv_batches(path, read_as, batch_records)
        return
    chunks = _decoded_chunks(stream, decoder, encoding)
    if format == "json":
        yield from _json_batches(chunks, batch_records)
    else:
        yield from _jsonl_batches(chunks, batch_records)


def parse_tabular_bytes(
    raw: bytes,
    *,
    format: str,  # noqa: A002 - matches contract field name
    encoding: str = "utf-8",
    read_as: Mapping[str, str] | None = None,
) -> tuple[dict[str, JsonValue], ...]:
    """Parse in-memory content and return every record — for content that is small.

    Raises:
        IngestionError: Empty content, unsupported format, decode/parse failure.
    """
    return tuple(
        record
        for batch in iter_tabular_batches(
            io.BytesIO(raw), format=format, encoding=encoding, read_as=read_as
        )
        for record in batch
    )


# ----------------------------------------------------------------- stream helpers


def _byte_chunks(source: BinaryIO) -> Iterator[bytes]:
    """The content in chunks; empty content is refused before anything is yielded."""
    first = source.read(_CHUNK_BYTES)
    if not first:
        raise IngestionError("source content is empty")

    def chunks() -> Iterator[bytes]:
        yield first
        while chunk := source.read(_CHUNK_BYTES):
            yield chunk

    return chunks()


def _decoder(encoding: str) -> codecs.IncrementalDecoder:
    try:
        return codecs.getincrementaldecoder(encoding)()
    except LookupError as exc:
        raise IngestionError(f"failed to decode content as {encoding!r}: {exc}") from exc


def _decoded_chunks(
    stream: Iterator[bytes], decoder: codecs.IncrementalDecoder, encoding: str
) -> Iterator[str]:
    try:
        for chunk in stream:
            if text := decoder.decode(chunk):
                yield text
        if text := decoder.decode(b"", final=True):
            yield text
    except UnicodeDecodeError as exc:
        raise IngestionError(f"failed to decode content as {encoding!r}: {exc}") from exc


@contextmanager
def _spill(workdir: Path | None) -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix=".parse-", dir=workdir))
    try:
        yield directory / "content"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _copy(stream: Iterator[bytes], path: Path) -> None:
    with path.open("wb") as handle:
        for chunk in stream:
            handle.write(chunk)


def _transcode(
    stream: Iterator[bytes], decoder: codecs.IncrementalDecoder, encoding: str, path: Path
) -> None:
    """Decode ``stream`` and write it as UTF-8 — the bytes Polars read from the text before."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        for text in _decoded_chunks(stream, decoder, encoding):
            handle.write(text)


# ----------------------------------------------------------------- formats


def _parquet_batches(path: Path, batch_records: int) -> Iterator[Batch]:
    try:
        frames = pl.scan_parquet(path).collect_batches(
            chunk_size=batch_records, maintain_order=True
        )
        for frame in frames:
            yield dataframe_to_records(frame)
    except IngestionError:
        raise
    except Exception as exc:  # Polars throws various exception types, so catch broadly
        raise IngestionError(f"failed to parse parquet content: {exc}") from exc


def _csv_batches(
    path: Path, read_as: Mapping[str, str] | None, batch_records: int
) -> Iterator[Batch]:
    # Fix only declared columns to Utf8. Leave inference for undeclared columns unchanged,
    # so specs not using read_as don't change behavior.
    overrides = {column: pl.Utf8 for column, dtype in (read_as or {}).items() if dtype == "str"}
    try:
        frames = pl.scan_csv(
            path, infer_schema_length=None, schema_overrides=overrides or None
        ).collect_batches(chunk_size=batch_records, maintain_order=True)
        for frame in frames:
            yield dataframe_to_records(frame)
    except IngestionError:
        raise
    except Exception as exc:
        raise IngestionError(f"failed to parse csv content: {exc}") from exc


def _jsonl_batches(chunks: Iterator[str], batch_records: int) -> Iterator[Batch]:
    batch: Batch = []
    seen = False
    for line_number, line in enumerate(_lines(chunks), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise IngestionError(f"failed to parse jsonl line {line_number}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IngestionError(f"jsonl line {line_number} must be a JSON object")
        batch.append(parsed)
        seen = True
        if len(batch) >= batch_records:
            yield batch
            batch = []
    if not seen:
        raise IngestionError("jsonl content has no non-empty lines")
    if batch:
        yield batch


def _lines(chunks: Iterator[str]) -> Iterator[str]:
    """Split text chunks into lines the way ``str.splitlines`` splits the whole text."""
    pending = ""
    for chunk in chunks:
        lines = (pending + chunk).splitlines(keepends=True)
        # The last piece may continue in the next chunk — and a trailing "\r" may be the
        # first half of "\r\n" — so it waits.
        pending = lines.pop() if lines else ""
        for line in lines:
            yield line.rstrip("\r\n\x0b\x0c\x1c\x1d\x1e\x85  ")
    if pending:
        yield pending.rstrip("\r\n\x0b\x0c\x1c\x1d\x1e\x85  ")


class _JsonArrayReader:
    """Reads a top-level JSON array one element at a time from text chunks.

    An element is decoded from the buffered text; when the decoder fails, the failure is
    either *incomplete input* (the text ends inside the element, so more text may fix
    it) or a *definite syntax error* (a wrong character before the end, which no
    further text can fix). Only incomplete input reads on — a definite error is raised
    at once instead of reading the rest of the content first (#920). While an element is
    incomplete, its buffered text is bounded by ``max_element_chars``, and the buffer at
    least doubles between attempts, so a long element is decoded a logarithmic number of
    times rather than once per chunk.
    """

    def __init__(self, chunks: Iterator[str], *, max_element_chars: int | None = None) -> None:
        self._chunks = chunks
        self._buffer = ""
        self._pos = 0
        self._exhausted = False
        self._decoder = json.JSONDecoder()
        # Read at construction, not import, so the module constant can be changed.
        self._max_element_chars = (
            MAX_JSON_ELEMENT_CHARS if max_element_chars is None else max_element_chars
        )

    def _next_chunk(self) -> str | None:
        if self._exhausted:
            return None
        chunk = next(self._chunks, None)
        if chunk is None:
            self._exhausted = True
        return chunk

    def _more(self) -> bool:
        chunk = self._next_chunk()
        if chunk is None:
            return False
        self._buffer = self._buffer[self._pos :] + chunk
        self._pos = 0
        return True

    def _grow(self) -> bool:
        """Read on for an incomplete element: at least double its text, within the limit.

        Chunks are collected in a list and joined once, so the element's text is copied
        once per attempt rather than once per chunk.
        """
        pending = self._buffer[self._pos :]
        parts = [pending]
        size = len(pending)
        target = min(max(2 * size, size + 1), self._max_element_chars + 1)
        while size < target and (chunk := self._next_chunk()) is not None:
            parts.append(chunk)
            size += len(chunk)
        if len(parts) == 1:
            return False
        self._buffer = "".join(parts)
        self._pos = 0
        return True

    def _skip_space(self) -> str:
        """The next non-space character, not consumed; ``""`` at the end."""
        while True:
            while self._pos < len(self._buffer) and self._buffer[self._pos] in " \t\n\r":
                self._pos += 1
            if self._pos < len(self._buffer):
                return self._buffer[self._pos]
            if not self._more():
                return ""

    def _fail(self, message: str) -> IngestionError:
        return IngestionError(f"failed to parse json content: {message}")

    def elements(self) -> Iterator[object]:
        if self._skip_space() != "[":
            # Not an array: decode the one value (bounded, failing early like an
            # element) only to tell malformed JSON from well-formed JSON of the wrong
            # shape — the content is refused either way.
            self._element()
            if self._skip_space():
                raise self._fail("extra data after the top-level value")
            raise IngestionError(_ARRAY_OF_OBJECTS)
        self._pos += 1
        if self._skip_space() == "]":
            self._pos += 1
        else:
            while True:
                yield self._element()
                separator = self._skip_space()
                self._pos += 1
                if separator == "]":
                    break
                if separator != ",":
                    raise self._fail("expected ',' or ']' between array elements")
                if self._skip_space() == "]":
                    raise self._fail("trailing comma before ']'")
        if self._skip_space():
            raise self._fail("extra data after the top-level array")

    def _element(self) -> object:
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, self._pos)
            except json.JSONDecodeError as exc:
                if self._exhausted or not _may_be_incomplete(exc):
                    raise self._fail(str(exc)) from exc
                if len(self._buffer) - self._pos > self._max_element_chars:
                    raise self._fail(
                        f"a single JSON value is longer than {self._max_element_chars} "
                        "characters (MAX_JSON_ELEMENT_CHARS); the whole array may be "
                        "larger, but each element must fit within this limit"
                    ) from exc
                if self._grow():
                    continue
                raise self._fail(str(exc)) from exc
            if _NUMBER_TAIL.fullmatch(self._buffer, end) and self._more():
                # A number or literal may continue in the next chunk: decode it again
                # with more text before trusting where it ended.
                continue
            self._pos = end
            return value


#: What may follow a number the decoder stopped early on because the text ended:
#: nothing, or the start of a fraction or exponent ("1." / "1e" / "1e+").
_NUMBER_TAIL = re.compile(r"(?:\.|[eE][+-]?)?")

#: The text from a decode error's position to the end of the buffer when that text may
#: still become valid: empty (the text ended where a token was expected), a cut number
#: tail, a cut ``\uXXXX`` escape (the error points at the ``u``; the C decoder wants one
#: more character after the four digits, so all four may be there), or a cut literal —
#: Python's decoder also accepts ``NaN``, ``Infinity`` and ``-Infinity``.
_INCOMPLETE_TAIL = re.compile(
    r"|\.|[eE][+-]?|u[0-9A-Fa-f]{0,4}"
    r"|t(?:r(?:u)?)?|f(?:a(?:l(?:s)?)?)?|n(?:u(?:l)?)?"
    r"|N(?:a)?|-?(?:I(?:n(?:f(?:i(?:n(?:i(?:t)?)?)?)?)?)?)?"
)


def _may_be_incomplete(exc: json.JSONDecodeError) -> bool:
    """Whether more text could make ``exc`` go away, as opposed to a definite error.

    The decoder reports a string cut by the end of the text as unterminated (at the
    opening quote, so the position alone does not tell); every other cut is reported at
    a position whose remaining text is one of :data:`_INCOMPLETE_TAIL`. Any other
    remaining text holds a character no continuation can make valid.
    """
    if exc.msg.startswith("Unterminated string"):
        return True
    return _INCOMPLETE_TAIL.fullmatch(exc.doc, exc.pos) is not None


def _json_batches(chunks: Iterator[str], batch_records: int) -> Iterator[Batch]:
    batch: Batch = []
    for element in _JsonArrayReader(chunks).elements():
        # Explicitly allow only array-of-object instead of free-form
        # key guessing (e.g. {"data": [...]})
        # — same "no free-form eval/guessing" principle already enforced in
        # quality.compare_columns etc.
        if not isinstance(element, dict):
            raise IngestionError(_ARRAY_OF_OBJECTS)
        batch.append(cast(dict[str, JsonValue], element))
        if len(batch) >= batch_records:
            yield batch
            batch = []
    if batch:
        yield batch


__all__ = ["BATCH_RECORDS", "iter_tabular_batches", "parse_tabular_bytes"]

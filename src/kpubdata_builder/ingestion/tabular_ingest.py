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
- **CSV** is decoded to UTF-8 into a spill file. Its records are read here, strictly
  (an unclosed quote or an extra field is refused with its line), written again without
  ambiguity, and typed by DuckDB over the whole file — so a batch never decides a type
  the next batch contradicts.
- **Parquet** is spilled to a file and read in batches by DuckDB.

:func:`parse_tabular_bytes` remains for callers holding small content in memory; it
gives the same records the stream gives.
"""

from __future__ import annotations

import codecs
import datetime as dt
import io
import json
import logging
import re
import shutil
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, cast

from ..spec import JsonValue
from .errors import IngestionError

logger = logging.getLogger(__name__)

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
            for batch in _parquet_batches(path, batch_records):
                _refuse_untexted_values(batch)
                yield batch
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


#: Parquet column types whose values have no one standard text. Builder's JSON, CSV and
#: Markdown outputs would each have to pick one, so the upload is refused instead (#979).
_UNTEXTED = {bytes: "binary", dt.timedelta: "duration"}


def _refuse_untexted_values(batch: Batch) -> None:
    """Refuse a record holding a binary or duration value, naming the column and type."""
    for record in batch:
        for column, value in record.items():
            kind = _untexted_kind(value)
            if kind is not None:
                raise IngestionError(
                    f"column {column!r} holds {kind} values, which a parquet upload cannot "
                    "carry: write the column as text or a number before uploading"
                )


def _untexted_kind(value: object) -> str | None:
    """``binary`` or ``duration`` when the value, or one nested in it, is one."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return None
    if isinstance(value, (list, tuple)):
        return next(filter(None, map(_untexted_kind, value)), None)
    if isinstance(value, dict):
        return next(filter(None, map(_untexted_kind, value.values())), None)
    return _UNTEXTED.get(type(value))


#: A zoned timestamp or UUID as the type of a list element or struct field: a type name
#: is followed by ``,``, ``)``, ``[`` or the end, where a field's name is followed by a space.
_NESTED_UNFETCHABLE = re.compile(r"\b(?:TIMESTAMP WITH TIME ZONE|UUID)(?=[,)\[]|$)")


def _parquet_batches(path: Path, batch_records: int) -> Iterator[Batch]:
    """The rows of a Parquet file as records, in file order, read by DuckDB.

    An instant (a zoned timestamp) is given as an aware datetime in UTC.
    """
    import duckdb

    from ..tabular.sql import quote_identifier

    with duckdb.connect(":memory:") as connection:
        connection.execute("SET TimeZone = 'UTC'")
        try:
            described = connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]
            ).fetchall()
            names = [str(row[0]) for row in described]
            types = [str(row[1]) for row in described]
            zoned = [t == "TIMESTAMP WITH TIME ZONE" for t in types]
            for name, type_ in zip(names, types, strict=True):
                # Inside a list or struct an instant needs pytz to fetch, and a UUID comes
                # out as an object no output writes: refused by name (#876 review).
                if type_ not in ("TIMESTAMP WITH TIME ZONE", "UUID") and _NESTED_UNFETCHABLE.search(
                    type_
                ):
                    raise IngestionError(
                        f"column {name!r} holds zoned timestamps or UUIDs inside a list or "
                        "struct, which a parquet upload cannot carry: store them as text"
                    )
            # Fetching an instant needs pytz, which is not a dependency: read its UTC
            # wall time and mark it UTC here. A UUID is read as its text.
            select = ", ".join(
                f"timezone('UTC', {quote_identifier(n)})"
                if z
                else f"CAST({quote_identifier(n)} AS VARCHAR)"
                if t == "UUID"
                else quote_identifier(n)
                for n, z, t in zip(names, zoned, types, strict=True)
            )
            cursor = connection.execute(f"SELECT {select} FROM read_parquet(?)", [str(path)])
            for rows in iter(lambda: cursor.fetchmany(batch_records), []):
                # Python values, as Polars' ``to_dicts`` gave them: a date, a Decimal.
                yield [
                    cast(
                        dict[str, JsonValue],
                        {
                            name: value.replace(tzinfo=dt.timezone.utc)
                            if z and isinstance(value, dt.datetime)
                            else value
                            for name, z, value in zip(names, zoned, row, strict=True)
                        },
                    )
                    for row in rows
                ]
        except IngestionError:
            raise
        except duckdb.Error as exc:
            # DuckDB's text names the spill file and the SQL; it stays in the log.
            logger.warning("parquet: DuckDB could not read the upload: %s", exc)
            raise IngestionError(
                "failed to parse parquet content: not a Parquet file Builder can read"
            ) from exc


#: How a CSV field's text is typed (the rules Polars applied before #876, kept so the
#: records a spec reads do not change): a column is Boolean when every value is ``true``
#: or ``false`` in any letter case, Int64 when every value is an optionally negative
#: run of digits, Float64 when every value is a float or an integer, and String
#: otherwise — or when it has no values. ``\p{Nd}`` is any decimal digit, as Rust's ``\d``.
_CSV_BOOLEAN = r"(?i)(true|false)"
_CSV_INTEGER = r"-?\p{Nd}+"
_CSV_FLOAT = (
    r"[-+]?((\p{Nd}*\.\p{Nd}+)([eE][-+]?\p{Nd}+)?|inf|NaN|(\p{Nd}+)[eE][-+]?\p{Nd}+|\p{Nd}+\.)"
)
#: A value a column's type cannot hold: an integer beyond 64 bits, or a number in digits
#: other than 0-9 — ``\p{Nd}`` types ``١٢٣`` as a number, as Polars did, and the cast
#: refuses it, as Polars' did (#876 review).
_CSV_NOT_CAST = (
    "failed to parse csv content: a value does not fit the type its column was read as "
    "(an integer beyond 64 bits, or a number written in digits other than 0-9)"
)


#: How a record's fields are read (#876 review): a field starting with a quote is quoted
#: and runs to the next quote not doubled (``""`` inside is a quote), which must be
#: followed by a comma or the end of the record; any other field runs to the next comma,
#: and a quote inside it is a character, as Polars read it. Quotes are found with
#: ``str.find``, so a record is read in time and memory linear in its length — a regular
#: expression over a quoted field remembered every character, and re-reading a record
#: from its start on each of its lines took time quadratic in them.


def _closing_quote(text: str, position: int) -> int:
    """The index of the quote closing a field whose text starts at ``position``, or -1
    when the field is not closed in ``text``."""
    while True:
        quote = text.find('"', position)
        if quote == -1 or not text.startswith('"', quote + 1):
            return quote
        position = quote + 2


def _csv_fields(text: str, position: int, fields: list[str | None], line: int) -> int:
    """Append the fields of ``text`` from ``position`` to ``fields``; -1 when the record
    ends in ``text``, else the index just after the quote opening its last field, which
    continues on the next line."""
    while True:
        if text.startswith('"', position):
            close = _closing_quote(text, position + 1)
            if close == -1:
                return position + 1
            fields.append(text[position + 1 : close].replace('""', '"'))
            position = close + 1
            if position == len(text):
                return -1
            if text[position] != ",":
                raise IngestionError(
                    f"failed to parse csv content: on line {line} a quoted field is "
                    "followed by something other than a comma"
                )
            position += 1
        else:
            comma = text.find(",", position)
            if comma == -1:
                fields.append(text[position:] or None)
                return -1
            fields.append(text[position:comma] or None)
            position = comma + 1


def _csv_records(path: Path) -> Iterator[tuple[int, list[str | None]]]:
    """The records of a CSV file with the line each starts on, strictly (#876 review).

    A quoted field may span lines; one that is never closed is refused with the line the
    record starts on — DuckDB's reader returned the rows before it and dropped the rest
    without an error. A quoted field followed by anything but a comma is refused. An
    unquoted empty field is ``None``, a quoted one the empty string. A blank line is
    skipped. Lines may end in LF, CRLF or CR, mixed; a byte-order mark is dropped before
    the header is read. Each line is read once: a quoted field open at the end of a line
    is kept as its pieces, and the next line is read from where the field went on.
    """
    fields: list[str | None] = []
    #: The pieces of the quoted field open at the end of the last line, else ``None``.
    open_field: list[str] | None = None
    start = 0
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for number, line in enumerate(handle, start=1):
            # One line ending: universal newlines read CRLF, LF and CR, mixed.
            if line.endswith("\r\n"):
                text = line[:-2]
            elif line.endswith(("\n", "\r")):
                text = line[:-1]
            else:
                text = line
            if open_field is None:
                if text == "":
                    continue
                start = number
                if '"' not in text:
                    yield start, [value or None for value in text.split(",")]
                    continue
                position = _csv_fields(text, 0, fields, number)
            else:
                close = _closing_quote(text, 0)
                if close == -1:
                    open_field.append(line)  # the line ending is the field's text
                    continue
                open_field.append(text[:close])
                fields.append("".join(open_field).replace('""', '"'))
                open_field = None
                position = close + 1
                if position < len(text):
                    if text[position] != ",":
                        raise IngestionError(
                            f"failed to parse csv content: on line {number} a quoted field "
                            "is followed by something other than a comma"
                        )
                    position = _csv_fields(text, position + 1, fields, number)
                else:
                    position = -1
            if position == -1:
                yield start, fields
                fields = []
            else:
                open_field = [text[position:], line[len(text) :]]
    if open_field is not None:
        raise IngestionError(
            f"failed to parse csv content: a quoted field starting on line {start} is never closed"
        )


def _csv_names(header: list[str | None]) -> list[str]:
    """The header row's names: a name repeated is renamed ``<name>_duplicated_<n>``.

    A renamed column that takes a name the header already has is refused, as Polars
    refused it, rather than one of the two columns being lost.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    for name in (value or "" for value in header):
        if name in seen:
            names.append(f"{name}_duplicated_{seen[name]}")
            seen[name] += 1
        else:
            names.append(name)
            seen[name] = 0
    if len(set(names)) != len(names):
        raise IngestionError(
            "failed to parse csv content: the header names a column twice once repeated "
            "names are numbered (<name>_duplicated_<n>)"
        )
    return names


def _quoted_csv_value(value: str | None) -> str:
    return "" if value is None else '"' + value.replace('"', '""') + '"'


def _csv_batches(
    path: Path, read_as: Mapping[str, str] | None, batch_records: int
) -> Iterator[Batch]:
    """The rows of a CSV file as records, typed by the whole file (``_CSV_*``).

    The file's structure is read here (``_csv_records``) and written again as a file with
    no ambiguity left — every value quoted, a null unquoted, one LF per record — which
    DuckDB reads as text. Every field stays text until a column's type is decided by all
    of its values, so a column ``read_as: str`` declares keeps its text (``00123``). A
    short row is padded with nulls; a row with more fields than the header is refused.
    """
    import duckdb

    from ..tabular.sql import quote_identifier

    records = _csv_records(path)
    first = next(records, None)
    if first is None:
        raise IngestionError("failed to parse csv content: no header row")
    names = _csv_names(first[1])
    normalized = path.with_name(path.name + ".normalized.csv")
    count = 0
    with normalized.open("w", encoding="utf-8", newline="") as handle:
        for line, fields in records:
            if len(fields) > len(names):
                raise IngestionError(
                    f"failed to parse csv content: line {line} has {len(fields)} fields, "
                    f"the header {len(names)}"
                )
            fields.extend([None] * (len(names) - len(fields)))
            handle.write(",".join(_quoted_csv_value(value) for value in fields) + "\n")
            count += 1
    keep_text = {column for column, dtype in (read_as or {}).items() if dtype == "str"}
    physical = [f"c{i}" for i in range(len(names))]
    columns = "{" + ", ".join(f"'{p}': 'VARCHAR'" for p in physical) + "}"
    with duckdb.connect(":memory:") as connection:
        try:
            connection.execute(
                "CREATE TABLE csv_rows AS SELECT * FROM read_csv(?, header = false, "
                "auto_detect = false, all_varchar = true, delim = ',', quote = '\"', "
                "escape = '\"', allow_quoted_nulls = false, new_line = '\\n', "
                f"max_line_size = {max(normalized.stat().st_size, 2_097_152) + 1}, "
                f"parallel = false, columns = {columns})",
                [str(normalized)],
            )
            read = connection.execute("SELECT count(*) FROM csv_rows").fetchone()
            if read is None or read[0] != count:
                logger.warning("csv: %s records parsed, DuckDB read %s", count, read)
                raise IngestionError("failed to parse csv content")
            kinds = _csv_kinds(connection, physical)
            select = []
            for name, column in zip(names, physical, strict=True):
                quoted = quote_identifier(column)
                kind = "String" if name in keep_text else kinds[column]
                expression = {
                    "Boolean": f"lower({quoted}) = 'true'",
                    "Int64": f"CAST({quoted} AS BIGINT)",
                    "Float64": f"CAST({quoted} AS DOUBLE)",
                }.get(kind, quoted)
                select.append(expression)
            # Every value is cast before the first record is given, so a value the type
            # cannot hold (_CSV_NOT_CAST) fails the parse, not a later batch.
            connection.execute(
                f"CREATE TABLE csv_typed AS SELECT {', '.join(select)} FROM csv_rows"
            )
            cursor = connection.execute("SELECT * FROM csv_typed")
            for rows in iter(lambda: cursor.fetchmany(batch_records), []):
                yield [dict(zip(names, row, strict=True)) for row in rows]
        except IngestionError:
            raise
        except duckdb.ConversionException as exc:
            # DuckDB's text names the spill file; the client gets a fixed sentence.
            logger.warning("csv: a value does not fit its column type: %s", exc)
            raise IngestionError(_CSV_NOT_CAST) from exc
        except duckdb.Error as exc:
            logger.warning("csv: DuckDB could not read the normalized file: %s", exc)
            raise IngestionError("failed to parse csv content") from exc


def _csv_kinds(connection: object, physical: list[str]) -> dict[str, str]:
    """Each text column's type by the ``_CSV_*`` rules, over all of its values."""
    import duckdb

    from ..tabular.sql import quote_identifier, quote_literal

    assert isinstance(connection, duckdb.DuckDBPyConnection)
    parts = []
    for column in physical:
        quoted = quote_identifier(column)
        for pattern in (_CSV_BOOLEAN, _CSV_INTEGER, f"({_CSV_INTEGER})|({_CSV_FLOAT})"):
            parts.append(f"bool_and(regexp_full_match({quoted}, {quote_literal(pattern)}))")
    row = connection.execute(f"SELECT {', '.join(parts)} FROM csv_rows").fetchone()
    assert row is not None
    kinds: dict[str, str] = {}
    for index, column in enumerate(physical):
        boolean, integer, number = row[index * 3 : index * 3 + 3]
        # bool_and of no values is null: a column without values is text.
        if boolean:
            kinds[column] = "Boolean"
        elif integer:
            kinds[column] = "Int64"
        elif number:
            kinds[column] = "Float64"
        else:
            kinds[column] = "String"
    return kinds


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

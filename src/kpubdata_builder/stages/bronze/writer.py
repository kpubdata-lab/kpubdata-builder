"""Writing Bronze records to disk as they arrive (#622).

Bronze used to hold every record of a source in memory — a tuple built after the last
page — and write it at the end. A 1.4 GiB upload or a long ``list_all`` held the whole
payload several times over. The writer takes records a batch at a time (a page, a
parsed chunk, a ``param_grid`` combination) and appends them to a staging file, so what
Bronze holds in memory is one batch.

The staging file is the **working copy**: each record exactly as the source gave it,
for the stages after Bronze to read. Exactly means two things plain JSON would lose:

- **Key order.** A table's column order and a struct's field order come from the
  source's key order, so keys are written as they came, not sorted.
- **Value types.** A provider or a Parquet file can hand over a ``date``, a
  ``datetime`` (with its time zone), a ``Decimal``, a tuple — values Silver infers
  types from. They are written as small tagged objects (:func:`encode_record`) and read
  back as the same values, not as their text. A dict that happens to use the tag key is
  itself wrapped, so no source data is ever read back as a tag. Nothing is unpickled:
  a working copy on disk is data, never code.

Line *n* is record *n*: the file order is the row ordinal (ADR 0021 D8), kept from the
first page of the first call to the last.

The persisted Bronze file (``raw_records.jsonl``, sorted keys) is written from the
working copy when the artifact is persisted, with the same bytes as before. A date, a
time, a datetime, a ``Decimal`` and ``bytes`` are written as their standard text
(#979); any other value JSON cannot hold (NaN #201, a ``timedelta``) fails the persist,
as it always did.

Every record is scrubbed of the requester's keys before it is written (#686). A writer
that is not committed removes everything it wrote.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import IO, cast
from zoneinfo import ZoneInfo

from ...spec import JsonValue

WORKING_NAME = "records.jsonl"

#: Marks a tagged value in the working copy. The NUL makes a collision with a real key
#: implausible; a dict that has it anyway is wrapped (see _encode), so none is misread.
_TAG = "\x00kpubdata"

Scrub = Callable[[JsonValue], JsonValue]


def encode_record(record: Mapping[str, object]) -> str:
    """One working-copy line: source key order, types kept, NaN allowed."""
    return json.dumps(_encode(dict(record)), ensure_ascii=False)


def decode_record(line: str) -> dict[str, JsonValue]:
    """The record :func:`encode_record` wrote, with its types."""
    return cast(dict[str, JsonValue], _decode(json.loads(line)))


def canonical_line(record: Mapping[str, JsonValue]) -> str:
    """A record as the persisted Bronze file holds it: sorted keys, no NaN (#201).

    A Parquet upload can hand the working copy native Python values the stdlib
    encoder does not know (#979). The working copy keeps their types via tags, and
    that is what Silver reads; the persisted file is a plain-JSON snapshot, so each is
    written as its one standard text (:func:`_plain`). Anything else still fails the
    persist with ``TypeError``, as it always did.
    """
    return json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False, default=_plain)


def _plain(value: object) -> str:
    """The text of a value plain JSON cannot hold, for the persisted file.

    Only types with one standard text are converted (the rule of
    ``exporters/_json_safe.py``): ISO 8601 for a date, a time and a datetime (``T``
    separator, numeric offset when it has a zone), the decimal text for a ``Decimal``,
    hex for ``bytes`` as in the working copy. A ``timedelta`` has no such text and is
    refused with everything else.
    """
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def read_records(path: Path) -> Iterator[dict[str, JsonValue]]:
    """The records of a working copy, one at a time."""
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield decode_record(line)


def _encode(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        if _TAG in value or not all(isinstance(key, str) for key in value):
            return {_TAG: "dict", "v": [[_encode(k), _encode(v)] for k, v in value.items()]}
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, tuple):
        return {_TAG: "tuple", "v": [_encode(item) for item in value]}
    if isinstance(value, dt.datetime):
        zone = value.tzinfo.key if isinstance(value.tzinfo, ZoneInfo) else None
        text = (value.replace(tzinfo=None) if zone else value).isoformat()
        return {_TAG: "datetime", "v": text, "zone": zone, "fold": value.fold}
    if isinstance(value, dt.date):
        return {_TAG: "date", "v": value.isoformat()}
    if isinstance(value, dt.time):
        return {_TAG: "time", "v": value.isoformat(), "fold": value.fold}
    if isinstance(value, dt.timedelta):
        return {_TAG: "timedelta", "v": [value.days, value.seconds, value.microseconds]}
    if isinstance(value, Decimal):
        return {_TAG: "decimal", "v": str(value)}
    if isinstance(value, bytes):
        return {_TAG: "bytes", "v": value.hex()}
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _decode(value: object) -> object:
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get(_TAG)
    if kind is None:
        return {key: _decode(item) for key, item in value.items()}
    payload = value["v"]
    if kind == "dict":
        return {_decode(k): _decode(v) for k, v in payload}
    if kind == "tuple":
        return tuple(_decode(item) for item in payload)
    if kind == "datetime":
        parsed = dt.datetime.fromisoformat(payload)
        if value.get("zone"):
            parsed = parsed.replace(tzinfo=ZoneInfo(value["zone"]))
        return parsed.replace(fold=value.get("fold", 0))
    if kind == "date":
        return dt.date.fromisoformat(payload)
    if kind == "time":
        return dt.time.fromisoformat(payload).replace(fold=value.get("fold", 0))
    if kind == "timedelta":
        days, seconds, micros = payload
        return dt.timedelta(days=days, seconds=seconds, microseconds=micros)
    if kind == "decimal":
        return Decimal(payload)
    if kind == "bytes":
        return bytes.fromhex(payload)
    raise ValueError(f"unknown value tag in a Bronze working copy: {kind!r}")


def new_staging_dir() -> Path:
    """A fresh private directory for a Bronze built outside a run (preview, library use)."""
    return Path(tempfile.mkdtemp(prefix="kpubdata-bronze-"))


class BronzeWriter:
    """Appends records to a source's staging directory; commit or abort, never neither.

    Use it as a context manager: leaving the block without committing — by an exception
    or otherwise — aborts, removing the staging directory, so a cancelled or failed fetch
    leaves no partial Bronze.
    """

    def __init__(self, staging_dir: Path, *, scrub: Scrub | None = None) -> None:
        """Start writing into ``staging_dir``, created if absent."""
        self._dir = staging_dir
        self._scrub = scrub
        staging_dir.mkdir(parents=True, exist_ok=True)
        self._handle: IO[str] | None = (staging_dir / WORKING_NAME).open("w", encoding="utf-8")
        self._count = 0
        self._committed = False

    @property
    def staging_dir(self) -> Path:
        return self._dir

    @property
    def record_count(self) -> int:
        """Records written so far."""
        return self._count

    def write_batch(self, records: Iterable[Mapping[str, JsonValue]]) -> int:
        """Append ``records`` in order; returns how many were written."""
        handle = self._open()
        written = 0
        for record in records:
            handle.write(encode_record(self._clean(record)))
            handle.write("\n")
            written += 1
        # A page is on disk once it is written, not when the fetch ends (#622): a later
        # page failing never takes an earlier one with it before the writer aborts.
        handle.flush()
        self._count += written
        return written

    def write_working_lines(self, path: Path) -> int:
        """Append a file of working-copy lines already scrubbed (a checkpoint fragment)."""
        handle = self._open()
        written = 0
        with path.open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                handle.write(line if line.endswith("\n") else line + "\n")
                written += 1
        self._count += written
        return written

    def commit(self) -> tuple[Path, int]:
        """Flush the working copy to disk; returns ``(path, record_count)``."""
        handle = self._open()
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        self._handle = None
        self._committed = True
        return self._dir / WORKING_NAME, self._count

    def abort(self) -> None:
        """Close and remove everything written. Safe to call more than once."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        shutil.rmtree(self._dir, ignore_errors=True)

    def __enter__(self) -> BronzeWriter:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None or not self._committed:
            self.abort()

    def _open(self) -> IO[str]:
        if self._handle is None:
            raise RuntimeError("the Bronze writer is already closed")
        return self._handle

    def _clean(self, record: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        if self._scrub is None:
            return record
        return cast(Mapping[str, JsonValue], self._scrub(dict(record)))


__all__ = [
    "WORKING_NAME",
    "BronzeWriter",
    "canonical_line",
    "decode_record",
    "encode_record",
    "new_staging_dir",
    "read_records",
]

"""Per-combination checkpoints for a ``param_grid`` fetch (#648, owner decision D4).

A long ``param_grid`` source — 1,500 combinations for Seoul rent — lost everything when
one call failed near the end. Each finished combination is now kept under the run
directory; rebuilding the same run id resumes from it and fetches only the
combinations that are missing.

The records and the bookkeeping are kept apart (#622), so a large combination is never
held in memory or packed into one JSON line::

    _checkpoints/<source>/
        000000.jsonl    one combination's records, working-copy lines
        000001.jsonl
        index.jsonl     one line per finished combination

- **A combination is finished when its index line exists.** Its fragment is written and
  flushed to disk first, then the index line is appended, so a crash between the two
  leaves a fragment no index line points to — ignored, and fetched again.
- **Matched by content, not by position alone.** An index line is reused only when its
  index and its parameters equal the combination the spec now expands to, and its
  fragment still has the recorded number of lines. Any mismatch means the spec changed
  or the files did, and the whole checkpoint is discarded rather than mixed in.
- **No key on disk.** Records are scrubbed of the requester's key values before they are
  written, as Bronze scrubs them before anything else is (#686).
- **A resumed run is not reproducible** (D4): its records came from two fetches at two
  times. Bronze reports how many combinations it took from a checkpoint, and the manifest
  records that, so the R1 comparison can leave the run out.

The checkpoint is removed once Bronze is written; a later rebuild starts fresh.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from types import TracebackType
from typing import IO, cast

from ...spec import JsonValue
from .models import CallTotal
from .writer import Scrub, encode_record

INDEX_NAME = "index.jsonl"


class Fragment:
    """One combination's records being written to its fragment file."""

    def __init__(self, path: Path, *, scrub: Scrub | None) -> None:
        self.path = path
        self._scrub = scrub
        self._handle: IO[str] | None = path.open("w", encoding="utf-8")
        self.record_count = 0

    def write_batch(self, records: Iterable[Mapping[str, JsonValue]]) -> None:
        handle = self._open()
        for record in records:
            cleaned = cast(
                Mapping[str, JsonValue],
                self._scrub(dict(record)) if self._scrub is not None else record,
            )
            handle.write(encode_record(cleaned))
            handle.write("\n")
            self.record_count += 1

    def close(self) -> None:
        handle = self._open()
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        self._handle = None

    def discard(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> Fragment:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.discard()

    def _open(self) -> IO[str]:
        if self._handle is None:
            raise RuntimeError("the checkpoint fragment is already closed")
        return self._handle


class CombinationCheckpoint:
    """The finished combinations of one source's ``param_grid`` fetch in one run."""

    def __init__(self, directory: Path, *, scrub: Scrub | None = None) -> None:
        self._dir = directory
        self._scrub = scrub

    @property
    def path(self) -> Path:
        return self._dir

    def load(
        self, combinations: Sequence[dict[str, JsonValue]]
    ) -> dict[int, tuple[Path, CallTotal]]:
        """Finished combinations that still match the spec: index → (fragment, total).

        An unreadable index line ends the read there — the last append may have been
        cut short by the crash that makes a resume necessary. A line whose parameters or
        fragment do not match discards the whole checkpoint.
        """
        index_path = self._dir / INDEX_NAME
        if not index_path.is_file():
            return {}
        done: dict[int, tuple[Path, CallTotal]] = {}
        with index_path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    break
                index = entry.get("index")
                fragment = self._dir / _fragment_name(index) if isinstance(index, int) else None
                if (
                    fragment is None
                    or not 0 <= cast(int, index) < len(combinations)
                    or entry.get("params") != _as_json(combinations[cast(int, index)])
                    or entry.get("fragment") != fragment.name
                    or _line_count(fragment) != entry.get("record_count")
                ):
                    self.remove()
                    return {}
                done[cast(int, index)] = (fragment, CallTotal(**entry["call_total"]))
        return done

    def fragment(self, index: int) -> Fragment:
        """Start writing combination ``index``'s records."""
        self._dir.mkdir(parents=True, exist_ok=True)
        return Fragment(self._dir / _fragment_name(index), scrub=self._scrub)

    def finish(
        self,
        fragment: Fragment,
        *,
        index: int,
        params: dict[str, JsonValue],
        call_total: CallTotal,
    ) -> None:
        """Mark a combination finished: its fragment on disk first, then its index line."""
        fragment.close()
        line = json.dumps(
            {
                "index": index,
                "params": params,
                "fragment": fragment.path.name,
                "record_count": fragment.record_count,
                "call_total": asdict(call_total),
            },
            ensure_ascii=False,
            default=str,
        )
        with (self._dir / INDEX_NAME).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def append(
        self,
        index: int,
        params: dict[str, JsonValue],
        records: Iterable[Mapping[str, JsonValue]],
        call_total: CallTotal,
    ) -> None:
        """Record one finished combination whose records are already in hand."""
        with self.fragment(index) as fragment:
            fragment.write_batch(records)
            self.finish(fragment, index=index, params=params, call_total=call_total)

    def remove(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


def _fragment_name(index: int) -> str:
    return f"{index:06d}.jsonl"


def _line_count(path: Path) -> int | None:
    if not path.is_file():
        return None
    with path.open("rb") as handle:
        return sum(1 for line in handle if line.strip())


def _as_json(value: dict[str, JsonValue]) -> JsonValue:
    """What ``value`` reads back as from JSON — tuples become lists — for comparison."""
    return cast(JsonValue, json.loads(json.dumps(value, ensure_ascii=False, default=str)))


__all__ = ["INDEX_NAME", "CombinationCheckpoint", "Fragment"]

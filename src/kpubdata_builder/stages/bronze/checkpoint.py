"""Per-combination checkpoints for a ``param_grid`` fetch (#648, owner decision D4).

A long ``param_grid`` source — 1,500 combinations for Seoul rent — lost everything when
one call failed near the end. Each finished combination is now appended to a JSONL file
under the run directory; rebuilding the same run id resumes from it and fetches only the
combinations that are missing.

- **Append-only, one line per combination.** Nothing is rewritten, so checkpointing is
  O(n) over a fetch rather than O(n²).
- **Matched by content, not by position alone.** A line is reused only when its index
  and its parameters equal the combination the spec now expands to. Any mismatch means
  the spec changed, and the whole checkpoint is discarded rather than mixed in.
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
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import cast

from ...spec import JsonValue
from .models import CallTotal

Scrub = Callable[[JsonValue], JsonValue]


class CombinationCheckpoint:
    """The finished combinations of one source's ``param_grid`` fetch in one run."""

    def __init__(self, path: Path, *, scrub: Scrub | None = None) -> None:
        self._path = path
        self._scrub = scrub

    @property
    def path(self) -> Path:
        return self._path

    def load(
        self, combinations: Sequence[dict[str, JsonValue]]
    ) -> dict[int, tuple[list[dict[str, JsonValue]], CallTotal]]:
        """Finished combinations that still match the spec, by index.

        An unreadable line ends the read there — the last append may have been cut
        short by the crash that makes a resume necessary. A line whose parameters do not
        match discards the whole checkpoint.
        """
        if not self._path.is_file():
            return {}
        done: dict[int, tuple[list[dict[str, JsonValue]], CallTotal]] = {}
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    break
                index = entry.get("index")
                if (
                    not isinstance(index, int)
                    or not 0 <= index < len(combinations)
                    or entry.get("params") != _as_json(combinations[index])
                ):
                    self.remove()
                    return {}
                total = entry["call_total"]
                done[index] = (
                    cast(list[dict[str, JsonValue]], entry["records"]),
                    CallTotal(**total),
                )
        return done

    def append(
        self,
        index: int,
        params: dict[str, JsonValue],
        records: Sequence[dict[str, JsonValue]],
        call_total: CallTotal,
    ) -> None:
        """Record one finished combination, durably, before the next one starts."""
        stored: JsonValue = [dict(r) for r in records]
        if self._scrub is not None:
            stored = self._scrub(stored)
        line = json.dumps(
            {
                "index": index,
                "params": params,
                "records": stored,
                "call_total": asdict(call_total),
            },
            ensure_ascii=False,
            default=str,
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def remove(self) -> None:
        self._path.unlink(missing_ok=True)


def _as_json(value: dict[str, JsonValue]) -> JsonValue:
    """What ``value`` reads back as from JSON — tuples become lists — for comparison."""
    return cast(JsonValue, json.loads(json.dumps(value, ensure_ascii=False, default=str)))


__all__ = ["CombinationCheckpoint"]

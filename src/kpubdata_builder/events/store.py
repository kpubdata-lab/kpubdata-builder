"""Append-only SQLite-based run event store (#496).

Same SQLite concurrency pattern as ``store.build_index.BuildIndex`` (#309, ADR 0003)
(WAL mode, busy_timeout, thread-local connection, transaction) reused but
failure policy differs:

- ``BuildIndex`` is **derivative, rebuildable index** from ``manifest.json``, so write
  Failures can be swallowed (ADR 0003) — even if lost, can be recreated by filesystem scan.
- This store is **only canonical** for run event timeline — recreatable
  no canonical original. So ``append()`` propagates failures without swallowing
  (#496 "no lost events"). Callers in pipeline wrapping this store
  (``events.recorder.BuildEventRecorder``) absorbs that failure so it doesn't
  breach *other* canonicals (manifest,
  source outcome), recording via ``BuildManifest.warnings``
  exposes — store itself never silently hides its own failures.

Concurrency: Multiple sources run in parallel via ``ThreadPoolExecutor`` (#247), each
event appending. ``AUTOINCREMENT`` primary key assigned by SQLite in commit order
global monotonic sequence; this single value preserves different threads' append order
without loss, without imposing causal ordering (#496 parallel
ordering policy).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from ..spec.models import JsonValue
from .models import BuildEvent, EventName, EventStatus, StageName

SCHEMA_VERSION = 1

_EVENTS_FILENAME = "_build_events.sqlite"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS build_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    source_key TEXT,
    stage TEXT,
    message TEXT,
    metrics TEXT
)
"""

_SELECT_COLUMNS = "seq, run_id, timestamp, event, status, source_key, stage, message, metrics"


class BuildEventStore:
    """Append-only event store for all runs under single output_root.

    Like ``BuildIndex``, separate SQLite file under output_root
    (``_build_events.sqlite``) — independent file from manifest.json/BuildIndex,
    so this store's schema changes don't affect other canonicals.
    """

    def __init__(self, output_root: Path, *, db_path: Path | None = None) -> None:
        self._output_root = output_root
        self._db_path = db_path if db_path is not None else output_root / _EVENTS_FILENAME
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return cast(sqlite3.Connection, self._local.conn)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path), timeout=30.0
        )  # busy_timeout: wait for concurrency contention
        conn.execute("PRAGMA journal_mode=WAL")  # Allow concurrent reads + parallel appends
        return conn

    def _init_db(self) -> None:
        with self._transaction():
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_version (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT DEFAULT (datetime('now'))
                )
                """
            )
            cur = self._conn.execute("SELECT version FROM schema_version")
            if cur.fetchone() is None:
                # v1 is first release — no destructive migration (DROP) needed. This store is
                # append-only canonical (unlike BuildIndex), so schema change never DROPs existing
                # event rows — future versions migrate only via ALTER/add new columns.
                self._conn.execute(
                    f"INSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION})"
                )
            self._conn.execute(_CREATE_TABLE_SQL)
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_build_events_run_seq ON build_events(run_id, seq)"
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def append(self, event: BuildEvent) -> BuildEvent:
        """Append event and return new instance with ``seq`` assigned by store.

        Does not swallow failures (#496) — this store is sole canonical of event timeline,
        so unlike ``BuildIndex`` (ADR 0003, derived index), silently ignoring write failures
        loses events permanently. Callers set failure handling policy.

        Raises:
            sqlite3.Error: Propagated as-is if recording fails.
        """
        timestamp = event.timestamp
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("event.timestamp must be timezone-aware")
        metrics_json = json.dumps(event.metrics) if event.metrics is not None else None
        with self._transaction():
            cur = self._conn.execute(
                """
                INSERT INTO build_events
                    (run_id, timestamp, event, status, source_key, stage, message, metrics)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.run_id,
                    timestamp.astimezone(timezone.utc).isoformat(),
                    event.event,
                    event.status,
                    event.source_key,
                    event.stage,
                    event.message,
                    metrics_json,
                ),
            )
            seq = cur.lastrowid
        if seq is None:  # pragma: no cover - sqlite3 always assigns lastrowid on INSERT
            raise RuntimeError("build event insert did not return a row id")
        return replace(event, seq=seq)

    def list_for_run(self, run_id: str, *, limit: int, tail: bool) -> tuple[BuildEvent, ...]:
        """Return up to ``limit`` events from single run in chronological ascending order.

        ``tail=False`` (default) starts from run beginning to ``limit`` items,
        ``tail=True`` picks most recent ``limit`` items, but return itself
        always ascending (#496 timeline rendering policy) — client
        doesn't need to flip each time. Bounded query: limit always passed as SQL ``LIMIT``,
        never reads entire table.
        """
        if tail:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM build_events "
                "WHERE run_id = ? ORDER BY seq DESC LIMIT ?",
                (run_id, limit),
            )
            rows = list(cur.fetchall())
            rows.reverse()
        else:
            cur = self._conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM build_events "
                "WHERE run_id = ? ORDER BY seq ASC LIMIT ?",
                (run_id, limit),
            )
            rows = list(cur.fetchall())
        return tuple(_row_to_event(row) for row in rows)

    def close(self) -> None:
        """Close connection (checkpoint WAL to main DB file first).

        Same pattern as ``BuildIndex.close()`` — safely move file in tests/redeployment.
        """
        if hasattr(self._local, "conn"):
            self._local.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._local.conn.close()
            delattr(self._local, "conn")


def _row_to_event(row: tuple[object, ...]) -> BuildEvent:
    seq, run_id, timestamp_text, event, status, source_key, stage, message, metrics_json = row
    metrics: dict[str, JsonValue] | None = None
    if metrics_json is not None:
        try:
            parsed = json.loads(cast(str, metrics_json))
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            metrics = cast(dict[str, JsonValue], parsed)
    return BuildEvent(
        seq=cast(int, seq),
        timestamp=datetime.fromisoformat(cast(str, timestamp_text)),
        run_id=cast(str, run_id),
        event=cast(EventName, event),
        status=cast(EventStatus, status),
        source_key=cast("str | None", source_key),
        stage=cast("StageName | None", stage),
        message=cast("str | None", message),
        metrics=metrics,
    )


__all__ = ["BuildEventStore", "SCHEMA_VERSION"]

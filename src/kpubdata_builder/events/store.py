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
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS, enable_wal

from ..spec.models import JsonValue
from ..store.schema_version import (
    UnsupportedSchemaVersionError,
    open_read_only,
    says_damaged,
    stored_version,
)
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

# Who submitted an async run, and when (#996). The event timeline says what happened to
# a run but not whose it is; ownership lived in the job registry (memory) until the run
# wrote a manifest. A run a restart interrupts has neither, so its owner could not read
# why it ended. Kept here, next to the events it belongs with; rows are only added.
_CREATE_SUBMISSIONS_SQL = """
CREATE TABLE IF NOT EXISTS run_submissions (
    run_id TEXT PRIMARY KEY,
    owner_id TEXT,
    created_by TEXT,
    submitted_at TEXT NOT NULL
)
"""


# How a run that left no manifest ended (#1120): cancelled before it produced one, or
# failed without ever running to its end — a restart, a shutdown, keys that were gone.
# The terminal event says so to the run's owner, in a message written for them. This is
# the record the build index is filled from, for a run it has no manifest to read: a
# stable code, the stage reached and a sentence made of fixed phrases
# (``manifest.endings``). One row per run, written before the terminal event and never
# rewritten.
_CREATE_ENDINGS_SQL = """
CREATE TABLE IF NOT EXISTS run_endings (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    code TEXT NOT NULL,
    summary TEXT NOT NULL,
    recorded_at TEXT NOT NULL
)
"""


def events_store_path(output_root: Path) -> Path:
    """Where the event store of ``output_root`` is, whether or not it exists yet."""
    return output_root / _EVENTS_FILENAME


@dataclass(frozen=True, slots=True)
class RunSubmission:
    """Who submitted an async run and when (#996)."""

    run_id: str
    owner_id: str | None
    created_by: str | None
    submitted_at: str
    #: The earlier run this one retries (#1042), when the submitter named one.
    retry_of: str | None = None


@dataclass(frozen=True, slots=True)
class RunEnding:
    """How a run that left no manifest ended (#1120).

    ``code``, ``stage`` and ``summary`` are ``manifest.endings`` vocabulary: nothing in
    them comes from an exception, an event's message, a spec or a request.
    """

    run_id: str
    status: str
    stage: str
    code: str
    summary: str
    recorded_at: str
    #: Who submitted the run, from its submission record; None when there is none.
    owner_id: str | None = None
    created_by: str | None = None


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
        # Before the first ordinary connection, which sets the journal mode.
        self._refuse_newer(stored_version(self._db_path))
        self._init_db()

    def _refuse_newer(self, found: int | None) -> None:
        """Refuse a store a newer release wrote (#1096).

        Canonical and append-only: there is nothing to rebuild it from, so it is not
        read as this version's.
        """
        if found is None or found <= SCHEMA_VERSION:
            return
        raise UnsupportedSchemaVersionError(
            store="run event store",
            location=str(self._db_path),
            found=found,
            supported=SCHEMA_VERSION,
            remedy=(
                "Run the release that wrote it, or restore the output directory from a "
                "backup taken before the upgrade; the events cannot be rebuilt from "
                "anything else."
            ),
        )

    @property
    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = self._connect()
        return cast(sqlite3.Connection, self._local.conn)

    def _connect(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), timeout=BUSY_TIMEOUT_SECONDS)
        enable_wal(conn)  # Allow concurrent reads + parallel appends
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
            cur = self._conn.execute("SELECT MAX(version) FROM schema_version")
            found = cur.fetchone()[0]
            # Again inside the transaction: a store made between the check above and
            # here is refused with nothing of this version added to it.
            self._refuse_newer(int(found) if found is not None else None)
            if found is None:
                # v1 is first release — no destructive migration (DROP) needed. This store is
                # append-only canonical (unlike BuildIndex), so schema change never DROPs existing
                # event rows — future versions migrate only via ALTER/add new columns.
                self._conn.execute(
                    f"INSERT INTO schema_version (version) VALUES ({SCHEMA_VERSION})"
                )
            self._conn.execute(_CREATE_TABLE_SQL)
            self._conn.execute(_CREATE_SUBMISSIONS_SQL)
            # Added later (#1120); a store made before it gains an empty table.
            self._conn.execute(_CREATE_ENDINGS_SQL)
            # ``retry_of`` came later (#1042). The table only gains a column: rows are
            # never rewritten, and a store made before it reads the column as NULL.
            columns = {
                str(row[1])
                for row in self._conn.execute("PRAGMA table_info(run_submissions)").fetchall()
            }
            if "retry_of" not in columns:
                self._conn.execute("ALTER TABLE run_submissions ADD COLUMN retry_of TEXT")
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

    def record_submission(
        self,
        run_id: str,
        *,
        owner_id: str | None,
        created_by: str | None,
        submitted_at: datetime,
        retry_of: str | None = None,
    ) -> None:
        """Remember who submitted ``run_id`` (#996). A second submission of the id is ignored."""
        if submitted_at.tzinfo is None or submitted_at.utcoffset() is None:
            raise ValueError("submitted_at must be timezone-aware")
        with self._transaction():
            self._conn.execute(
                "INSERT OR IGNORE INTO run_submissions"
                " (run_id, owner_id, created_by, submitted_at, retry_of) VALUES (?, ?, ?, ?, ?)",
                (
                    run_id,
                    owner_id,
                    created_by,
                    submitted_at.astimezone(timezone.utc).isoformat(),
                    retry_of,
                ),
            )

    def submission(self, run_id: str) -> RunSubmission | None:
        """The recorded submitter of ``run_id``, or None when it was never recorded."""
        row = self._conn.execute(
            "SELECT owner_id, created_by, submitted_at, retry_of FROM run_submissions"
            " WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunSubmission(
            run_id=run_id,
            owner_id=None if row[0] is None else str(row[0]),
            created_by=None if row[1] is None else str(row[1]),
            submitted_at=str(row[2]),
            retry_of=None if row[3] is None else str(row[3]),
        )

    def record_ending(
        self, run_id: str, *, status: str, stage: str, code: str, summary: str, at: datetime
    ) -> bool:
        """Record how ``run_id`` ended without a manifest (#1120).

        The first record of a run stays: a run id is one attempt (#1042), and a second
        ending for it — a restart marking a run whose record was written just before the
        process died — changes nothing. Returns whether this call wrote the row.
        """
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        with self._transaction():
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO run_endings"
                " (run_id, status, stage, code, summary, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, status, stage, code, summary, at.astimezone(timezone.utc).isoformat()),
            )
            return cur.rowcount == 1

    def ending(self, run_id: str) -> RunEnding | None:
        """The recorded ending of ``run_id`` with who submitted it, or None."""
        row = self._conn.execute(f"{_SELECT_ENDINGS} WHERE e.run_id = ?", (run_id,)).fetchone()
        return None if row is None else _row_to_ending(row)

    def terminal_event(self, run_id: str) -> BuildEvent | None:
        """The run's last terminal event (finished, failed or cancelled), if it has one."""
        events = [
            event
            for event in self.list_for_run(run_id, limit=50, tail=True)
            if event.event in ("run_finished", "run_failed", "run_cancelled")
        ]
        return events[-1] if events else None

    def unfinished_runs(self) -> tuple[str, ...]:
        """Runs with a submission or start event and no terminal event (#683)."""
        rows = self._conn.execute(
            "SELECT run_id FROM build_events GROUP BY run_id HAVING"
            " SUM(event IN ('run_submitted', 'run_started')) > 0 AND"
            " SUM(event IN ('run_finished', 'run_failed', 'run_cancelled')) = 0"
            " ORDER BY run_id"
        ).fetchall()
        return tuple(str(row[0]) for row in rows)

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


_SELECT_ENDINGS = (
    "SELECT e.run_id, e.status, e.stage, e.code, e.summary, e.recorded_at,"
    " s.owner_id, s.created_by FROM run_endings e"
    " LEFT JOIN run_submissions s ON s.run_id = e.run_id"
)


def _row_to_ending(row: tuple[object, ...]) -> RunEnding:
    return RunEnding(
        run_id=str(row[0]),
        status=str(row[1]),
        stage=str(row[2]),
        code=str(row[3]),
        summary=str(row[4]),
        recorded_at=str(row[5]),
        owner_id=None if row[6] is None else str(row[6]),
        created_by=None if row[7] is None else str(row[7]),
    )


def read_run_endings(output_root: Path) -> tuple[RunEnding, ...]:
    """Every recorded ending under ``output_root``, read without changing the store.

    For a rebuild of the build index (#1120), which may run while a server has the
    store open and must not create or alter it: the connection is read-only. Empty
    when there is no store, or it has no such table yet — one written before this
    record existed.

    Raises:
        sqlite3.Error: The store could not be read for another reason. A rebuild that
            went on would drop every such run from the index without saying so.
    """
    path = events_store_path(output_root)
    if not path.is_file():
        return ()
    try:
        with closing(open_read_only(path)) as conn:
            rows = conn.execute(f"{_SELECT_ENDINGS} ORDER BY e.run_id").fetchall()
    except sqlite3.Error as exc:
        if says_damaged(exc):
            return ()
        raise
    return tuple(_row_to_ending(row) for row in rows)


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


__all__ = [
    "SCHEMA_VERSION",
    "BuildEventStore",
    "RunEnding",
    "RunSubmission",
    "events_store_path",
    "read_run_endings",
]

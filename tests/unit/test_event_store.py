"""BuildEvent model/BuildEventStore repository unit test (#496).

HTTP API/pipeline integration is covered by test_events_api.py/test_pipeline_events.py respectively.
This file purely verifies store's append-only contract, ordering, concurrency, bounded query, and timezone-aware timestamps.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder.events import BuildEvent, BuildEventStore


class _FailingConnection:
    """Fake connection that always fails ``execute`` (sqlite3.Connection is immutable)."""

    def execute(self, *_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("simulated append failure")

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _SpyConnection:
    """Wrapper that delegates to real connection and records only executed SQL text."""

    def __init__(self, real: sqlite3.Connection) -> None:
        self._real = real
        self.executed_sql: list[str] = []

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        self.executed_sql.append(sql)
        return self._real.execute(sql, params)

    def commit(self) -> None:
        self._real.commit()

    def rollback(self) -> None:
        self._real.rollback()


def _event(
    run_id: str = "r1",
    *,
    event: str = "run_started",
    status: str = "ok",
    source_key: str | None = None,
    stage: str | None = None,
    message: str | None = None,
    metrics: dict[str, object] | None = None,
    timestamp: datetime | None = None,
) -> BuildEvent:
    return BuildEvent(
        seq=0,
        timestamp=timestamp or datetime.now(tz=timezone.utc),
        run_id=run_id,
        event=event,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        source_key=source_key,
        stage=stage,  # type: ignore[arg-type]
        message=message,
        metrics=metrics,
    )


class TestAppend:
    def test_append_assigns_monotonic_seq(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        first = store.append(_event())
        second = store.append(_event())
        assert first.seq == 1
        assert second.seq == 2

    def test_append_many_events_all_persisted(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        for i in range(50):
            store.append(_event(message=f"event-{i}"))
        events = store.list_for_run("r1", limit=1000, tail=False)
        assert len(events) == 50
        assert [e.message for e in events] == [f"event-{i}" for i in range(50)]

    def test_append_does_not_overwrite_previous_events(self, tmp_path: Path) -> None:
        """append never overwrites existing rows — a new row is added each time."""
        store = BuildEventStore(tmp_path)
        store.append(_event(message="first"))
        store.append(_event(message="second"))
        events = store.list_for_run("r1", limit=10, tail=False)
        assert [e.message for e in events] == ["first", "second"]

    def test_timestamp_round_trips_as_timezone_aware(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        store.append(_event())
        (event,) = store.list_for_run("r1", limit=10, tail=False)
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset() is not None

    def test_naive_timestamp_is_rejected(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        naive = _event(timestamp=datetime.now())  # noqa: DTZ005 - intentionally naive
        with pytest.raises(ValueError, match="timezone-aware"):
            store.append(naive)

    def test_metrics_round_trip(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        store.append(_event(metrics={"rows": 84321, "ok": True}))
        (event,) = store.list_for_run("r1", limit=10, tail=False)
        assert event.metrics == {"rows": 84321, "ok": True}

    def test_metrics_none_round_trips_as_none(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        store.append(_event(metrics=None))
        (event,) = store.list_for_run("r1", limit=10, tail=False)
        assert event.metrics is None

    def test_append_failure_propagates_not_swallowed(self, tmp_path: Path) -> None:
        """store.append() is the single source of truth for event timeline; does not swallow failures.

        Unlike BuildIndex (ADR 0003, derived index), this store does not silently
        swallow failures but propagates exceptions directly — the recorder (pipeline-side wrapper)
        decides how to handle that failure (#496, paired with recorder swallow test in test_pipeline_events.py).

        ``sqlite3.Connection``/``Cursor`` are C extension types and cannot be monkeypatched directly
        (immutable type), so thread-local connection slots are replaced with fake connections to inject failures.
        """
        store = BuildEventStore(tmp_path)
        store._local.conn = _FailingConnection()
        with pytest.raises(sqlite3.OperationalError, match="simulated append failure"):
            store.append(_event())


class TestOrdering:
    def test_same_timestamp_events_still_ordered_by_seq(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        same_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.append(_event(message="a", timestamp=same_ts))
        store.append(_event(message="b", timestamp=same_ts))
        store.append(_event(message="c", timestamp=same_ts))
        events = store.list_for_run("r1", limit=10, tail=False)
        assert [e.message for e in events] == ["a", "b", "c"]
        assert [e.seq for e in events] == sorted(e.seq for e in events)

    def test_events_from_different_runs_are_isolated(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        store.append(_event(run_id="r1", message="r1-a"))
        store.append(_event(run_id="r2", message="r2-a"))
        store.append(_event(run_id="r1", message="r1-b"))
        r1_events = store.list_for_run("r1", limit=10, tail=False)
        assert [e.message for e in r1_events] == ["r1-a", "r1-b"]

    def test_concurrent_append_no_lost_events(self, tmp_path: Path) -> None:
        """Events are not lost even when multiple threads append concurrently (#496).

        Simulates source workers (#247) executed in parallel via ThreadPoolExecutor.
        """
        store = BuildEventStore(tmp_path)
        threads_count = 8
        events_per_thread = 25
        errors: list[BaseException] = []

        def _worker(worker_id: int) -> None:
            try:
                for i in range(events_per_thread):
                    store.append(_event(message=f"w{worker_id}-{i}", source_key=f"w{worker_id}"))
            except BaseException as exc:  # noqa: BLE001 - collect exception raised in thread
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(threads_count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        events = store.list_for_run("r1", limit=10_000, tail=False)
        assert len(events) == threads_count * events_per_thread
        seqs = [e.seq for e in events]
        assert len(seqs) == len(set(seqs))  # All seqs are unique — no loss/duplication
        assert seqs == sorted(seqs)  # append order (global monotonic order) is preserved

        for worker_id in range(threads_count):
            worker_messages = [e.message for e in events if e.source_key == f"w{worker_id}"]
            assert worker_messages == [f"w{worker_id}-{i}" for i in range(events_per_thread)]


class TestLimitAndTail:
    def _seed(self, store: BuildEventStore, count: int) -> None:
        for i in range(count):
            store.append(_event(message=f"e{i}"))

    def test_default_returns_from_start_ascending(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        self._seed(store, 10)
        events = store.list_for_run("r1", limit=5, tail=False)
        assert [e.message for e in events] == [f"e{i}" for i in range(5)]

    def test_tail_returns_most_recent_but_ascending(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        self._seed(store, 10)
        events = store.list_for_run("r1", limit=3, tail=True)
        assert [e.message for e in events] == ["e7", "e8", "e9"]
        assert [e.seq for e in events] == sorted(e.seq for e in events)

    def test_limit_larger_than_available_returns_all(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        self._seed(store, 3)
        events = store.list_for_run("r1", limit=1000, tail=False)
        assert len(events) == 3

    def test_empty_run_returns_empty_tuple(self, tmp_path: Path) -> None:
        store = BuildEventStore(tmp_path)
        events = store.list_for_run("no-such-run", limit=100, tail=False)
        assert events == ()

    def test_bounded_query_uses_sql_limit(self, tmp_path: Path) -> None:
        """Even in large runs, limit is applied at SQL level without reading entire table.

        ``sqlite3.Connection`` is an immutable C type and cannot be monkeypatched directly,
        so thread-local connection slots are replaced with a spy that delegates to the real connection
        and records only executed SQL.
        """
        store = BuildEventStore(tmp_path)
        self._seed(store, 500)
        spy = _SpyConnection(store._conn)
        store._local.conn = spy

        events = store.list_for_run("r1", limit=5, tail=False)
        assert len(events) == 5
        assert any("LIMIT" in sql for sql in spy.executed_sql)


class TestModelVocabulary:
    def test_build_event_is_frozen(self, tmp_path: Path) -> None:
        event = _event()
        with pytest.raises(AttributeError):
            event.status = "fail"  # type: ignore[misc]

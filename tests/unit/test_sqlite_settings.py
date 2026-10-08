"""Putting a new store in WAL mode when another connection is doing the same (#1210).

A database file is in rollback mode until one connection changes it. Two connections
that open a new store at once both try, and SQLite refuses the second at once — it does
not wait the busy timeout for the exclusive lock the change needs — so one of two first
openers of a store failed with ``database is locked``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from kpubdata_builder import sqlite_settings
from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS, enable_wal
from kpubdata_builder.store.build_index import SqliteBuildIndex


class _Refusing:
    """A connection whose journal mode cannot be changed for the first ``refusals`` tries."""

    def __init__(self, refusals: int, message: str = "database is locked") -> None:
        self.refusals = refusals
        self.message = message
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)
        if len(self.statements) <= self.refusals:
            raise sqlite3.OperationalError(self.message)


def _enable(connection: _Refusing) -> None:
    enable_wal(connection)  # type: ignore[arg-type]


def test_it_sets_the_mode_on_a_real_connection(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "store.sqlite")
    try:
        enable_wal(connection)

        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_it_tries_again_while_another_connection_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite_settings.time, "sleep", lambda _seconds: None)
    connection = _Refusing(refusals=3)

    _enable(connection)

    assert connection.statements == ["PRAGMA journal_mode=WAL"] * 4


def test_it_gives_up_when_the_wait_for_a_lock_has_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, BUSY_TIMEOUT_SECONDS / 2, BUSY_TIMEOUT_SECONDS])
    monkeypatch.setattr(sqlite_settings.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(sqlite_settings.time, "sleep", lambda _seconds: None)
    connection = _Refusing(refusals=100)

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _enable(connection)

    # Once inside the wait, once at its end: not for ever.
    assert len(connection.statements) == 2


def test_it_does_not_try_again_for_another_reason() -> None:
    connection = _Refusing(refusals=1, message="disk I/O error")

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        _enable(connection)

    assert len(connection.statements) == 1


def _open_together(root: Path, openers: int) -> list[BaseException]:
    """Open the build index of ``root`` from ``openers`` threads at once; what failed."""
    together = threading.Barrier(openers)
    failures: list[BaseException] = []

    def open_it() -> None:
        try:
            together.wait(timeout=10)
            SqliteBuildIndex(root).close(checkpoint=False)
        except BaseException as error:  # noqa: BLE001 - returned to the caller
            failures.append(error)

    threads = [threading.Thread(target=open_it) for _ in range(openers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return failures


def test_openers_of_a_store_that_is_not_there_yet_all_get_it(tmp_path: Path) -> None:
    """The race itself: without the retry, some rounds lose an opener."""
    for round_number in range(40):
        root = tmp_path / f"round-{round_number}"
        root.mkdir()

        assert _open_together(root, openers=8) == [], f"round {round_number}"

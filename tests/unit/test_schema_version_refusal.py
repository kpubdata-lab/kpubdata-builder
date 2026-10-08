"""A state store a newer release wrote is refused and left as it is (#1096).

Rolling a deployment back is when this happens. The event store took any version as
its own, and the build index dropped its table for any version but its own — a newer
one too — so the newer release came back to an empty index and nothing had said so.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.events import BuildEventStore
from kpubdata_builder.events.store import SCHEMA_VERSION as EVENTS_VERSION
from kpubdata_builder.events.store import events_store_path
from kpubdata_builder.service import BuilderService
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.store import SCHEMA_VERSION as INDEX_VERSION
from kpubdata_builder.store import SqliteBuildIndex, rebuild_index
from kpubdata_builder.store.build_index import BuildEntry, BuildStatus, _iter_manifest_entries
from kpubdata_builder.store.schema_version import UnsupportedSchemaVersionError


def _no_client(**_kwargs: object) -> SourceClient:
    raise AssertionError("no provider is called here")


def _set_version(path: Path, version: int) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE schema_version SET version = ?", (version,))


def _dump(path: Path) -> list[str]:
    """Every table's definition and rows — what the file holds, as text."""
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())


def _index_with_one_run(root: Path) -> Path:
    index = SqliteBuildIndex(root)
    index.insert_or_replace(
        run_id="run-1",
        status="ok",
        started_at="2026-01-01T10:00:00Z",
        finished_at="2026-01-01T10:05:00Z",
        owner_id="oidc:a",
    )
    index.close()
    return root / "_builds.sqlite"


def _events_with_one_submission(root: Path) -> Path:
    store = BuildEventStore(root)
    store.record_submission(
        "run-1",
        owner_id="oidc:a",
        created_by="oidc:a",
        submitted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.close()
    return events_store_path(root)


def test_newer_build_index_is_refused_and_not_dropped(tmp_path: Path) -> None:
    path = _index_with_one_run(tmp_path)
    _set_version(path, INDEX_VERSION + 1)
    before = _dump(path)

    with pytest.raises(UnsupportedSchemaVersionError) as refusal:
        SqliteBuildIndex(tmp_path)

    assert refusal.value.found == INDEX_VERSION + 1
    assert refusal.value.supported == INDEX_VERSION
    assert "rebuild-index" in str(refusal.value)
    assert _dump(path) == before


def test_older_build_index_is_still_recreated(tmp_path: Path) -> None:
    """Unchanged: the index is derived, and an older one is made again."""
    path = _index_with_one_run(tmp_path)
    _set_version(path, INDEX_VERSION - 1)

    index = SqliteBuildIndex(tmp_path)

    assert index.get("run-1") is None
    index.close()


def test_rebuild_index_is_the_way_out_of_a_newer_index(tmp_path: Path) -> None:
    path = _index_with_one_run(tmp_path)
    _set_version(path, INDEX_VERSION + 1)

    assert rebuild_index(tmp_path) == 0

    index = SqliteBuildIndex(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchall() == [(INDEX_VERSION,)]
    index.close()


def test_newer_event_store_is_refused_and_left_as_it_is(tmp_path: Path) -> None:
    path = _events_with_one_submission(tmp_path)
    _set_version(path, EVENTS_VERSION + 1)
    before = _dump(path)

    with pytest.raises(UnsupportedSchemaVersionError) as refusal:
        BuildEventStore(tmp_path)

    assert refusal.value.found == EVENTS_VERSION + 1
    assert "cannot be rebuilt" in str(refusal.value)
    assert _dump(path) == before


def test_refusing_a_newer_event_store_adds_nothing_to_it(tmp_path: Path) -> None:
    """A store with only what the newer release would have: no table is added to it."""
    path = events_store_path(tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (EVENTS_VERSION + 1,))
    before = _dump(path)

    with pytest.raises(UnsupportedSchemaVersionError):
        BuildEventStore(tmp_path)

    assert _dump(path) == before


def test_current_stores_open_as_before(tmp_path: Path) -> None:
    _index_with_one_run(tmp_path)
    _events_with_one_submission(tmp_path)

    index = SqliteBuildIndex(tmp_path)
    store = BuildEventStore(tmp_path)

    assert index.get("run-1") is not None
    assert store.submission("run-1") is not None
    index.close()
    store.close()


def test_service_opens_an_existing_event_store_when_it_is_made(tmp_path: Path) -> None:
    """Not on the first build: the store is opened lazily only when there is none yet."""
    path = _events_with_one_submission(tmp_path)
    _set_version(path, EVENTS_VERSION + 1)

    with pytest.raises(UnsupportedSchemaVersionError):
        BuilderService(output_root=tmp_path, client_factory=_no_client)


def test_service_still_leaves_no_event_store_behind(tmp_path: Path) -> None:
    BuilderService(output_root=tmp_path, client_factory=_no_client)

    assert not events_store_path(tmp_path).exists()


@pytest.mark.parametrize("make", [_index_with_one_run, _events_with_one_submission])
def test_serve_says_so_in_one_line_and_does_not_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    make: object,
) -> None:
    import kpubdata_builder.service.http as http_module

    started: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: started.append(service))
    path = make(tmp_path)  # type: ignore[operator]
    _set_version(path, 99)
    before = _dump(path)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "error: " in err
    assert "schema version 99" in err
    assert "Traceback" not in err
    assert started == []
    assert _dump(path) == before


def _journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


def _siblings(path: Path) -> set[str]:
    return {item.name for item in path.parent.iterdir()}


@pytest.mark.parametrize(
    ("make", "open_store"),
    [(_index_with_one_run, SqliteBuildIndex), (_events_with_one_submission, BuildEventStore)],
)
def test_refusal_does_not_change_the_journal_mode(
    tmp_path: Path, make: object, open_store: object
) -> None:
    """The version is read before the connection that sets WAL is opened.

    A release that keeps this store in another journal mode would otherwise find it
    switched, with ``-wal`` and ``-shm`` files beside it, by a release that refused it.
    """
    path = make(tmp_path)  # type: ignore[operator]
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    _set_version(path, 99)
    assert _journal_mode(path) == "delete"
    files = _siblings(path)

    with pytest.raises(UnsupportedSchemaVersionError):
        open_store(tmp_path)  # type: ignore[operator]

    assert _journal_mode(path) == "delete"
    assert _siblings(path) == files


def _write_run(root: Path, run_id: str, *, owner_id: str, errors: list[str]) -> None:
    import json

    run_dir = root / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "started_at": "2026-01-01T10:00:00Z",
                "finished_at": "2026-01-01T10:05:00Z",
                "owner_id": owner_id,
                "created_by": owner_id,
                "errors": errors,
            }
        ),
        encoding="utf-8",
    )


def test_older_index_is_rebuilt_from_the_manifests_before_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not left empty: the runs are in the index when the service is handed to serve."""
    import kpubdata_builder.service.http as http_module

    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])
    _write_run(tmp_path, "run-failed", owner_id="oidc:b", errors=["boom"])
    path = _index_with_one_run(tmp_path)
    _set_version(path, INDEX_VERSION - 1)
    seen: dict[str, object] = {}

    def fake_serve(service: object, **_kwargs: object) -> None:
        index = SqliteBuildIndex(tmp_path)
        seen["ok"] = index.get("run-ok")
        seen["failed"] = index.get("run-failed")
        seen["stale"] = index.get("run-1")
        index.close()

    monkeypatch.setattr(http_module, "serve", fake_serve)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    ok, failed = seen["ok"], seen["failed"]
    assert ok is not None and failed is not None
    assert (ok.status, ok.owner_id) == ("ok", "oidc:a")  # type: ignore[attr-defined]
    assert (failed.status, failed.owner_id) == ("failed", "oidc:b")  # type: ignore[attr-defined]
    # The row with no manifest behind it is gone: the index is what the manifests say.
    assert seen["stale"] is None
    assert "rebuilt the build index from the manifests: 2 run(s)" in capsys.readouterr().out


def test_missing_index_is_rebuilt_from_the_manifests(tmp_path: Path) -> None:
    from kpubdata_builder.store import bring_index_up_to_date

    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])

    assert bring_index_up_to_date(tmp_path) == 1

    index = SqliteBuildIndex(tmp_path)
    assert index.get("run-ok") is not None
    index.close()


def test_current_index_is_left_alone(tmp_path: Path) -> None:
    """No scan on an ordinary start, and a row is not dropped for lacking a manifest."""
    from kpubdata_builder.store import bring_index_up_to_date

    _index_with_one_run(tmp_path)

    assert bring_index_up_to_date(tmp_path) is None

    index = SqliteBuildIndex(tmp_path)
    assert index.get("run-1") is not None
    index.close()


def test_newer_index_is_not_rebuilt_on_start(tmp_path: Path) -> None:
    from kpubdata_builder.store import bring_index_up_to_date

    path = _index_with_one_run(tmp_path)
    _set_version(path, INDEX_VERSION + 1)
    before = _dump(path)

    assert bring_index_up_to_date(tmp_path) is None

    assert _dump(path) == before


_OLD_INDEX_LEFT_OPEN = """
import os, sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA wal_autocheckpoint=0")
conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
conn.execute("INSERT INTO schema_version (version) VALUES (?)", (int(sys.argv[2]),))
conn.execute("CREATE TABLE builds (run_id TEXT PRIMARY KEY, old_col TEXT)")
conn.executemany("INSERT INTO builds VALUES (?, 'x')", [(f"old-{i}",) for i in range(50)])
conn.commit()
os._exit(0)
"""


def test_rebuild_is_not_undone_by_the_wal_the_last_process_left(tmp_path: Path) -> None:
    """A process that ended without closing its connections leaves ``-wal`` behind.

    SQLite applies a ``-wal`` it finds beside a database to that database, so a new
    index renamed into place next to the old one's was read as the old one: version 4,
    then dropped as older and left empty, after ``serve`` had said it was rebuilt.
    """
    import subprocess
    import sys

    from kpubdata_builder.store import bring_index_up_to_date

    path = tmp_path / "_builds.sqlite"
    subprocess.run(
        [sys.executable, "-c", _OLD_INDEX_LEFT_OPEN, str(path), str(INDEX_VERSION - 1)],
        check=True,
    )
    # The old index is in the files the dead process left, not in the database file.
    assert Path(f"{path}-wal").stat().st_size > 0
    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])
    _write_run(tmp_path, "run-failed", owner_id="oidc:b", errors=["boom"])

    assert bring_index_up_to_date(tmp_path) == 2

    assert bring_index_up_to_date(tmp_path) is None
    index = SqliteBuildIndex(tmp_path)
    ok, failed = index.get("run-ok"), index.get("run-failed")
    assert ok is not None and ok.owner_id == "oidc:a"
    assert failed is not None and failed.status == "failed"
    assert index.get("old-0") is None
    index.close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchall() == [(INDEX_VERSION,)]
        assert conn.execute("SELECT COUNT(*) FROM builds").fetchone() == (2,)
    assert not [item.name for item in tmp_path.iterdir() if ".bak" in item.name]
    assert not [item.name for item in tmp_path.iterdir() if ".tmp" in item.name]


def test_rebuild_beside_an_open_index_is_seen_by_its_connections(tmp_path: Path) -> None:
    """``rebuild-index`` while the server runs (#1157).

    The new index used to be renamed into the old one's place. A thread of the server
    that already had a connection stayed on the file that was there, so a build it
    recorded afterwards was in a file nobody else read: the same list differed by which
    thread answered, until a restart.
    """
    import threading

    _write_run(tmp_path, "run-a", owner_id="oidc:a", errors=[])
    server = SqliteBuildIndex(tmp_path)
    server.insert_or_replace(
        run_id="no-manifest", status="ok", started_at=None, finished_at="2026-01-01T00:00:00Z"
    )

    assert rebuild_index(tmp_path) == 1

    # The server's open connection sees what the rebuild made of the index...
    assert server.get("run-a") is not None
    assert server.get("no-manifest") is None
    # ...and what it writes next is seen by a connection opened after the rebuild.
    _write_run(tmp_path, "run-b", owner_id="oidc:b", errors=[])
    server.insert_or_replace(
        run_id="run-b", status="ok", started_at=None, finished_at="2026-01-02T00:00:00Z"
    )
    seen: dict[str, bool] = {}

    def another_thread() -> None:
        seen["a"] = server.get("run-a") is not None
        seen["b"] = server.get("run-b") is not None

    worker = threading.Thread(target=another_thread)
    worker.start()
    worker.join()
    assert seen == {"a": True, "b": True}
    later = SqliteBuildIndex(tmp_path)
    assert later.get("run-b") is not None
    later.close()
    server.close()
    # No second file was made: the index is the one the server has open.
    assert not [item.name for item in tmp_path.iterdir() if item.name.endswith((".tmp", ".bak"))]


def test_rebuild_keeps_a_build_that_finished_while_it_was_scanning(tmp_path: Path) -> None:
    """In the index and not in the scan, with a manifest: it is not a stale row."""
    _write_run(tmp_path, "run-a", owner_id="oidc:a", errors=[])
    _write_run(tmp_path, "run-late", owner_id="oidc:b", errors=[])
    index = SqliteBuildIndex(tmp_path)
    index.insert_or_replace(
        run_id="run-late", status="ok", started_at=None, finished_at="2026-01-02T00:00:00Z"
    )
    scanned_before_it_finished = [
        entry for entry in _manifest_entries(tmp_path) if entry.run_id == "run-a"
    ]

    index.replace_contents(scanned_before_it_finished)

    assert index.get("run-a") is not None
    assert index.get("run-late") is not None
    index.close()


def test_failed_rebuild_in_place_leaves_the_index_as_it_was(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-a", owner_id="oidc:a", errors=[])
    path = _index_with_one_run(tmp_path)
    before = _dump(path)
    index = SqliteBuildIndex(tmp_path)
    entries = _manifest_entries(tmp_path)
    bad_status = cast(BuildStatus, "not-a-status")
    broken = [*entries, replace(entries[0], run_id="run-bad", status=bad_status)]

    with pytest.raises(sqlite3.IntegrityError):
        index.replace_contents(broken)

    index.close()
    assert _dump(path) == before


def _manifest_entries(root: Path) -> list[BuildEntry]:
    return list(_iter_manifest_entries(root))


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE builds",
        "ALTER TABLE builds RENAME COLUMN status TO state",
        "ALTER TABLE builds DROP COLUMN owner_id",
        "ALTER TABLE builds ADD COLUMN extra TEXT",
    ],
    ids=["table missing", "column renamed", "column dropped", "column added"],
)
def test_rebuild_recovers_an_index_of_this_version_that_cannot_be_written(
    tmp_path: Path, damage: str
) -> None:
    """The version says this release's and the table does not: still what a rebuild is for.

    Refilling in place fails on such a file, and the rebuild used to replace the file
    whatever was in it. It falls back to that.
    """
    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])
    path = _index_with_one_run(tmp_path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(damage)

    assert rebuild_index(tmp_path) == 1

    index = SqliteBuildIndex(tmp_path)
    recovered = index.get("run-ok")
    assert recovered is not None and recovered.owner_id == "oidc:a"
    assert index.get("run-1") is None
    index.close()
    assert not [item.name for item in tmp_path.iterdir() if item.name.endswith((".tmp", ".bak"))]


def test_rebuild_in_place_does_not_wait_for_a_reader(tmp_path: Path) -> None:
    """A server connection in a read transaction holds back a ``TRUNCATE`` checkpoint
    for the whole busy timeout; the rebuild leaves the WAL to the server instead."""
    import time

    _write_run(tmp_path, "run-a", owner_id="oidc:a", errors=[])
    path = _index_with_one_run(tmp_path)
    with closing(sqlite3.connect(path)) as reader:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM builds").fetchone()
        started = time.monotonic()

        assert rebuild_index(tmp_path) == 1

        assert time.monotonic() - started < 10


def test_rebuild_does_not_replace_the_file_because_the_index_was_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lock that is not released in time is an error, and the index is left alone.

    Falling back to a new file here would be the split of #1157 again, and silent: the
    server writing to the index is exactly the server that would be left on the old
    file.
    """
    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])
    path = _index_with_one_run(tmp_path)
    inode = path.stat().st_ino

    def impatient(self: SqliteBuildIndex) -> sqlite3.Connection:
        conn = sqlite3.connect(str(path), timeout=0.2)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    with closing(sqlite3.connect(path, isolation_level=None)) as server:
        server.execute("BEGIN IMMEDIATE")
        server.execute("INSERT INTO builds (run_id, status) VALUES ('run-server', 'ok')")
        monkeypatch.setattr(SqliteBuildIndex, "_connect", impatient)

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            rebuild_index(tmp_path)

        server.execute("COMMIT")
        monkeypatch.undo()
        # The same file, with what the server wrote and nothing of the rebuild.
        assert path.stat().st_ino == inode
        assert [row[0] for row in server.execute("SELECT run_id FROM builds ORDER BY run_id")] == [
            "run-1",
            "run-server",
        ]
    assert not [item.name for item in tmp_path.iterdir() if item.name.endswith((".tmp", ".bak"))]


def test_a_new_index_has_the_table_the_rebuild_looks_for(tmp_path: Path) -> None:
    """The column list is written twice; this is what keeps the two the same."""
    from kpubdata_builder.store.build_index import _has_this_releases_table

    path = _index_with_one_run(tmp_path)

    assert _has_this_releases_table(path)
    assert not _has_this_releases_table(tmp_path / "absent.sqlite")


_HOLD_THE_INDEX = """
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA locking_mode=EXCLUSIVE")
conn.execute("BEGIN EXCLUSIVE")
conn.execute("INSERT INTO builds (run_id, status) VALUES ('run-server', 'ok')")
print("held", flush=True)
time.sleep(float(sys.argv[2]))
conn.execute("COMMIT")
"""


@pytest.fixture
def short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """The read-only look at a store waits as long as a store does; not in a test."""
    from kpubdata_builder.store import schema_version

    monkeypatch.setattr(schema_version, "PROBE_TIMEOUT_SECONDS", 0.3)


def test_the_read_only_look_waits_as_long_as_the_stores_do() -> None:
    from kpubdata_builder.store import schema_version
    from kpubdata_builder.store.inventory import STORES

    assert max(store.timeout_seconds for store in STORES) == schema_version.PROBE_TIMEOUT_SECONDS


@pytest.mark.usefixtures("short_wait")
def test_rebuild_beside_a_process_that_holds_the_index_is_an_error(tmp_path: Path) -> None:
    """A real lock, and the whole of ``rebuild_index`` (#1157).

    Every read of the index before the rebuild — its version, then its table — meets
    the lock. Reading "no version" or "no table" out of that replaced the file, and the
    process that held it was left on the one that had been there.
    """
    import subprocess
    import sys

    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])
    path = _index_with_one_run(tmp_path)
    inode = path.stat().st_ino
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_INDEX, str(path), "60"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            rebuild_index(tmp_path)

        assert path.stat().st_ino == inode
        leftovers = [i.name for i in tmp_path.iterdir() if i.name.endswith((".tmp", ".bak"))]
        assert leftovers == []
    finally:
        holder.kill()
        holder.wait()
    # Nothing of the rebuild is in it: the run it would have added is not there.
    with closing(sqlite3.connect(path)) as conn:
        assert [r[0] for r in conn.execute("SELECT run_id FROM builds")] == ["run-1"]


@pytest.mark.usefixtures("short_wait")
def test_serve_says_a_locked_store_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Not a traceback: the operator is told which kind of thing went wrong (#1157)."""
    import subprocess
    import sys

    import kpubdata_builder.service.http as http_module

    started: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: started.append(service))
    path = _index_with_one_run(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_INDEX, str(path), "60"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"

        assert main(["serve", "--output-dir", str(tmp_path)]) == 1
    finally:
        holder.kill()
        holder.wait()

    err = capsys.readouterr().err
    assert "error: a state store could not be opened: database is locked" in err
    assert "Traceback" not in err
    assert started == []


@pytest.mark.usefixtures("short_wait")
@pytest.mark.usefixtures("short_wait")
def test_a_locked_store_has_no_version_to_report(tmp_path: Path) -> None:
    import subprocess
    import sys

    from kpubdata_builder.store.build_index import _has_this_releases_table
    from kpubdata_builder.store.schema_version import stored_version

    path = _index_with_one_run(tmp_path)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_THE_INDEX, str(path), "60"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            stored_version(path)
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            _has_this_releases_table(path)
    finally:
        holder.kill()
        holder.wait()


def test_a_file_that_is_not_a_database_has_no_version_and_no_table(tmp_path: Path) -> None:
    from kpubdata_builder.store.build_index import _has_this_releases_table
    from kpubdata_builder.store.schema_version import stored_version

    garbage = tmp_path / "_builds.sqlite"
    garbage.write_bytes(b"this is not a database" * 100)

    assert stored_version(garbage) is None
    assert not _has_this_releases_table(garbage)


def test_a_database_without_a_version_table_has_no_version(tmp_path: Path) -> None:
    from kpubdata_builder.store.schema_version import stored_version

    path = tmp_path / "other.sqlite"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("CREATE TABLE something (x)")

    assert stored_version(path) is None


def test_a_file_that_is_not_a_database_is_still_replaced(tmp_path: Path) -> None:
    _write_run(tmp_path, "run-ok", owner_id="oidc:a", errors=[])
    (tmp_path / "_builds.sqlite").write_bytes(b"this is not a database" * 100)

    assert rebuild_index(tmp_path) == 1

    index = SqliteBuildIndex(tmp_path)
    assert index.get("run-ok") is not None
    index.close()


def test_the_read_only_look_is_opened_with_that_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value reaches SQLite: thirty seconds, read-only, and nothing set on the file."""
    from kpubdata_builder.store import schema_version

    path = _index_with_one_run(tmp_path)
    opened: list[tuple[str, dict[str, object]]] = []
    real_connect: Callable[..., sqlite3.Connection] = sqlite3.connect

    def recording(database: str, **kwargs: object) -> sqlite3.Connection:
        opened.append((database, kwargs))
        return real_connect(database, **kwargs)

    monkeypatch.setattr(schema_version.sqlite3, "connect", recording)

    assert schema_version.stored_version(path) == INDEX_VERSION

    ((database, kwargs),) = opened
    assert database.endswith("?mode=ro")
    assert kwargs == {"uri": True, "timeout": 30.0}


def test_serve_keeps_the_traceback_of_a_database_error_that_is_not_about_reaching_the_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed migration is not "could not be opened"; it is raised as it is."""
    import kpubdata_builder.service.http as http_module
    from kpubdata_builder import store

    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: None)

    def failing(_root: Path) -> int | None:
        raise sqlite3.OperationalError("no such column: owner_id")

    monkeypatch.setattr(store, "bring_index_up_to_date", failing)

    with pytest.raises(sqlite3.OperationalError, match="no such column"):
        main(["serve", "--output-dir", str(tmp_path)])


@pytest.mark.parametrize(
    ("message", "unreachable"),
    [
        ("database is locked", True),
        ("database table is locked", True),
        ("unable to open database file", True),
        ("disk I/O error", True),
        ("attempt to write a readonly database", True),
        ("database or disk is full", True),
        # A word of those messages as the name of something else is not one of them.
        ("no such column: locked_at", False),
        ("database schema is locked: main", True),
        ("database table is locked: builds", True),
        ("no such column: disk i/o error", False),
        ('near "locked": syntax error', False),
        ("no such column: owner_id", False),
        ("no such table: builds", False),
        ("file is not a database", False),
    ],
)
def test_which_errors_say_the_store_could_not_be_reached(message: str, unreachable: bool) -> None:
    from kpubdata_builder.store.schema_version import says_unreachable

    assert says_unreachable(sqlite3.OperationalError(message)) is unreachable

"""A state store a newer release wrote is refused and left as it is (#1096).

Rolling a deployment back is when this happens. The event store took any version as
its own, and the build index dropped its table for any version but its own — a newer
one too — so the newer release came back to an empty index and nothing had said so.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.events import BuildEventStore
from kpubdata_builder.events.store import SCHEMA_VERSION as EVENTS_VERSION
from kpubdata_builder.events.store import events_store_path
from kpubdata_builder.service import BuilderService
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.store import SCHEMA_VERSION as INDEX_VERSION
from kpubdata_builder.store import SqliteBuildIndex, rebuild_index
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

"""The table catalog is checked when the server starts, and copied before it changes (#1096).

The catalog is canonical: nothing rebuilds it. It was opened on the first request that
needed it, so one this release could not use failed that request and not the start; and
a migration rewrote it with no copy of what it had been.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kpubdata_builder.cli import main
from kpubdata_builder.service import BuilderService
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.warehouse import CATALOG_FILENAME, SnapshotStateError, TableCatalog
from kpubdata_builder.warehouse import catalog as catalog_module
from kpubdata_builder.warehouse.catalog import SCHEMA_VERSION

WORKSPACE = "ws-1"
#: An older version the migrations really start from: the step from it rebuilds the
#: snapshot table, so a catalog made by this release and marked as it migrates cleanly.
OLD = 2


def _no_client(**_kwargs: object) -> SourceClient:
    raise AssertionError("no provider is called here")


def _catalog_with_one_table(root: Path) -> tuple[Path, str]:
    catalog = TableCatalog(root)
    table = catalog.create_table(WORKSPACE, "sales")
    catalog.close()
    return root / CATALOG_FILENAME, table.id


def _set_version(path: Path, version: int) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _version(path: Path) -> int:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return int(conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0])


def _journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


def _dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return list(conn.iterdump())


def _copies(root: Path) -> list[str]:
    return sorted(item.name for item in root.iterdir() if "before-migration" in item.name)


def test_older_catalog_is_copied_before_it_is_migrated(tmp_path: Path) -> None:
    path, table_id = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    before = _dump(path)

    migrated = TableCatalog(tmp_path)

    assert migrated.get_table(table_id).logical_name == "sales"
    assert _version(path) == SCHEMA_VERSION
    assert _copies(tmp_path) == [f"{CATALOG_FILENAME}.v{OLD}.before-migration"]
    copy = tmp_path / _copies(tmp_path)[0]
    # What the catalog was, whole, and readable without a -shm beside it.
    assert _dump(copy) == before
    assert _journal_mode(copy) == "delete"
    migrated.close()


def test_current_catalog_is_not_copied(tmp_path: Path) -> None:
    _catalog_with_one_table(tmp_path)

    TableCatalog(tmp_path).close()

    assert _copies(tmp_path) == []


def test_copy_from_the_first_attempt_is_kept(tmp_path: Path) -> None:
    """A second start does not replace it with a later state."""
    path, _ = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    TableCatalog(tmp_path).close()
    copy = tmp_path / _copies(tmp_path)[0]
    first = copy.read_bytes()

    again = TableCatalog(tmp_path)
    again.create_table(WORKSPACE, "later")
    again.close()
    _set_version(path, OLD)
    TableCatalog(tmp_path).close()

    assert copy.read_bytes() == first


def test_failed_migration_leaves_the_old_version_and_the_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interrupted, then retried: the catalog is on the old version until it is not."""
    path, table_id = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    before = _dump(path)
    working = dict(catalog_module._MIGRATIONS)
    # The first step runs; a later one fails. Nothing of the first is kept.
    last = SCHEMA_VERSION - 1
    broken = {**working, last: ("THIS IS NOT SQL",)}
    monkeypatch.setattr(catalog_module, "_MIGRATIONS", broken)

    with pytest.raises(sqlite3.Error):
        TableCatalog(tmp_path)

    assert _version(path) == OLD
    assert _dump(path) == before
    assert _dump(tmp_path / _copies(tmp_path)[0]) == before

    monkeypatch.setattr(catalog_module, "_MIGRATIONS", working)
    retried = TableCatalog(tmp_path)
    assert _version(path) == SCHEMA_VERSION
    assert retried.get_table(table_id).logical_name == "sales"
    retried.close()


def test_newer_catalog_is_refused_as_it_was_found(tmp_path: Path) -> None:
    path, _ = _catalog_with_one_table(tmp_path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
    _set_version(path, SCHEMA_VERSION + 1)
    before = _dump(path)
    files = sorted(item.name for item in tmp_path.iterdir())

    with pytest.raises(SnapshotStateError, match="no migration"):
        TableCatalog(tmp_path)

    assert _dump(path) == before
    # Refused before the connection that sets WAL, and with nothing made beside it.
    assert _journal_mode(path) == "delete"
    assert sorted(item.name for item in tmp_path.iterdir()) == files


def test_service_opens_an_existing_catalog_when_it_is_made(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"
    path, _ = _catalog_with_one_table(warehouse)
    _set_version(path, SCHEMA_VERSION + 1)

    with pytest.raises(SnapshotStateError):
        BuilderService(output_root=tmp_path, client_factory=_no_client, warehouse_root=warehouse)


def test_service_still_makes_no_catalog_until_one_is_needed(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse"

    BuilderService(output_root=tmp_path, client_factory=_no_client, warehouse_root=warehouse)

    assert not (warehouse / CATALOG_FILENAME).exists()


def test_serve_says_so_in_one_line_and_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import kpubdata_builder.service.http as http_module

    started: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: started.append(service))
    warehouse = tmp_path / "warehouse"
    path, _ = _catalog_with_one_table(warehouse)
    _set_version(path, 999)
    before = _dump(path)

    code = main(["serve", "--output-dir", str(tmp_path), "--warehouse", str(warehouse)])

    err = capsys.readouterr().err
    assert code == 1
    assert "error: catalog schema version is 999" in err
    assert "Traceback" not in err
    assert started == []
    assert _dump(path) == before

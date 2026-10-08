"""The table catalog is checked when the server starts, and copied before it changes (#1096).

The catalog is canonical: nothing rebuilds it. It was opened on the first request that
needed it, so one this release could not use failed that request and not the start; and
a migration rewrote it with no copy of what it had been.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
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


def _tables(path: Path) -> list[str]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return sorted(row[0] for row in conn.execute("SELECT logical_name FROM tables"))


def test_a_second_upgrade_copies_what_the_rolled_back_release_wrote(tmp_path: Path) -> None:
    """Upgrade, roll back, upgrade again (#1163): the copy is the latest state.

    The copy used to be kept from the first attempt, so putting it back the second time
    lost what the old release had written after the rollback.
    """
    path, _ = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    TableCatalog(tmp_path).close()  # 1. upgrade: the copy holds state A
    copy = tmp_path / f"{CATALOG_FILENAME}.v{OLD}.before-migration"
    assert _tables(copy) == ["sales"]

    # 2. roll back: the copy is put back, as an operator would — copied, so it stays —
    # and the old release writes B.
    for leftover in (f"{path}-wal", f"{path}-shm"):
        Path(leftover).unlink(missing_ok=True)
    shutil.copyfile(copy, path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO tables (id, workspace_id, logical_name, current_snapshot_id, revision)"
            " VALUES ('tbl_b', ?, 'written_after_rollback', NULL, 0)",
            (WORKSPACE,),
        )
    before = _dump(path)

    TableCatalog(tmp_path).close()  # 3. upgrade again

    # 4. the copy to put back now has B.
    assert _tables(copy) == ["sales", "written_after_rollback"]
    assert _dump(copy) == before
    assert _copies(tmp_path) == [copy.name]


def test_the_copy_is_taken_when_the_version_could_not_be_read_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The early read finds nothing when the file is locked; the migration still copies.

    The copy is decided by the version read under the migration's own lock, not by the
    early, lock-free read (#1163).
    """
    path, _ = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    before = _dump(path)
    monkeypatch.setattr(catalog_module, "stored_version", lambda _path: None)

    TableCatalog(tmp_path).close()

    assert _version(path) == SCHEMA_VERSION
    assert _dump(tmp_path / f"{CATALOG_FILENAME}.v{OLD}.before-migration") == before


def test_no_writer_lands_between_the_copy_and_the_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy is taken under the migration's write lock (#1163).

    A writer that tries while the copy is being taken waits for the migration to commit,
    so its write is on the migrated catalog and in neither the copy nor a lost gap.
    """
    path, _ = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    copied = catalog_module.TableCatalog._copy_before_migration
    outcome: dict[str, object] = {}

    def copy_while_another_writes(self: TableCatalog, found: int) -> Path:
        def write() -> None:
            try:
                with closing(sqlite3.connect(path, timeout=0.2)) as conn, conn:
                    conn.execute("UPDATE tables SET revision = revision + 1")
                outcome["write"] = "landed"
            except sqlite3.OperationalError as exc:
                outcome["write"] = str(exc)

        writer = threading.Thread(target=write)
        writer.start()
        writer.join()
        return copied(self, found)

    monkeypatch.setattr(
        catalog_module.TableCatalog, "_copy_before_migration", copy_while_another_writes
    )

    TableCatalog(tmp_path).close()

    assert outcome["write"] == "database is locked"


def test_starts_at_the_same_moment_leave_one_copy_and_no_partial(tmp_path: Path) -> None:
    """Two processes starting together used to share one ``.partial`` name (#1163)."""
    path, table_id = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    before = _dump(path)
    start = threading.Barrier(4)
    errors: list[BaseException] = []

    def open_catalog() -> None:
        start.wait()
        try:
            TableCatalog(tmp_path).close()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=open_catalog) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert _version(path) == SCHEMA_VERSION
    assert _copies(tmp_path) == [f"{CATALOG_FILENAME}.v{OLD}.before-migration"]
    assert _dump(tmp_path / _copies(tmp_path)[0]) == before
    assert TableCatalog(tmp_path).get_table(table_id).logical_name == "sales"


def test_a_failed_copy_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, _ = _catalog_with_one_table(tmp_path)
    _set_version(path, OLD)
    before = _dump(path)

    def no_room(self: Path, target: Path) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(Path, "replace", no_room)

    with pytest.raises(OSError, match="no space"):
        TableCatalog(tmp_path)

    monkeypatch.undo()
    assert [item.name for item in tmp_path.iterdir() if "partial" in item.name] == []
    # Nothing was migrated without its copy.
    assert _version(path) == OLD
    assert _dump(path) == before


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

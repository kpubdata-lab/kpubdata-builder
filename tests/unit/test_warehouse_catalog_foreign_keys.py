"""The catalog's foreign keys survive the version 2 -> 3 rebuild, and are repaired (#1319).

The version 2 -> 3 step renamed ``table_snapshots`` to ``table_snapshots_v2``,
created the new table and dropped the renamed one. SQLite 3.26 and later rewrite the
foreign keys of every table that references a renamed table, so ``snapshot_leases``
and ``snapshot_holds`` were left pointing at the dropped ``table_snapshots_v2``: every
lease or hold insert then failed with ``no such table``. With leases present, the drop
itself failed the foreign-key check and the catalog could not be upgraded at all.

The version 2 catalog below is not a recorded file. It is built with the DDL of the
last release whose ``SCHEMA_VERSION`` was 2 (``warehouse/catalog.py`` at the parent of
commit 50a8613), copied verbatim, and filled through the same columns that release
wrote.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kpubdata_builder.warehouse import SnapshotLayout, SnapshotStateError, TableCatalog
from kpubdata_builder.warehouse import gc as warehouse_gc
from kpubdata_builder.warehouse.catalog import CATALOG_FILENAME, SCHEMA_VERSION

WORKSPACE = "ws_personal"
TABLE_ID = "tbl_sales"
SNAPSHOTS = ("snap_1", "snap_2")
FUTURE = "2999-01-01T00:00:00+00:00"

# The schema of the last version 2 release, statement for statement.
_V2_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS schema_version ("
    " version INTEGER PRIMARY KEY,"
    " applied_at TEXT NOT NULL DEFAULT (datetime('now')))",
    "CREATE TABLE IF NOT EXISTS tables ("
    " id TEXT PRIMARY KEY,"
    " workspace_id TEXT NOT NULL,"
    " logical_name TEXT NOT NULL,"
    " current_snapshot_id TEXT,"
    " revision INTEGER NOT NULL DEFAULT 0,"
    " UNIQUE (workspace_id, logical_name))",
    "CREATE TABLE IF NOT EXISTS table_snapshots ("
    " id TEXT PRIMARY KEY,"
    " table_id TEXT NOT NULL REFERENCES tables(id),"
    " run_id TEXT NOT NULL,"
    " schema_version INTEGER NOT NULL,"
    " coverage_hash TEXT NOT NULL,"
    " artifact_digest TEXT NOT NULL,"
    " row_count INTEGER,"
    " state TEXT NOT NULL CHECK (state IN"
    "   ('staging','validated','committed','quarantined')),"
    " created_at TEXT NOT NULL,"
    " committed_at TEXT,"
    " owner_id TEXT,"
    " coverage_fingerprint TEXT,"
    " source_params_fingerprint TEXT,"
    " schema_contract_version TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_snapshots_table ON table_snapshots(table_id, created_at DESC)",
    "CREATE TABLE IF NOT EXISTS snapshot_leases ("
    " lease_id TEXT PRIMARY KEY,"
    " snapshot_id TEXT NOT NULL REFERENCES table_snapshots(id),"
    " acquired_at TEXT NOT NULL,"
    " expires_at TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_leases_snapshot ON snapshot_leases(snapshot_id, expires_at)",
)


def _connect(path: Path, *, foreign_keys: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}")
    return conn


def _version_2_catalog(root: Path, *, leases: int) -> Path:
    """A version 2 catalog: one table, two committed snapshots, ``leases`` leases."""
    root.mkdir(parents=True)
    path = root / CATALOG_FILENAME
    with closing(_connect(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        for statement in _V2_SCHEMA:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_version (version) VALUES (2)")
        conn.execute(
            "INSERT INTO tables (id, workspace_id, logical_name, current_snapshot_id, revision)"
            " VALUES (?, ?, 'sales', ?, 2)",
            (TABLE_ID, WORKSPACE, SNAPSHOTS[-1]),
        )
        for number, snapshot_id in enumerate(SNAPSHOTS, start=1):
            conn.execute(
                "INSERT INTO table_snapshots (id, table_id, run_id, schema_version,"
                " coverage_hash, artifact_digest, row_count, state, created_at,"
                " committed_at, owner_id)"
                " VALUES (?, ?, ?, 1, ?, 'sha256:x', 1, 'committed', ?, ?, 'alice')",
                (
                    snapshot_id,
                    TABLE_ID,
                    f"run-{number}",
                    f"cov-{number}",
                    f"2026-01-0{number}T00:00:00+00:00",
                    f"2026-01-0{number}T00:00:00+00:00",
                ),
            )
        for number in range(leases):
            conn.execute(
                "INSERT INTO snapshot_leases VALUES (?, ?, '2026-01-01T00:00:00+00:00', ?)",
                (f"lease_{number}", SNAPSHOTS[0], FUTURE),
            )
    return path


def _parents(path: Path, table: str) -> set[str]:
    with closing(_connect(path)) as conn:
        return {
            str(row[0])
            for row in conn.execute('SELECT "table" FROM pragma_foreign_key_list(?)', (table,))
        }


def _rows(path: Path, table: str) -> list[tuple[object, ...]]:
    with closing(_connect(path)) as conn:
        return list(conn.execute(f"SELECT * FROM {table} ORDER BY 1"))  # noqa: S608 - fixed names


def _assert_consistent(path: Path) -> None:
    """Every foreign key names a table that exists, holds, and the file is sound."""
    with closing(_connect(path)) as conn:
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        assert list(conn.execute("PRAGMA integrity_check")) == [("ok",)]
    assert _parents(path, "snapshot_leases") == {"table_snapshots"}
    assert _parents(path, "snapshot_holds") == {"table_snapshots"}


def _exercise(root: Path) -> None:
    """Leases, holds, a commit and a collection all work, and bad references fail."""
    catalog = TableCatalog(root)
    try:
        existing = {h.hold_id for h in catalog.live_holds(SNAPSHOTS[1])}
        pin = catalog.pin(SNAPSHOTS[0])
        hold = catalog.place_hold(SNAPSHOTS[1], kind="audit", reason="evidence for #1319")
        assert {h.hold_id for h in catalog.live_holds(SNAPSHOTS[1])} == existing | {hold.hold_id}

        snapshot = catalog.begin_snapshot(
            TABLE_ID,
            run_id="run-3",
            schema_version=1,
            coverage_hash="cov-3",
            artifact_digest="sha256:x",
            row_count=1,
        )
        layout = SnapshotLayout(catalog.root, TABLE_ID)
        (layout.begin(snapshot.id) / "part-0.csv").write_text("a\n1\n", encoding="utf-8")
        catalog.mark_validated(snapshot.id)
        layout.promote(snapshot.id)
        catalog.commit_snapshot(snapshot.id, expected_revision=2)
        assert catalog.get_table(TABLE_ID).current_snapshot_id == snapshot.id

        catalog.release(pin.lease_id)
        catalog.release_hold(hold.hold_id)
        assert {h.hold_id for h in catalog.live_holds(SNAPSHOTS[1])} == existing
        warehouse_gc.collect(catalog, TABLE_ID, keep=1)

        # The foreign keys are enforced again: a reference to nothing fails closed.
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            catalog._conn.execute(  # noqa: SLF001 - the check is on the constraint itself
                "INSERT INTO snapshot_leases VALUES ('bad', 'snap_missing', ?, ?)",
                (FUTURE, FUTURE),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            catalog._conn.execute(  # noqa: SLF001
                "INSERT INTO snapshot_holds VALUES ('bad', 'snap_missing', 'audit', 'r', ?, NULL)",
                (FUTURE,),
            )
    finally:
        catalog.close()


# ---------------------------------------------------------------- migration


@pytest.mark.parametrize("leases", [0, 2], ids=["no-leases", "with-leases"])
def test_a_version_2_catalog_migrates_with_its_foreign_keys_intact(
    tmp_path: Path, leases: int
) -> None:
    """Tables, snapshots and leases survive, and leases and holds name table_snapshots."""
    root = tmp_path / "warehouse"
    path = _version_2_catalog(root, leases=leases)
    before_tables = _rows(path, "tables")
    before_snapshots = _rows(path, "table_snapshots")
    before_leases = _rows(path, "snapshot_leases")

    TableCatalog(root).close()

    _assert_consistent(path)
    assert _rows(path, "tables") == before_tables
    # Version 5 appended the coverage column; every earlier value is unchanged.
    assert [row[:-1] for row in _rows(path, "table_snapshots")] == before_snapshots
    assert _rows(path, "snapshot_leases") == before_leases
    assert _rows(path, "schema_version")[0][0] == SCHEMA_VERSION
    _exercise(root)


def test_a_migration_that_finds_dangling_rows_is_rolled_back(tmp_path: Path) -> None:
    """A lease naming a snapshot that does not exist stops the migration.

    The rows are not repaired by deleting them: the catalog stays on version 2 with
    every row, and the error says which rows to look at.
    """
    root = tmp_path / "warehouse"
    path = _version_2_catalog(root, leases=1)
    with closing(_connect(path, foreign_keys=False)) as conn:
        conn.execute(
            "INSERT INTO snapshot_leases VALUES ('lease_orphan', 'snap_gone', ?, ?)",
            (FUTURE, FUTURE),
        )
    before = {table: _rows(path, table) for table in ("tables", "table_snapshots")}
    before_leases = _rows(path, "snapshot_leases")

    with pytest.raises(SnapshotStateError, match="snapshot_leases"):
        TableCatalog(root)

    assert _rows(path, "schema_version")[0][0] == 2
    assert {table: _rows(path, table) for table in before} == before
    assert _rows(path, "snapshot_leases") == before_leases
    assert _parents(path, "snapshot_leases") == {"table_snapshots"}


# ------------------------------------------------------------------- repair


def _broken_by_the_old_migration(root: Path, *, populated: bool) -> Path:
    """A version 5 catalog whose leases and holds name the dropped table_snapshots_v2.

    The rebuild is the one the version 2 -> 3 step used to run, with foreign keys on
    as the catalog's connection had them. The leases and holds are written with
    foreign keys off — with them on, every insert into the broken tables fails.
    """
    catalog = TableCatalog(root)
    catalog.close()
    path = root / CATALOG_FILENAME
    with closing(_connect(path)) as conn:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE table_snapshots RENAME TO table_snapshots_v2")
        conn.execute(
            "CREATE TABLE table_snapshots ("
            " id TEXT PRIMARY KEY,"
            " table_id TEXT NOT NULL REFERENCES tables(id),"
            " run_id TEXT NOT NULL,"
            " schema_version INTEGER NOT NULL,"
            " coverage_hash TEXT NOT NULL,"
            " artifact_digest TEXT NOT NULL,"
            " row_count INTEGER,"
            " state TEXT NOT NULL CHECK (state IN"
            "   ('staging','validated','committed','quarantined','abandoned','retiring')),"
            " created_at TEXT NOT NULL,"
            " committed_at TEXT,"
            " owner_id TEXT,"
            " coverage_fingerprint TEXT,"
            " source_params_fingerprint TEXT,"
            " schema_contract_version TEXT,"
            " coverage TEXT)"
        )
        conn.execute("INSERT INTO table_snapshots SELECT * FROM table_snapshots_v2")
        conn.execute("DROP TABLE table_snapshots_v2")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_table"
            " ON table_snapshots(table_id, created_at DESC)"
        )
        conn.execute(
            "INSERT INTO tables (id, workspace_id, logical_name, current_snapshot_id, revision)"
            " VALUES (?, ?, 'sales', ?, 2)",
            (TABLE_ID, WORKSPACE, SNAPSHOTS[-1]),
        )
        for number, snapshot_id in enumerate(SNAPSHOTS, start=1):
            conn.execute(
                "INSERT INTO table_snapshots (id, table_id, run_id, schema_version,"
                " coverage_hash, artifact_digest, row_count, state, created_at, committed_at)"
                " VALUES (?, ?, ?, 1, ?, 'sha256:x', 1, 'committed', ?, ?)",
                (
                    snapshot_id,
                    TABLE_ID,
                    f"run-{number}",
                    f"cov-{number}",
                    f"2026-01-0{number}T00:00:00+00:00",
                    f"2026-01-0{number}T00:00:00+00:00",
                ),
            )
        conn.execute("COMMIT")
    assert _parents(path, "snapshot_leases") == {"table_snapshots_v2"}
    assert _parents(path, "snapshot_holds") == {"table_snapshots_v2"}
    with closing(_connect(path)) as conn, pytest.raises(sqlite3.OperationalError, match="_v2"):
        conn.execute(
            "INSERT INTO snapshot_leases VALUES ('x', ?, ?, ?)", (SNAPSHOTS[0], FUTURE, FUTURE)
        )
    if populated:
        with closing(_connect(path, foreign_keys=False)) as conn:
            conn.execute(
                "INSERT INTO snapshot_leases VALUES ('lease_0', ?, ?, ?)",
                (SNAPSHOTS[0], FUTURE, FUTURE),
            )
            conn.execute(
                "INSERT INTO snapshot_holds VALUES ('hold_0', ?, 'retention', 'kept', ?, NULL)",
                (SNAPSHOTS[1], FUTURE),
            )
    return path


@pytest.mark.parametrize("populated", [False, True], ids=["empty", "populated"])
def test_a_catalog_the_old_migration_broke_is_repaired_in_place(
    tmp_path: Path, populated: bool
) -> None:
    """The two tables are rebuilt with the right reference; no row is lost."""
    root = tmp_path / "warehouse"
    path = _broken_by_the_old_migration(root, populated=populated)
    before = {
        table: _rows(path, table)
        for table in ("tables", "table_snapshots", "snapshot_leases", "snapshot_holds")
    }

    TableCatalog(root).close()

    _assert_consistent(path)
    assert {table: _rows(path, table) for table in before} == before
    copy = root / f"{CATALOG_FILENAME}.v{SCHEMA_VERSION}.before-foreign-key-repair"
    assert _parents(copy, "snapshot_leases") == {"table_snapshots_v2"}
    _exercise(root)


def test_a_repair_that_cannot_be_proven_changes_nothing(tmp_path: Path) -> None:
    """A hold whose snapshot is gone cannot be given a valid reference.

    The catalog is canonical, so the hold is not dropped to make the check pass: the
    repair is rolled back, the file keeps every row, and the error names the row.
    """
    root = tmp_path / "warehouse"
    path = _broken_by_the_old_migration(root, populated=True)
    with closing(_connect(path, foreign_keys=False)) as conn:
        conn.execute(
            "INSERT INTO snapshot_holds VALUES ('hold_orphan', 'snap_gone', 'audit', 'r', ?, NULL)",
            (FUTURE,),
        )
    before = {
        table: _rows(path, table)
        for table in ("tables", "table_snapshots", "snapshot_leases", "snapshot_holds")
    }

    with pytest.raises(SnapshotStateError, match="hold_orphan|snapshot_holds") as raised:
        TableCatalog(root)

    assert "foreign_key_check" in str(raised.value)
    assert {table: _rows(path, table) for table in before} == before
    assert _parents(path, "snapshot_holds") == {"table_snapshots_v2"}


def test_a_dangling_reference_it_does_not_recognise_is_refused(tmp_path: Path) -> None:
    """Only the damage the old migration did is repaired; anything else is reported."""
    root = tmp_path / "warehouse"
    TableCatalog(root).close()
    path = root / CATALOG_FILENAME
    with closing(_connect(path)) as conn:
        conn.execute("CREATE TABLE notes (id TEXT, table_id TEXT REFERENCES tables_old(id))")

    with pytest.raises(SnapshotStateError, match="tables_old"):
        TableCatalog(root)

    assert _parents(path, "notes") == {"tables_old"}

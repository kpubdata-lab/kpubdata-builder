"""Every SQLite store records a schema version and refuses a newer one (#1096).

Seven of the ten stores recorded none: the credentials, the uploads, the publish
receipts, the document revisions, the sign-up ledger, the saved analyses and the
provider test log. A file a newer release wrote was read as this release's, and three of
them changed only by adding whatever column was missing when they were opened.

Each of the seven is held to the same four things here, on a file written the way the
release before this one wrote it — the table definitions below are that release's, kept
as the fixture: a new file gets the version, an existing file without one is adopted
with every row it had, a file with a newer version is refused and left as it is, and a
file from before a column was added gains the column.
"""

from __future__ import annotations

import base64
import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from kpubdata_builder.credentials import store as credentials_store
from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher
from kpubdata_builder.service import BuilderService, analyses_api, provider_tests, revisions
from kpubdata_builder.service import publish as publish_service
from kpubdata_builder.service import user_ledger as ledger_module
from kpubdata_builder.stages.bronze.build import SourceClient
from kpubdata_builder.store.schema_version import (
    StoreSchema,
    UnsupportedSchemaVersionError,
    stored_version,
)
from kpubdata_builder.uploads import store as uploads_store

_MASTER_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
_CIPHER = AesGcmCredentialCipher.from_base64(_MASTER_KEY)
_OWNER = "oidc:a"


def _no_client(**_kwargs: object) -> SourceClient:
    raise AssertionError("no provider is called here")


# ------------------------------------------ the files as the release before wrote them


def _legacy_credentials(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE provider_credentials (owner_id TEXT NOT NULL, provider TEXT NOT NULL,"
        " ciphertext BLOB NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY (owner_id, provider))"
    )
    ciphertext = _CIPHER.encrypt(
        "the-key", associated_data=credentials_store.associated_data(_OWNER, "datago")
    )
    conn.execute(
        "INSERT INTO provider_credentials VALUES (?, ?, ?, ?)",
        (_OWNER, "datago", ciphertext, "2026-09-01T00:00:00+00:00"),
    )


def _legacy_provider_tests(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE provider_tests ("
        " owner_id TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,"
        " checked_at TEXT NOT NULL, error_category TEXT, response_code INTEGER,"
        " dataset TEXT, PRIMARY KEY (owner_id, provider))"
    )
    conn.execute(
        "INSERT INTO provider_tests VALUES (?, 'datago', 'ok', '2026-09-01T00:00:00Z',"
        " NULL, 200, 'datago.hospital_info')",
        (_OWNER,),
    )


def _legacy_revisions(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE revisions ("
        " workspace TEXT NOT NULL, kind TEXT NOT NULL, doc_id TEXT NOT NULL,"
        " revision INTEGER NOT NULL, content TEXT NOT NULL, note TEXT,"
        " author TEXT NOT NULL, created_at TEXT NOT NULL, reverted_from INTEGER,"
        " idempotency_key TEXT,"
        " PRIMARY KEY (workspace, kind, doc_id, revision))"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_revisions_idempotency"
        " ON revisions(workspace, kind, doc_id, idempotency_key)"
        " WHERE idempotency_key IS NOT NULL"
    )
    conn.execute(
        "CREATE TABLE revision_audit ("
        " seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,"
        " kind TEXT NOT NULL, doc_id TEXT NOT NULL, revision INTEGER NOT NULL,"
        " action TEXT NOT NULL, author TEXT NOT NULL, at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO revisions VALUES ('ws', 'spec', 'doc', 1, '{\"name\": \"x\"}', 'first',"
        " 'oidc:a', '2026-09-01T00:00:00+00:00', NULL, 'key-1')"
    )
    conn.execute(
        "INSERT INTO revision_audit (workspace, kind, doc_id, revision, action, author, at)"
        " VALUES ('ws', 'spec', 'doc', 1, 'save', 'oidc:a', '2026-09-01T00:00:00+00:00')"
    )


def _legacy_ledger(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE users ("
        " user_id TEXT PRIMARY KEY, display_name TEXT, status TEXT NOT NULL,"
        " first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,"
        " decided_at TEXT, decided_by TEXT)"
    )
    conn.execute(
        "INSERT INTO users VALUES ('oidc:a', 'a@example.org', 'rejected',"
        " '2026-09-01T00:00:00+00:00', '2026-09-02T00:00:00+00:00',"
        " '2026-09-03T00:00:00+00:00', 'admin')"
    )


_ANALYSES_FIRST = (
    "CREATE TABLE analyses ("
    " analysis_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, owner_id TEXT,"
    " name TEXT NOT NULL, sql TEXT NOT NULL, row_limit INTEGER NOT NULL,"
    " table_name TEXT NOT NULL, snapshot_id TEXT NOT NULL, hold_id TEXT NOT NULL,"
    " result_meta TEXT NOT NULL, created_at TEXT NOT NULL)"
)
_ANALYSIS_ROW = (
    "INSERT INTO analyses (analysis_id, workspace_id, owner_id, name, sql, row_limit,"
    " table_name, snapshot_id, hold_id, result_meta, created_at)"
    " VALUES ('an-1', 'ws', 'oidc:a', 'mine', 'SELECT 1', 10, 't', 'snap-1', 'hold-1', '{}',"
    " '2026-09-01T00:00:00+00:00')"
)


def _oldest_analyses(conn: sqlite3.Connection) -> None:
    """Before the dialect columns (#875)."""
    conn.execute(_ANALYSES_FIRST)
    conn.execute("CREATE INDEX idx_analyses_workspace ON analyses(workspace_id, created_at DESC)")
    conn.execute(_ANALYSIS_ROW)


def _legacy_analyses(conn: sqlite3.Connection) -> None:
    _oldest_analyses(conn)
    conn.execute(
        "ALTER TABLE analyses ADD COLUMN sql_dialect TEXT NOT NULL DEFAULT 'legacy-polars'"
    )
    conn.execute("ALTER TABLE analyses ADD COLUMN engine TEXT NOT NULL DEFAULT 'polars'")
    conn.execute("ALTER TABLE analyses ADD COLUMN engine_version TEXT")
    conn.execute("ALTER TABLE analyses ADD COLUMN query_contract_version TEXT")
    conn.execute("UPDATE analyses SET sql_dialect = 'duckdb', engine = 'duckdb'")


_UPLOAD_ROW = (
    "INSERT INTO uploads (upload_id, owner_id, format, encoding, size_bytes,"
    " original_filename, content, created_at)"
    " VALUES ('upl_1', 'oidc:a', 'csv', 'utf-8', 8, 'a.csv', X'612C620A312C320A',"
    " '2026-09-01T00:00:00+00:00')"
)


def _oldest_uploads(conn: sqlite3.Connection) -> None:
    """Before a large payload could be kept in a file beside the store (#622)."""
    conn.execute(
        "CREATE TABLE uploads (upload_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,"
        " format TEXT NOT NULL, encoding TEXT NOT NULL, size_bytes INTEGER NOT NULL,"
        " original_filename TEXT, content BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    conn.execute("CREATE INDEX idx_uploads_owner_id ON uploads(owner_id)")
    conn.execute(_UPLOAD_ROW)


def _legacy_uploads(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE uploads (upload_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,"
        " format TEXT NOT NULL, encoding TEXT NOT NULL, size_bytes INTEGER NOT NULL,"
        " original_filename TEXT, content BLOB, created_at TEXT NOT NULL,"
        " blob_path TEXT, content_sha256 TEXT)"
    )
    conn.execute("CREATE INDEX idx_uploads_owner_id ON uploads(owner_id)")
    conn.execute(_UPLOAD_ROW)


_RECEIPTS_TABLE = (
    "CREATE TABLE publish_receipts (owner_key TEXT NOT NULL, run_id TEXT NOT NULL,"
    " target TEXT NOT NULL, destination TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,"
    " options_json TEXT NOT NULL,"
    " state TEXT NOT NULL CHECK (state IN ('pending', 'succeeded', 'unknown')),"
    " result_json TEXT, PRIMARY KEY (owner_key, run_id, target, destination))"
)
_RECEIPT_ROW = (
    "INSERT INTO publish_receipts VALUES ('oidc:a', 'run-1', 'huggingface', 'me/data',"
    " 'sha256:f', '{}', 'succeeded', '{\"url\": \"https://example.org\"}')"
)


def _oldest_receipts(conn: sqlite3.Connection) -> None:
    """The audit log before it carried the owner and the run (#563)."""
    conn.execute(_RECEIPTS_TABLE)
    conn.execute(
        "CREATE TABLE publish_receipt_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " fingerprint TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL,"
        " recorded_at TEXT NOT NULL)"
    )
    conn.execute(_RECEIPT_ROW)
    conn.execute(
        "INSERT INTO publish_receipt_audit (fingerprint, action, actor, recorded_at)"
        " VALUES ('sha256:f', 'reconcile_succeeded', 'system', '2026-09-01T00:00:00+00:00')"
    )


def _legacy_receipts(conn: sqlite3.Connection) -> None:
    conn.execute(_RECEIPTS_TABLE)
    conn.execute(
        "CREATE TABLE publish_receipt_audit (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
        " fingerprint TEXT NOT NULL, owner_key TEXT, run_id TEXT, action TEXT NOT NULL,"
        " actor TEXT NOT NULL, recorded_at TEXT NOT NULL)"
    )
    conn.execute(_RECEIPT_ROW)
    conn.execute(
        "INSERT INTO publish_receipt_audit (fingerprint, owner_key, run_id, action, actor,"
        " recorded_at) VALUES ('sha256:f', 'oidc:a', 'run-1', 'reconcile_succeeded', 'system',"
        " '2026-09-01T00:00:00+00:00')"
    )


# ------------------------------------------------- what each store's own API reads back


def _open_credentials(path: Path) -> object:
    return credentials_store.SQLiteCredentialRepository(path, _CIPHER).get_secret(_OWNER, "datago")


def _open_provider_tests(path: Path) -> object:
    return provider_tests.ProviderTestLog(path).last_tests(_OWNER)


def _open_revisions(path: Path) -> object:
    store = revisions.RevisionStore(path)
    return (
        [(r.revision, r.content, r.note) for r in store.history("ws", "spec", "doc")],
        store.audit("ws", "spec", "doc"),
    )


def _open_ledger(path: Path) -> object:
    return [(entry.user_id, entry.status) for entry in ledger_module.UserLedger(path).list()]


def _open_analyses(path: Path) -> object:
    return [
        (a.analysis_id, a.sql, a.sql_dialect) for a in analyses_api.AnalysisStore(path).list("ws")
    ]


def _open_uploads(path: Path) -> object:
    return uploads_store.SQLiteUploadRepository(path, max_bytes=1024).get_content(_OWNER, "upl_1")


def _open_receipts(path: Path) -> object:
    store = publish_service.PublishReceiptStore(path.parent)
    assert store.path == path
    receipt = store.get_by_key(
        owner_key=_OWNER, run_id="run-1", target="huggingface", destination="me/data"
    )
    audit = store.audit_entries(owner_key=_OWNER, run_id="run-1")
    return (None if receipt is None else receipt.state, [entry["action"] for entry in audit])


@dataclass(frozen=True)
class Case:
    """One store: its schema, its file, and a file the release before this one wrote."""

    schema: StoreSchema
    #: The file, relative to the output directory.
    relative: str
    #: Writes the tables and a row as the release before this one did.
    legacy: Callable[[sqlite3.Connection], None]
    #: Opens the store and returns what its API reads of that row.
    open: Callable[[Path], object]
    #: What ``open`` returns for a ``legacy`` file, and for an empty store.
    kept: object
    empty: object
    #: The file as it was before the store gained a column, the table and the columns it
    #: gained, and what ``open`` returns for it. None for a store that never changed.
    oldest: Callable[[sqlite3.Connection], None] | None = None
    gained: tuple[str, tuple[str, ...]] | None = None
    kept_from_oldest: object = None


_KEPT_TEST = {
    "datago": {
        "status": "ok",
        "checked_at": "2026-09-01T00:00:00Z",
        "error_category": None,
        "response_code": 200,
        "dataset": "datago.hospital_info",
    }
}
_KEPT_AUDIT = [
    {"revision": 1, "action": "save", "author": "oidc:a", "at": "2026-09-01T00:00:00+00:00"}
]

CASES = (
    Case(
        credentials_store.SCHEMA,
        ".service/provider-credentials.sqlite3",
        _legacy_credentials,
        _open_credentials,
        kept="the-key",
        empty=None,
    ),
    Case(
        provider_tests.SCHEMA,
        ".service/provider_tests.sqlite3",
        _legacy_provider_tests,
        _open_provider_tests,
        kept=_KEPT_TEST,
        empty={},
    ),
    Case(
        revisions.SCHEMA,
        ".service/revisions.sqlite3",
        _legacy_revisions,
        _open_revisions,
        kept=([(1, {"name": "x"}, "first")], _KEPT_AUDIT),
        empty=([], []),
    ),
    Case(
        ledger_module.SCHEMA,
        ".service/users.sqlite3",
        _legacy_ledger,
        _open_ledger,
        kept=[("oidc:a", "rejected")],
        empty=[],
    ),
    Case(
        analyses_api.SCHEMA,
        ".service/analyses.sqlite3",
        _legacy_analyses,
        _open_analyses,
        kept=[("an-1", "SELECT 1", "duckdb")],
        empty=[],
        oldest=_oldest_analyses,
        gained=(
            "analyses",
            ("sql_dialect", "engine", "engine_version", "query_contract_version"),
        ),
        kept_from_oldest=[("an-1", "SELECT 1", "legacy-polars")],
    ),
    Case(
        uploads_store.SCHEMA,
        ".service/uploads.sqlite3",
        _legacy_uploads,
        _open_uploads,
        kept=b"a,b\n1,2\n",
        empty=None,
        oldest=_oldest_uploads,
        gained=("uploads", ("blob_path", "content_sha256")),
        kept_from_oldest=b"a,b\n1,2\n",
    ),
    Case(
        publish_service.RECEIPTS_SCHEMA,
        "_publish_receipts.sqlite",
        _legacy_receipts,
        _open_receipts,
        kept=("succeeded", ["reconcile_succeeded"]),
        empty=(None, []),
        oldest=_oldest_receipts,
        gained=("publish_receipt_audit", ("owner_key", "run_id")),
        # The audit row has no owner to be found by: it predates the column.
        kept_from_oldest=("succeeded", []),
    ),
)
_IDS = [case.relative for case in CASES]
_WITH_COLUMNS = [case for case in CASES if case.oldest is not None]

every_store = pytest.mark.parametrize("case", CASES, ids=_IDS)


def _write(path: Path, script: Callable[[sqlite3.Connection], None]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        script(conn)


def _rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Every row of every table but the version table, by table."""
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        tables = [
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
                " AND name NOT LIKE 'sqlite_%' AND name != 'schema_version' ORDER BY name"
            )
        ]
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables
        }


def _dump(path: Path) -> list[str]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return list(conn.iterdump())


def _columns(path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _versions(path: Path) -> list[int]:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return [int(row[0]) for row in conn.execute("SELECT version FROM schema_version")]


def _set_version(path: Path, version: int) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            " version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))


def _journal_mode(path: Path) -> str:
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])


# ------------------------------------------------------------------------ a fresh file


@every_store
def test_a_new_store_records_its_version(case: Case, tmp_path: Path) -> None:
    path = tmp_path / case.relative

    assert case.open(path) == case.empty

    assert case.schema.version >= 1
    assert _versions(path) == [case.schema.version]


# ----------------------------------------------- a file from before the version existed


@every_store
def test_a_store_without_a_version_is_adopted_with_its_rows(case: Case, tmp_path: Path) -> None:
    path = tmp_path / case.relative
    _write(path, case.legacy)
    before = _rows(path)
    assert stored_version(path) is None

    assert case.open(path) == case.kept

    assert _versions(path) == [case.schema.version]
    assert _rows(path) == before


@every_store
def test_opening_a_store_again_changes_nothing(case: Case, tmp_path: Path) -> None:
    path = tmp_path / case.relative
    _write(path, case.legacy)
    case.open(path)
    adopted = _dump(path)

    assert case.open(path) == case.kept

    assert _dump(path) == adopted


_ROLLBACK_JOURNAL = [case for case in CASES if case.schema is not publish_service.RECEIPTS_SCHEMA]


@pytest.mark.parametrize("case", _ROLLBACK_JOURNAL, ids=[c.relative for c in _ROLLBACK_JOURNAL])
def test_a_store_at_this_version_is_opened_on_a_disk_that_cannot_be_written(
    case: Case, tmp_path: Path
) -> None:
    """Opening a store that needs nothing writes nothing.

    The receipt store is not here: it is in WAL mode, which cannot be read from a
    directory that cannot be written whatever this code does.
    """
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    path = tmp_path / case.relative
    _write(path, case.legacy)
    case.open(path)
    content = path.read_bytes()
    path.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        assert case.open(path) == case.kept
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)

    assert path.read_bytes() == content


# --------------------------------------------------------------------- a newer file


@every_store
def test_a_store_a_newer_release_wrote_is_refused_and_left_as_it_is(
    case: Case, tmp_path: Path
) -> None:
    path = tmp_path / case.relative
    _write(path, case.legacy)
    _set_version(path, case.schema.version + 1)
    before = _dump(path)
    mode = _journal_mode(path)

    with pytest.raises(UnsupportedSchemaVersionError) as refusal:
        case.open(path)

    assert refusal.value.found == case.schema.version + 1
    assert refusal.value.supported == case.schema.version
    message = str(refusal.value)
    assert case.schema.store in message and str(path) in message
    assert "newer release" in message and case.schema.remedy in message
    assert _dump(path) == before
    assert _journal_mode(path) == mode


@every_store
def test_the_service_does_not_start_on_a_newer_store(
    case: Case, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refused when the service is made, not on the first request that needs the store."""
    monkeypatch.setenv("KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY", _MASTER_KEY)
    monkeypatch.delenv("KPUBDATA_BUILDER_STORAGE_BACKEND", raising=False)
    path = tmp_path / case.relative
    _write(path, case.legacy)
    _set_version(path, case.schema.version + 1)
    before = _dump(path)

    with pytest.raises(UnsupportedSchemaVersionError) as refusal:
        BuilderService(output_root=tmp_path, client_factory=_no_client)

    assert refusal.value.store == case.schema.store
    assert _dump(path) == before


def test_serve_says_a_newer_store_in_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import kpubdata_builder.service.http as http_module
    from kpubdata_builder.cli import main

    started: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: started.append(service))
    path = tmp_path / ".service" / "revisions.sqlite3"
    _write(path, _legacy_revisions)
    _set_version(path, 99)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "error: document revision store" in err and "schema version 99" in err
    assert "Traceback" not in err
    assert started == []


def test_a_service_that_uses_no_store_still_leaves_no_file(tmp_path: Path) -> None:
    """Looking for a newer store creates none."""
    BuilderService(output_root=tmp_path, client_factory=_no_client)

    assert not (tmp_path / ".service").exists()
    assert not (tmp_path / "_publish_receipts.sqlite").exists()


# ---------------------------------------------------- a file from before a column


@pytest.mark.parametrize("case", _WITH_COLUMNS, ids=[c.relative for c in _WITH_COLUMNS])
def test_a_store_from_before_a_column_gains_it_and_keeps_its_rows(
    case: Case, tmp_path: Path
) -> None:
    assert case.oldest is not None and case.gained is not None
    table, gained = case.gained
    path = tmp_path / case.relative
    _write(path, case.oldest)
    before = _rows(path)
    width = len(before[table][0])
    assert _columns(path, table).isdisjoint(gained)

    assert case.open(path) == case.kept_from_oldest

    assert set(gained) <= _columns(path, table)
    assert _versions(path) == [case.schema.version]
    after = _rows(path)
    # Each row is what it was, with the new columns after it.
    assert [row[:width] for row in after[table]] == before[table]
    assert {name: rows for name, rows in after.items() if name != table} == {
        name: rows for name, rows in before.items() if name != table
    }


def test_an_audit_row_from_before_it_named_its_owner_is_still_there(tmp_path: Path) -> None:
    path = tmp_path / "_publish_receipts.sqlite"
    _write(path, _oldest_receipts)

    _open_receipts(path)

    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute(
            "SELECT fingerprint, owner_key, run_id, action FROM publish_receipt_audit"
        ).fetchall()
    assert rows == [("sha256:f", None, None, "reconcile_succeeded")]


# ------------------------------------------------------------ the steps themselves


def _two_step_schema(second: Callable[[sqlite3.Connection], None]) -> StoreSchema:
    def first(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, body TEXT)")

    return StoreSchema(store="note store", migrations=(first, second), remedy="Do nothing.")


def _add_author(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE notes ADD COLUMN author TEXT NOT NULL DEFAULT 'unknown'")


def _connect(path: Path) -> Callable[[], sqlite3.Connection]:
    return lambda: sqlite3.connect(path, timeout=30.0)


def _notes_at_version_one(path: Path) -> None:
    one = StoreSchema(
        store="note store", migrations=_two_step_schema(_add_author).migrations[:1], remedy=""
    )
    one.bring_up_to_date(path, _connect(path))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("INSERT INTO notes (body) VALUES ('kept')")


def test_an_older_version_is_walked_forward_one_step_at_a_time(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite"
    _notes_at_version_one(path)
    assert stored_version(path) == 1

    _two_step_schema(_add_author).bring_up_to_date(path, _connect(path))

    assert stored_version(path) == 2
    assert _rows(path) == {"notes": [(1, "kept", "unknown")]}


def test_a_new_file_runs_every_step(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite"

    _two_step_schema(_add_author).bring_up_to_date(path, _connect(path))

    assert stored_version(path) == 2
    assert _columns(path, "notes") == {"id", "body", "author"}


def test_a_store_at_its_version_takes_no_write_lock(tmp_path: Path) -> None:
    """It is only looked at, read-only: opening it does not queue behind a writer."""
    path = tmp_path / "notes.sqlite"
    schema = _two_step_schema(_add_author)
    schema.bring_up_to_date(path, _connect(path))

    def no_connection() -> sqlite3.Connection:
        raise AssertionError("a store at its version was connected to for a migration")

    schema.bring_up_to_date(path, no_connection)


def test_a_step_that_fails_leaves_the_store_at_the_version_it_had(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite"
    _notes_at_version_one(path)
    before = _dump(path)

    def fails_half_way(conn: sqlite3.Connection) -> None:
        _add_author(conn)
        conn.execute("UPDATE notes SET author = 'changed'")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError, match="interrupted"):
        _two_step_schema(fails_half_way).bring_up_to_date(path, _connect(path))

    assert _dump(path) == before
    assert stored_version(path) == 1

    # And the next start tries again, from the same place.
    _two_step_schema(_add_author).bring_up_to_date(path, _connect(path))
    assert stored_version(path) == 2
    assert _rows(path) == {"notes": [(1, "kept", "unknown")]}


def test_a_failed_adoption_leaves_no_version_behind(tmp_path: Path) -> None:
    """The first step and the version it earns are one transaction."""
    path = tmp_path / "notes.sqlite"
    _write(path, lambda conn: conn.execute("CREATE TABLE other (id INTEGER)"))

    def fails(conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY)")
        raise RuntimeError("interrupted")

    schema = StoreSchema(store="note store", migrations=(fails,), remedy="")
    with pytest.raises(RuntimeError, match="interrupted"):
        schema.bring_up_to_date(path, _connect(path))

    assert stored_version(path) is None
    assert set(_rows(path)) == {"other"}


def test_a_store_made_newer_between_the_look_and_the_lock_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The version is read again under the write lock, not trusted from the first look."""
    from kpubdata_builder.store import schema_version

    path = tmp_path / "notes.sqlite"
    _notes_at_version_one(path)
    real = schema_version.stored_version

    def look_then_upgrade(target: Path) -> int | None:
        found = real(target)
        _set_version(target, 7)
        return found

    monkeypatch.setattr(schema_version, "stored_version", look_then_upgrade)

    with pytest.raises(UnsupportedSchemaVersionError) as refusal:
        _two_step_schema(_add_author).bring_up_to_date(path, _connect(path))

    assert refusal.value.found == 7
    assert _columns(path, "notes") == {"id", "body"}


@pytest.mark.parametrize("case", _WITH_COLUMNS, ids=[c.relative for c in _WITH_COLUMNS])
def test_many_openers_of_one_old_store_add_each_column_once(case: Case, tmp_path: Path) -> None:
    """Two processes starting on one old file: the second waits, then finds it done."""
    assert case.oldest is not None
    path = tmp_path / case.relative
    _write(path, case.oldest)
    start = threading.Barrier(6)
    results: list[object] = []
    errors: list[BaseException] = []

    def opener() -> None:
        try:
            start.wait()
            results.append(case.open(path))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=opener) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == [case.kept_from_oldest] * 6
    assert _versions(path) == [case.schema.version]


# ------------------------------------------------ the ledger on a disk it cannot write


def test_a_ledger_without_a_version_is_still_read_on_a_read_only_disk(tmp_path: Path) -> None:
    """Adopting it needs a write the disk refuses; the users it knows are served (#1121)."""
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    directory = tmp_path / ".service"
    path = directory / "users.sqlite3"
    _write(path, _legacy_ledger)
    path.chmod(0o444)
    directory.chmod(0o555)
    try:
        assert _open_ledger(path) == [("oidc:a", "rejected")]
        assert stored_version(path) is None
    finally:
        directory.chmod(0o755)
        path.chmod(0o644)

    # Writable again: the next start records the version.
    assert _open_ledger(path) == [("oidc:a", "rejected")]
    assert _versions(path) == [ledger_module.SCHEMA.version]


def test_a_newer_ledger_is_refused_on_a_read_only_disk_too(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    directory = tmp_path / ".service"
    path = directory / "users.sqlite3"
    _write(path, _legacy_ledger)
    _set_version(path, ledger_module.SCHEMA.version + 1)
    path.chmod(0o444)
    directory.chmod(0o555)
    try:
        with pytest.raises(UnsupportedSchemaVersionError):
            ledger_module.UserLedger(path)
    finally:
        directory.chmod(0o755)
        path.chmod(0o644)


def test_a_ledger_that_is_not_there_still_fails_on_a_read_only_disk(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    directory = tmp_path / ".service"
    directory.mkdir()
    directory.chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ledger_module.UserLedger(directory / "users.sqlite3")
    finally:
        directory.chmod(0o755)

"""The state store inventory is what the code does and what the document says (#1096).

A list nobody checks is one more thing to drift. This holds it on both sides: to the
modules that open SQLite, and to the table in ``docs/deploy.md``.
"""

from __future__ import annotations

import ast
import re
from dataclasses import replace
from pathlib import Path

import pytest

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS
from kpubdata_builder.store.inventory import (
    NOT_STORES,
    STORES,
    StateStore,
    unversioned_without_reason,
)

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "src" / "kpubdata_builder"
_DOCUMENT = _ROOT / "docs" / "deploy.md"

_IDS = [store.path for store in STORES]


def _opens_sqlite(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
        ):
            return True
    return False


def _modules_that_open_sqlite() -> set[str]:
    found: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        if _opens_sqlite(ast.parse(path.read_text(encoding="utf-8"))):
            found.add(path.relative_to(_SRC).as_posix())
    return found


def _source(store: StateStore) -> str:
    return (_SRC / store.module).read_text(encoding="utf-8")


def test_every_module_that_opens_sqlite_is_accounted_for() -> None:
    """A new store, or a new use of SQLite, has to be put on the list."""
    listed = {store.module for store in STORES}

    assert listed.isdisjoint(NOT_STORES)
    assert _modules_that_open_sqlite() == listed | set(NOT_STORES)


def test_no_two_stores_share_a_module_or_a_file() -> None:
    assert len({store.module for store in STORES}) == len(STORES)
    assert len({(store.root, store.path) for store in STORES}) == len(STORES)


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_file_name_is_the_one_the_code_uses(store: StateStore) -> None:
    name = store.path.rsplit("/", 1)[-1]
    used_in = [_source(store), (_SRC / "service" / "app.py").read_text(encoding="utf-8")]

    assert any(f'"{name}"' in text for text in used_in), name


#: How a connection may state its lock wait: the shared constant, or a name of the
#: module's own that the tests below hold to it.
_WAITS = {"BUSY_TIMEOUT_SECONDS", "PROBE_TIMEOUT_SECONDS"}


def _connects(module: str) -> list[tuple[int, str | None]]:
    """Every ``sqlite3.connect`` call in ``module``: its line and its ``timeout``, as written."""
    source = (_SRC / module).read_text(encoding="utf-8")
    calls: list[tuple[int, str | None]] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "sqlite3"
        ):
            continue
        timeout = next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)
        calls.append((node.lineno, None if timeout is None else ast.unparse(timeout)))
    return calls


@pytest.mark.parametrize("module", sorted(_modules_that_open_sqlite()))
def test_every_connection_waits_the_one_time_for_a_lock(module: str) -> None:
    """No store has a wait of its own, and none is left to SQLite's default of five."""
    calls = _connects(module)

    assert calls, module
    assert [(line, wait) for line, wait in calls if wait not in _WAITS] == []


def test_the_names_a_module_gives_the_wait_are_the_one_wait() -> None:
    from kpubdata_builder import sqlite_settings
    from kpubdata_builder.store import schema_version

    assert schema_version.PROBE_TIMEOUT_SECONDS == BUSY_TIMEOUT_SECONDS
    assert sqlite_settings.BUSY_TIMEOUT_MS == BUSY_TIMEOUT_SECONDS * 1000


@pytest.mark.parametrize("module", sorted(_modules_that_open_sqlite()))
def test_a_wait_set_by_pragma_is_the_one_wait_too(module: str) -> None:
    """``PRAGMA busy_timeout`` is in milliseconds and was once written as a number."""
    source = (_SRC / module).read_text(encoding="utf-8")

    pragmas = re.findall(r"PRAGMA busy_timeout\s*=\s*([^\"\s)]+)", source)

    assert [value for value in pragmas if value != "{BUSY_TIMEOUT_MS}"] == []


def test_the_pragma_check_sees_a_number() -> None:
    assert re.findall(
        r"PRAGMA busy_timeout\s*=\s*([^\"\s)]+)", 'c.execute("PRAGMA busy_timeout=30000")'
    ) == ["30000"]


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_the_list_states_the_one_wait(store: StateStore) -> None:
    assert store.timeout_seconds == BUSY_TIMEOUT_SECONDS


def test_the_check_sees_a_connection_with_a_wait_of_its_own(tmp_path: Path) -> None:
    """What the check above is for: it reads the call, not a comment beside it."""
    tree = ast.parse(
        "import sqlite3\n"
        "a = sqlite3.connect(path, timeout=5.0)\n"
        "b = sqlite3.connect(path)\n"
        "c = sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)\n"
    )
    waits = [
        None
        if (value := next((kw.value for kw in node.keywords if kw.arg == "timeout"), None)) is None
        else ast.unparse(value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]

    assert [wait in _WAITS for wait in waits] == [False, False, True]


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_journal_mode_is_the_one_the_module_sets(store: StateStore) -> None:
    source = _source(store)
    sets_wal = re.search(r"\benable_wal\(", source) is not None

    assert sets_wal == (store.journal == "wal")
    # Through the one helper, which waits for another connection doing the same (#1210).
    assert re.search(r'execute\(\s*"PRAGMA journal_mode\s*=\s*WAL"', source) is None


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_versioning_is_what_the_module_does(store: StateStore) -> None:
    assert _records_a_version(_source(store)) == (store.versioning == "schema_version")


def _records_a_version(source: str) -> bool:
    """Whether a module keeps a version table: its own, or through ``StoreSchema``."""
    own_table = "CREATE TABLE IF NOT EXISTS schema_version" in source
    shared = (
        re.search(r"\bStoreSchema\(", source) is not None
        and re.search(r"\.bring_up_to_date\(", source) is not None
    )
    return own_table or shared


def test_the_version_check_tells_a_store_that_records_none() -> None:
    """A store that only adds the columns it finds missing records no version."""
    assert not _records_a_version(
        'conn.execute("CREATE TABLE IF NOT EXISTS t (a TEXT)")\n'
        'conn.execute("ALTER TABLE t ADD COLUMN b TEXT")\n'
    )
    # Declaring a schema is not using it.
    assert not _records_a_version("SCHEMA = StoreSchema(store='t', migrations=(), remedy='')\n")
    assert _records_a_version(
        "SCHEMA = StoreSchema(store='t', migrations=(), remedy='')\n"
        "SCHEMA.bring_up_to_date(path, connect)\n"
    )


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_every_store_that_records_a_version_refuses_a_newer_one(store: StateStore) -> None:
    """The version is there to be checked: a newer file ends in the one refusal."""
    if store.versioning != "schema_version":
        pytest.skip("records no version")
    source = _source(store)

    # Through ``StoreSchema``, which refuses; or by raising the refusal itself; or, for
    # the catalog, by finding no migration path from the version (``SnapshotStateError``).
    assert (
        re.search(r"\.bring_up_to_date\(", source) is not None
        or "UnsupportedSchemaVersionError(" in source
        or "_migration_path(" in source
    )


def test_a_store_without_a_version_says_why() -> None:
    """No store is left unversioned by default; an exception carries its reason."""
    assert unversioned_without_reason() == []


def test_the_reason_check_sees_a_store_that_gives_none() -> None:
    silent = StateStore(
        name="cache",
        module="cache.py",
        root="output",
        path="cache.sqlite",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="unversioned",
        if_lost="nothing",
    )
    explained = replace(silent, path="explained.sqlite", unversioned_because="rebuilt at start")
    versioned_with_a_reason = replace(
        explained, path="confused.sqlite", versioning="schema_version"
    )

    assert unversioned_without_reason((silent, explained, versioned_with_a_reason)) == [
        "cache.sqlite",
        "confused.sqlite",
    ]


def _row(store: StateStore) -> str:
    where = "출력 디렉터리" if store.root == "output" else "웨어하우스"
    journal = "WAL" if store.journal == "wal" else "기본(rollback journal)"
    versioning = (
        "버전 표" if store.versioning == "schema_version" else f"없음 — {store.unversioned_because}"
    )
    return (
        f"| {store.name} | {where}의 `{store.path}` | {store.timeout_seconds:g}초 | {journal} "
        f"| {versioning} | {store.if_lost} |"
    )


def test_the_document_has_a_row_for_every_store_as_listed() -> None:
    """Printed on failure: the rows to paste into ``docs/deploy.md``."""
    document = _DOCUMENT.read_text(encoding="utf-8")
    rows = [_row(store) for store in STORES]

    missing = [row for row in rows if row not in document]

    assert not missing, "docs/deploy.md is stale; its table should hold:\n" + "\n".join(rows)


def test_the_document_names_no_store_the_list_does_not() -> None:
    document = _DOCUMENT.read_text(encoding="utf-8")
    start = document.index("### 6.0.1 ")
    section = document[start : document.index("\n### ", start + 1)]

    table_rows = [line for line in section.splitlines() if line.startswith("| ") and "`" in line]

    assert len(table_rows) == len(STORES)

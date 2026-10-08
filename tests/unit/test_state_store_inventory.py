"""The state store inventory is what the code does and what the document says (#1096).

A list nobody checks is one more thing to drift. This holds it on both sides: to the
modules that open SQLite, and to the table in ``docs/deploy.md``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from kpubdata_builder.store.inventory import NOT_STORES, STORES, StateStore

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


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_timeout_is_the_one_the_module_connects_with(store: StateStore) -> None:
    source = _source(store)
    seconds = {float(value) for value in re.findall(r"(?<!\w)timeout=(\d+(?:\.\d+)?)", source)}
    # A ``PRAGMA busy_timeout`` set beside it is in milliseconds and has to agree.
    seconds |= {int(value) / 1000 for value in re.findall(r"busy_timeout=(\d+)", source)}
    milliseconds = re.search(r"^_BUSY_TIMEOUT_MS = ([\d_]+)$", source, re.MULTILINE)
    if milliseconds:
        seconds.add(int(milliseconds[1].replace("_", "")) / 1000)

    assert seconds == {store.timeout_seconds}


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_journal_mode_is_the_one_the_module_sets(store: StateStore) -> None:
    sets_wal = re.search(r'execute\(\s*"PRAGMA journal_mode\s*=\s*WAL"', _source(store)) is not None

    assert sets_wal == (store.journal == "wal")


@pytest.mark.parametrize("store", STORES, ids=_IDS)
def test_versioning_is_what_the_module_does(store: StateStore) -> None:
    source = _source(store)
    has_version_table = "CREATE TABLE IF NOT EXISTS schema_version" in source
    adds_columns = re.search(r"ALTER TABLE \S+ ADD COLUMN", source) is not None

    if store.versioning == "schema_version":
        assert has_version_table
    else:
        assert not has_version_table
        assert adds_columns == (store.versioning == "columns")


def _row(store: StateStore) -> str:
    where = "출력 디렉터리" if store.root == "output" else "웨어하우스"
    journal = "WAL" if store.journal == "wal" else "기본(rollback journal)"
    versioning = {
        "schema_version": "버전 표",
        "columns": "없음 — 빠진 열을 열 때 더한다",
        "none": "없음",
    }[store.versioning]
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

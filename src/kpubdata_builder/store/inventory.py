"""Every SQLite state store Builder keeps, written once (#1096).

Ten stores are opened from ten modules, each with the connection settings its author
chose: four use WAL and six do not, three record a schema version and three more add
columns in place. None of that was written down anywhere, so nobody could say what a new
store should do or whether an existing one was an exception on purpose. How long a
connection waits for a lock differed too — two waited five seconds and the rest thirty —
and is now one value for all of them (``sqlite_settings``).

This is the list. ``tests/unit/test_state_store_inventory.py`` holds it to the code —
a module that opens SQLite and is not here fails, and so does a connection that waits
some other time for a lock, or an entry whose journal mode or versioning is not what its
module does — and to the table in
``docs/deploy.md``. A change to how a store connects is then a change to this file,
made on purpose.

This module describes the stores; it does not open them. The descriptions are the
operator documentation and are written in Korean, as the deployment guide is.
"""

# One store per entry; the descriptions are prose and run as long as they need to.
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS

#: ``wal``: the store sets ``PRAGMA journal_mode=WAL``. ``default``: it sets nothing and
#: runs in SQLite's rollback-journal mode.
JournalMode = Literal["wal", "default"]

#: ``schema_version``: a version table, checked when the store is opened.
#: ``columns``: no version; a missing column is added in place when the store is opened.
#: ``none``: the tables are created if absent and have never changed.
Versioning = Literal["schema_version", "columns", "none"]

#: Where the file is, relative to the directory named.
Root = Literal["output", "warehouse"]


@dataclass(frozen=True)
class StateStore:
    """One SQLite file Builder keeps state in."""

    #: What it holds, as the deployment guide says it.
    name: str
    #: The module that opens it, relative to the package.
    module: str
    root: Root
    #: The file, relative to ``root``.
    path: str
    #: Seconds a connection waits for a lock before it fails.
    timeout_seconds: float
    journal: JournalMode
    versioning: Versioning
    #: What losing the file costs, as the deployment guide says it.
    if_lost: str


STORES: tuple[StateStore, ...] = (
    StateStore(
        name="빌드 인덱스",
        module="store/build_index.py",
        root="output",
        path="_builds.sqlite",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="wal",
        versioning="schema_version",
        if_lost="잃어도 된다 — manifest 와, manifest 없이 끝난 run 은 run 이벤트 저장소의 종료 기록에서 다시 만든다(`serve` 가 기동할 때, 또는 `rebuild-index`)",
    ),
    StateStore(
        name="run 이벤트와 제출 기록",
        module="events/store.py",
        root="output",
        path="_build_events.sqlite",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="wal",
        versioning="schema_version",
        if_lost="run 의 타임라인과 제출자 기록, manifest 없이 끝난 run 의 종료 기록(#1120)을 잃는다. 다시 만들 수 없다",
    ),
    StateStore(
        name="게시 영수증",
        module="service/publish.py",
        root="output",
        path="_publish_receipts.sqlite",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="wal",
        versioning="columns",
        if_lost="어떤 run 을 어디에 게시했는지와, 같은 게시가 두 번 나가는 것을 막는 근거를 잃는다. 다시 만들 수 없다",
    ),
    StateStore(
        name="provider 자격 증명 (암호화)",
        module="credentials/store.py",
        root="output",
        path=".service/provider-credentials.sqlite3",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="none",
        if_lost="사용자가 저장한 provider 키를 잃는다. 각자 다시 입력해야 한다",
    ),
    StateStore(
        name="업로드",
        module="uploads/store.py",
        root="output",
        path=".service/uploads.sqlite3",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="columns",
        if_lost="올린 파일과 그 목록을 잃는다(큰 파일의 내용은 옆의 `uploads.sqlite3.blobs/` 에 있다)",
    ),
    StateStore(
        name="provider 연결 테스트의 마지막 결과",
        module="service/provider_tests.py",
        root="output",
        path=".service/provider_tests.sqlite3",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="none",
        if_lost="잃어도 된다 — 연결 테스트를 다시 하면 채워진다",
    ),
    StateStore(
        name="문서 revision 과 감사 기록",
        module="service/revisions.py",
        root="output",
        path=".service/revisions.sqlite3",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="none",
        if_lost="BuildSpec 과 표시 주석의 저장 이력을 잃는다. 다시 만들 수 없다",
    ),
    StateStore(
        name="가입 원장",
        module="service/user_ledger.py",
        root="output",
        path=".service/users.sqlite3",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="none",
        if_lost="가입 승인·거절 기록을 잃는다. allowlist 에 없는 사용자는 다시 승인을 기다린다",
    ),
    StateStore(
        name="저장한 분석",
        module="service/analyses_api.py",
        root="output",
        path=".service/analyses.sqlite3",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="default",
        versioning="columns",
        if_lost="저장한 SQL 과 그것이 읽은 스냅샷의 기록을 잃는다. 다시 만들 수 없다",
    ),
    StateStore(
        name="테이블 카탈로그",
        module="warehouse/catalog.py",
        root="warehouse",
        path="_warehouse.sqlite",
        timeout_seconds=BUSY_TIMEOUT_SECONDS,
        journal="wal",
        versioning="schema_version",
        if_lost="어떤 스냅샷이 어느 테이블의 현재 것인지를 잃는다. 다시 만들 수 없다",
    ),
)

#: Modules that open SQLite without owning a store, and why.
NOT_STORES: dict[str, str] = {
    "store/schema_version.py": "reads a store's version on a read-only connection",
    "warehouse/backup.py": "copies the catalog and reads the copy",
}

__all__ = ["NOT_STORES", "STORES", "JournalMode", "Root", "StateStore", "Versioning"]

"""A request reads the sign-up ledger and writes it only when something changed (#1121).

Every authenticated request updated ``last_seen_at``. A disk that could not be written —
full, read-only — then answered every signed-in user with a 500, though the ledger could
still be read and nothing about them had changed.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kpubdata_builder.service import BuilderService
from kpubdata_builder.service import user_ledger as ledger_module
from kpubdata_builder.service.user_ledger import (
    LAST_SEEN_REFRESH_SECONDS,
    LedgerUnavailableError,
    UserLedger,
)

from .test_signup_ledger import _ADMIN, _LISTED, _NEWCOMER, _as, _code, _service, _user

_Statements = Sequence[tuple[str, tuple[object, ...]]]


class _Writes:
    """Counts the ledger's write transactions, and can make them fail like a full disk."""

    def __init__(self, ledger: UserLedger, monkeypatch: pytest.MonkeyPatch) -> None:
        self.count = 0
        self.failing = False
        real = ledger._write

        def write(statements: _Statements) -> None:
            self.count += 1
            if self.failing:
                raise sqlite3.OperationalError("database or disk is full")
            real(statements)

        monkeypatch.setattr(ledger, "_write", write)


def _ledger(tmp_path: Path) -> UserLedger:
    return UserLedger(tmp_path / "users.sqlite3")


def _age_last_seen(ledger: UserLedger, user_id: str | None, seconds: float) -> str:
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(timespec="seconds")
    with ledger._connect() as conn:
        conn.execute("UPDATE users SET last_seen_at = ? WHERE user_id = ?", (stamp, user_id))
    return stamp


# ------------------------------------------------------------------ what is written


def test_a_known_user_is_read_and_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    first = ledger.observe(_LISTED)
    writes = _Writes(ledger, monkeypatch)

    for _ in range(20):
        assert ledger.observe(_LISTED) == first

    assert writes.count == 0


def test_last_seen_is_written_again_once_it_is_an_hour_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    ledger.observe(_LISTED)
    writes = _Writes(ledger, monkeypatch)

    _age_last_seen(ledger, _LISTED.owner_id, LAST_SEEN_REFRESH_SECONDS - 60)
    ledger.observe(_LISTED)
    assert writes.count == 0

    stale = _age_last_seen(ledger, _LISTED.owner_id, LAST_SEEN_REFRESH_SECONDS + 60)
    seen = ledger.observe(_LISTED)
    assert writes.count == 1
    assert seen.last_seen_at > stale
    # And then not again until another hour has passed.
    ledger.observe(_LISTED)
    assert writes.count == 1


def test_a_new_display_name_is_written_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _ledger(tmp_path)
    ledger.observe(_user("renamed"))
    writes = _Writes(ledger, monkeypatch)
    renamed = _user("renamed")
    renamed = type(renamed)(**{**renamed.__dict__, "display_name": "new-name@example.com"})

    assert ledger.observe(renamed).display_name == "new-name@example.com"

    assert writes.count == 1
    assert ledger.list()[0].display_name == "new-name@example.com"


# ----------------------------------------------- a decision is seen on the next request


def test_a_rejection_takes_effect_on_the_next_request_without_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    writes = _Writes(service._user_ledger(), monkeypatch)

    service._user_ledger().decide(str(_LISTED.owner_id), "rejected", by="admin")

    refused = _as(service, monkeypatch, _LISTED, "GET", "/version")
    assert refused.status_code == 403 and _code(refused) == "signup_rejected"
    assert writes.count == 0


def test_the_list_does_not_undo_a_rejection_made_while_the_user_was_pending(
    tmp_path: Path,
) -> None:
    """The admission by the list is written only while the row is still pending."""
    ledger = _ledger(tmp_path)
    ledger.observe(_NEWCOMER)
    ledger.decide(str(_NEWCOMER.owner_id), "rejected", by="admin")
    now_listed = type(_NEWCOMER)(**{**_NEWCOMER.__dict__, "admitted": True})

    assert ledger.observe(now_listed).status == "rejected"
    assert ledger.list()[0].decided_by == "admin"


# --------------------------------------------------------- a disk that cannot be written


def test_a_known_user_is_served_when_the_ledger_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    service = _service(tmp_path)
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    ledger = service._user_ledger()
    _age_last_seen(ledger, _LISTED.owner_id, LAST_SEEN_REFRESH_SECONDS * 2)
    writes = _Writes(ledger, monkeypatch)
    writes.failing = True

    # A write is due (last seen two hours ago) and fails; the user is still let in.
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    assert writes.count == 1
    assert "could not be written" in caplog.text

    # The disk has room again: the next request saves what the last one could not.
    writes.failing = False
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    assert writes.count == 2
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    assert writes.count == 2


def test_a_rejected_user_stays_out_when_the_ledger_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Negative: serving the entry as read never turns a refusal into an admission."""
    service = _service(tmp_path)
    _as(service, monkeypatch, _LISTED, "GET", "/version")
    ledger = service._user_ledger()
    ledger.decide(str(_LISTED.owner_id), "rejected", by="admin")
    _age_last_seen(ledger, _LISTED.owner_id, LAST_SEEN_REFRESH_SECONDS * 2)
    _Writes(ledger, monkeypatch).failing = True

    refused = _as(service, monkeypatch, _LISTED, "GET", "/version")

    assert refused.status_code == 403 and _code(refused) == "signup_rejected"


def test_a_pending_user_stays_pending_when_the_ledger_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    _as(service, monkeypatch, _NEWCOMER, "GET", "/version")
    ledger = service._user_ledger()
    _age_last_seen(ledger, _NEWCOMER.owner_id, LAST_SEEN_REFRESH_SECONDS * 2)
    _Writes(ledger, monkeypatch).failing = True

    waiting = _as(service, monkeypatch, _NEWCOMER, "GET", "/version")

    assert waiting.status_code == 403 and _code(waiting) == "signup_pending"


def test_a_first_sign_in_that_cannot_be_recorded_is_answered_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    ledger = service._user_ledger()
    writes = _Writes(ledger, monkeypatch)
    writes.failing = True

    for newcomer in (_NEWCOMER, _LISTED):
        answer = _as(service, monkeypatch, newcomer, "GET", "/version")
        assert answer.status_code == 503
        assert _code(answer) == "signup_ledger_unavailable"
        # Says nothing of the disk, the path or the user.
        assert "disk" not in str(answer.body) and str(tmp_path) not in str(answer.body)
    assert ledger.list() == []

    # Room again: the same users sign in as they would have.
    writes.failing = False
    assert _code(_as(service, monkeypatch, _NEWCOMER, "GET", "/version")) == "signup_pending"
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200


def test_a_ledger_that_cannot_be_read_admits_nobody_but_an_administrator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    ledger = service._user_ledger()

    def unreadable(_user_id: str) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ledger, "_read", unreadable)

    # Approved a moment ago — and not let in on the strength of a ledger nobody can read.
    answer = _as(service, monkeypatch, _LISTED, "GET", "/version")
    assert answer.status_code == 503 and _code(answer) == "signup_ledger_unavailable"
    with pytest.raises(LedgerUnavailableError):
        ledger.observe(_LISTED)
    # The administrator is never held by the ledger, and can look at what is wrong.
    assert _as(service, monkeypatch, _ADMIN, "GET", "/version").status_code == 200


# ------------------------------------------------------------------------ concurrency


def test_two_first_requests_of_one_user_make_one_row(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    barrier = threading.Barrier(8)
    seen: list[str] = []
    errors: list[BaseException] = []

    def sign_in() -> None:
        try:
            barrier.wait(timeout=5)
            seen.append(ledger.observe(_NEWCOMER).status)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=sign_in) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert seen == ["pending"] * 8
    assert len(ledger.list()) == 1


def test_a_first_sign_in_and_a_rejection_arriving_together_never_end_approved(
    tmp_path: Path,
) -> None:
    """A user the list admits is rejected by an administrator while their requests run."""
    ledger = _ledger(tmp_path)
    ledger.observe(_NEWCOMER)
    now_listed = type(_NEWCOMER)(**{**_NEWCOMER.__dict__, "admitted": True})
    stop = threading.Event()

    def keep_signing_in() -> None:
        while not stop.is_set():
            ledger.observe(now_listed)

    worker = threading.Thread(target=keep_signing_in)
    worker.start()
    try:
        ledger.decide(str(_NEWCOMER.owner_id), "rejected", by="admin")
    finally:
        stop.set()
        worker.join(timeout=10)

    assert ledger.observe(now_listed).status == "rejected"


def test_the_ledger_waits_for_a_busy_writer_and_is_not_in_wal_mode(tmp_path: Path) -> None:
    """WAL would make the file unreadable from a directory that cannot be written."""
    ledger = _ledger(tmp_path)

    with ledger._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == ledger_module._BUSY_TIMEOUT_MS


# ------------------------------------------------- a filesystem that is really read-only


@pytest.fixture()
def read_only_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[BuilderService]:
    """A service whose ledger file and directory have been made read-only on disk, after
    one listed and one pending user signed in. Nothing is patched: the failures below
    are the operating system's."""
    if os.geteuid() == 0:
        pytest.skip("root writes through a read-only mode")
    service = _service(tmp_path)
    assert _as(service, monkeypatch, _LISTED, "GET", "/version").status_code == 200
    assert _code(_as(service, monkeypatch, _NEWCOMER, "GET", "/version")) == "signup_pending"
    directory = tmp_path / ".service"
    ledger_file = directory / "users.sqlite3"
    # Whatever SQLite left beside the file while it could write is what it will find.
    _age_last_seen(service._user_ledger(), _LISTED.owner_id, LAST_SEEN_REFRESH_SECONDS * 2)
    ledger_file.chmod(0o444)
    directory.chmod(0o555)
    try:
        with pytest.raises(sqlite3.OperationalError):
            service._user_ledger()._write([("UPDATE users SET display_name = ?", ("x",))])
        yield service
    finally:
        directory.chmod(0o755)
        ledger_file.chmod(0o644)


def test_on_a_read_only_filesystem_a_known_user_is_still_read_and_served(
    read_only_service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A write is due — last seen two hours ago — and the disk refuses it.
    for _ in range(3):
        assert _as(read_only_service, monkeypatch, _LISTED, "GET", "/version").status_code == 200


def test_on_a_read_only_filesystem_refusals_are_still_read(
    read_only_service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    waiting = _as(read_only_service, monkeypatch, _NEWCOMER, "GET", "/version")

    assert waiting.status_code == 403 and _code(waiting) == "signup_pending"


def test_on_a_read_only_filesystem_a_first_sign_in_is_unavailable_not_admitted(
    read_only_service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    stranger = _user("stranger", admitted=True)

    answer = _as(read_only_service, monkeypatch, stranger, "GET", "/version")

    assert answer.status_code == 503 and _code(answer) == "signup_ledger_unavailable"


def test_when_the_filesystem_is_writable_again_what_was_not_saved_is_saved(
    tmp_path: Path, read_only_service: BuilderService, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = read_only_service._user_ledger()
    before = {entry.user_id: entry.last_seen_at for entry in ledger.list()}
    _as(read_only_service, monkeypatch, _LISTED, "GET", "/version")
    assert {e.user_id: e.last_seen_at for e in ledger.list()} == before

    (tmp_path / ".service").chmod(0o755)
    (tmp_path / ".service" / "users.sqlite3").chmod(0o644)
    assert _as(read_only_service, monkeypatch, _LISTED, "GET", "/version").status_code == 200

    after = {entry.user_id: entry.last_seen_at for entry in ledger.list()}
    assert after[str(_LISTED.owner_id)] > before[str(_LISTED.owner_id)]


def test_a_service_without_oidc_users_still_never_opens_the_ledger(tmp_path: Path) -> None:
    """Negative: nothing here makes a deployment without OIDC touch the file."""
    service = BuilderService(output_root=tmp_path, client_factory=lambda **_: object())

    assert service._user_ledger_store is None
    assert not (tmp_path / ".service" / "users.sqlite3").exists()

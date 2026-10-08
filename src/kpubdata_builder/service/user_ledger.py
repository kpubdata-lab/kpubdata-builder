"""The Builder sign-up ledger (#785, option B).

ADR 0012's 2026-09-30 amendment: in a multi-user deployment sign-up needs an allowlist,
and the approval ledger belongs to Builder — no identity-provider admin credential is
kept here. A user signing in through OIDC for the first time is recorded as
``pending``; an administrator marks them ``approved`` or ``rejected``, with no restart.

- A user on an ``OIDC_ALLOWED_*`` list (or an administrator) is admitted by sign-in and
  recorded as ``approved`` with ``decided_by: allowlist``, so the list stays the
  operator's tool and the ledger shows everyone who has signed in.
- ``rejected`` blocks a user even when a list admits them: an administrator can shut an
  account out at once, without editing the environment and restarting.
- Administrators are never blocked — the ledger cannot lock out the people who run it.
- Only OIDC principals are recorded. A deployment without OIDC never touches this.

The row key is ``owner_id``, an irreversible hash; the display name is what the
administrator needs to recognise the person (their verified email). No token or
credential is stored.

A request reads the ledger and seldom writes it (#1121). Every authenticated request
used to update ``last_seen_at``, so a disk that could not be written — full, read-only —
answered every signed-in user with a 500, though nothing about them had changed:

- **Read on every request, never cached.** A rejection takes effect on the user's next
  request, and a ledger that cannot be read admits nobody (``LedgerUnavailableError``).
- **Written when something changes**: a first sign-in, a pending user the list now
  admits, a new display name — and ``last_seen_at`` at most once per
  ``LAST_SEEN_REFRESH_SECONDS``, so it says when the user was last here to the hour.
- **A failed write does not shut out a user the ledger already knows.** Their status was
  read; what could not be saved is logged and saved by a later request. Only a first
  sign-in, which has no row to read, fails — as ``LedgerUnavailableError``.

One request can get ahead of a decision: a pending user the list has come to admit is
let in on the request that could not save that admission, even if an administrator
rejected them in the moment after the row was read. The next request reads the
rejection.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS

SignupStatus = Literal["pending", "approved", "rejected"]

logger = logging.getLogger(__name__)

#: How stale ``last_seen_at`` may get before a request writes it again (#1121).
LAST_SEEN_REFRESH_SECONDS = 3600.0
#: A writer waits for another instead of failing at once — the value the warehouse
#: catalog and the build index use.
#:
#: Their WAL journal mode is deliberately **not** used here. A WAL database cannot be
#: read from a directory that cannot be written: every connection, a reader included,
#: has to create the ``-shm`` file, and this ledger opens a connection per request. On a
#: read-only filesystem a ``SELECT`` then fails with "attempt to write a readonly
#: database" — the very case this module exists to survive. In the default rollback
#: journal mode a reader creates nothing. What that costs is a reader waiting out a
#: writer's commit, and a request writes at most once an hour for a user.
_BUSY_TIMEOUT_MS = int(BUSY_TIMEOUT_SECONDS * 1000)


class LedgerUnavailableError(Exception):
    """The ledger could not say whether this user is admitted.

    Raised when it cannot be read, or when a first sign-in cannot be recorded. The
    caller answers that the service cannot admit the user right now; it never lets them
    in on the strength of a ledger it could not read. Carries no detail of the failure.
    """


@dataclass(frozen=True)
class LedgerEntry:
    user_id: str
    display_name: str | None
    status: SignupStatus
    first_seen_at: str
    last_seen_at: str
    decided_at: str | None
    decided_by: str | None

    def body(self) -> dict[str, JsonValue]:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _last_seen_is_stale(last_seen_at: str, now: str) -> bool:
    try:
        elapsed = datetime.fromisoformat(now) - datetime.fromisoformat(last_seen_at)
    except (TypeError, ValueError):
        return True
    return elapsed.total_seconds() >= LAST_SEEN_REFRESH_SECONDS


class UserLedger:
    """SQLite ledger of OIDC users and their sign-up status."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS users ("
                " user_id TEXT PRIMARY KEY, display_name TEXT, status TEXT NOT NULL,"
                " first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,"
                " decided_at TEXT, decided_by TEXT)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=_BUSY_TIMEOUT_MS / 1000)) as conn:
            conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            with conn:
                yield conn

    def _read(self, user_id: str) -> LedgerEntry | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return None if row is None else LedgerEntry(*row)

    def _write(self, statements: Sequence[tuple[str, tuple[object, ...]]]) -> None:
        """Run ``statements`` in one transaction. The one place a request writes."""
        with self._lock, self._connect() as conn:
            for sql, parameters in statements:
                conn.execute(sql, parameters)

    def observe(self, principal: Principal) -> LedgerEntry:
        """The user's entry, created on first sight and written only when it changes.

        Raises:
            LedgerUnavailableError: The ledger could not be read, or this is a first
                sign-in and it could not be recorded.
        """
        if principal.owner_id is None:
            raise ValueError("an OIDC principal always has an owner_id")
        user_id = principal.owner_id
        try:
            entry = self._read(user_id)
        except sqlite3.Error as exc:
            logger.error("sign-up ledger could not be read: %s", type(exc).__name__)
            raise LedgerUnavailableError from exc
        now = _now()
        if entry is None:
            return self._record_first_sign_in(principal, now)

        updated = entry
        statements: list[tuple[str, tuple[object, ...]]] = []
        if principal.display_name and principal.display_name != entry.display_name:
            updated = replace(updated, display_name=principal.display_name)
        admitted_since = entry.status == "pending" and principal.admitted
        if updated is not entry or admitted_since or _last_seen_is_stale(entry.last_seen_at, now):
            updated = replace(updated, last_seen_at=now)
            statements.append(
                (
                    "UPDATE users SET last_seen_at = ?, display_name = ? WHERE user_id = ?",
                    (now, updated.display_name, user_id),
                )
            )
        if admitted_since:
            # Put on a list since signing up: the list admits them now. Only while still
            # pending — an administrator's decision made meanwhile is not overwritten.
            updated = replace(updated, status="approved", decided_at=now, decided_by="allowlist")
            statements.append(
                (
                    "UPDATE users SET status = 'approved', decided_at = ?,"
                    " decided_by = 'allowlist' WHERE user_id = ? AND status = 'pending'",
                    (now, user_id),
                )
            )
        if not statements:
            return entry
        try:
            self._write(statements)
        except sqlite3.Error as exc:
            # The user's status was read and stands. What could not be saved — when they
            # were last seen, a new name, the list's admission — is saved by a later
            # request; the list admits them on this one too.
            logger.warning(
                "sign-up ledger could not be written (%s); serving the entry as read",
                type(exc).__name__,
            )
            return updated
        if admitted_since:
            # Read back: an administrator may have decided while this request ran.
            try:
                return self._read(user_id) or updated
            except sqlite3.Error as exc:
                raise LedgerUnavailableError from exc
        return updated

    def _record_first_sign_in(self, principal: Principal, now: str) -> LedgerEntry:
        auto = principal.admitted
        assert principal.owner_id is not None  # noqa: S101 - checked by the caller
        try:
            # OR IGNORE: two first requests of one user arrive together, and both read
            # "no row" before either wrote.
            self._write(
                [
                    (
                        "INSERT OR IGNORE INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            principal.owner_id,
                            principal.display_name,
                            "approved" if auto else "pending",
                            now,
                            now,
                            now if auto else None,
                            "allowlist" if auto else None,
                        ),
                    )
                ]
            )
            entry = self._read(principal.owner_id)
        except sqlite3.Error as exc:
            logger.error("a first sign-in could not be recorded: %s", type(exc).__name__)
            raise LedgerUnavailableError from exc
        if entry is None:  # pragma: no cover - the row was just written
            raise LedgerUnavailableError
        return entry

    def decide(self, user_id: str, status: SignupStatus, *, by: str) -> LedgerEntry | None:
        """Set a user's status; None when no such user has signed in."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE users SET status = ?, decided_at = ?, decided_by = ? WHERE user_id = ?",
                (status, _now(), by, user_id),
            )
            if cursor.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return LedgerEntry(*row)

    def list(self, *, status: SignupStatus | None = None) -> list[LedgerEntry]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM users ORDER BY first_seen_at DESC, user_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM users WHERE status = ? ORDER BY first_seen_at DESC, user_id",
                    (status,),
                ).fetchall()
        return [LedgerEntry(*row) for row in rows]


def admission_refusal(entry: LedgerEntry, principal: Principal) -> dict[str, JsonValue] | None:
    """The 403 body for a principal the ledger does not let in, or None to let them in."""
    if principal.is_admin:
        return None
    if entry.status == "rejected":
        return {"error": "this account's sign-up was rejected", "code": "signup_rejected"}
    if entry.status == "approved":
        return None
    return {
        "error": "this account's sign-up is waiting for an administrator's approval",
        "code": "signup_pending",
    }


__all__ = [
    "LAST_SEEN_REFRESH_SECONDS",
    "LedgerEntry",
    "LedgerUnavailableError",
    "SignupStatus",
    "UserLedger",
    "admission_refusal",
]

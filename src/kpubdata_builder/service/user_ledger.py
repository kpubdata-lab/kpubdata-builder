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
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue

SignupStatus = Literal["pending", "approved", "rejected"]


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
        with closing(sqlite3.connect(self._path, timeout=30)) as conn, conn:
            yield conn

    def observe(self, principal: Principal) -> LedgerEntry:
        """Record a sign-in and return the user's entry, creating it on first sight."""
        if principal.owner_id is None:
            raise ValueError("an OIDC principal always has an owner_id")
        now = _now()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (principal.owner_id,)
            ).fetchone()
            if row is None:
                auto = principal.admitted
                conn.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
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
            else:
                conn.execute(
                    "UPDATE users SET last_seen_at = ?, display_name = ? WHERE user_id = ?",
                    (now, principal.display_name or row[1], principal.owner_id),
                )
                if row[2] == "pending" and principal.admitted:
                    # Put on a list since signing up: the list admits them now.
                    conn.execute(
                        "UPDATE users SET status = 'approved', decided_at = ?,"
                        " decided_by = 'allowlist' WHERE user_id = ?",
                        (now, principal.owner_id),
                    )
            row = conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (principal.owner_id,)
            ).fetchone()
        return LedgerEntry(*row)

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


__all__ = ["LedgerEntry", "SignupStatus", "UserLedger", "admission_refusal"]

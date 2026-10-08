"""Immutable revisions of edited documents, with concurrency checks and an audit trail (#820).

Warehouse tables have a revision for committing snapshots (#713, #787), but nothing a
person edits — a BuildSpec, a display annotation — had one. This store keeps every save
as a new, immutable revision:

- **Concurrency by expected revision.** A save names the revision it was based on
  (``expected_revision``; 0 for a new document). If another save got there first, it is
  refused with the current revision, never merged or overwritten.
- **Idempotent retries.** A save may carry an ``idempotency_key``; repeating it returns
  the revision the first attempt made instead of adding another.
- **Revert is a new revision** with an old revision's content — history is never
  rewritten.
- **Server-decided author and time**, and an audit entry written in the same transaction
  as the revision, so a document change without its audit record cannot exist.
- **No credential is stored.** Content that carries a credential-named field
  (``serviceKey``, ``api_key`` …) or the value of a key the current request carries is
  refused, so a spec pasted with its key never reaches the store, its history or its
  audit trail.

Documents are scoped like warehouse tables (``ownership.warehouse_workspace``): another
owner's document is absent, not forbidden.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS

from .. import logging_redaction
from ..spec import JsonValue
from . import request_credentials

#: Kinds of document the store keeps. A saved analysis (#783) and a correction set
#: (#822) have their own stores; they can move here when they need editing history.
REVISION_KINDS: tuple[str, ...] = ("spec", "annotation")
_MAX_CONTENT_BYTES = 1024 * 1024


class RevisionConflict(Exception):
    """The document moved on since the revision the save was based on."""

    def __init__(self, current: int) -> None:
        super().__init__(f"the document is at revision {current}")
        self.current = current


class CredentialInContent(ValueError):
    """The content carries a credential; the message names where, never the value."""


@dataclass(frozen=True)
class Revision:
    kind: str
    doc_id: str
    revision: int
    content: JsonValue
    note: str | None
    author: str
    created_at: str
    reverted_from: int | None

    def body(self, *, with_content: bool = True) -> dict[str, JsonValue]:
        body: dict[str, JsonValue] = {
            "kind": self.kind,
            "doc_id": self.doc_id,
            "revision": self.revision,
            "note": self.note,
            "author": self.author,
            "created_at": self.created_at,
            "reverted_from": self.reverted_from,
        }
        if with_content:
            body["content"] = self.content
        return body


def credential_paths(content: JsonValue, path: str = "content") -> list[str]:
    """Where ``content`` carries a credential: a credential-named field with a value, a
    ``name=value`` credential parameter in text, or a key the current request carries."""
    found: list[str] = []
    request_values = [v for v in request_credentials.current_keys().values() if v]
    if isinstance(content, Mapping):
        for key, value in content.items():
            where = f"{path}.{key}"
            if str(key).casefold() in logging_redaction.SENSITIVE_PARAM_KEYS and value not in (
                None,
                "",
            ):
                found.append(where)
            else:
                found.extend(credential_paths(value, where))
    elif isinstance(content, list):
        for index, item in enumerate(content):
            found.extend(credential_paths(item, f"{path}[{index}]"))
    elif isinstance(content, str) and (
        logging_redaction.redact(content) != content
        or any(v in content for v in request_values)
        or _credential_line(content)
    ):
        found.append(path)
    return found


#: A ``name: value`` line — YAML or similar text — whose name is a credential parameter.
_KEY_VALUE_LINE = re.compile(
    r"""^\s*-?\s*["']?(?P<name>[A-Za-z_][\w-]*)["']?\s*:\s*(?P<value>\S.*)$"""
)


def _credential_line(text: str) -> bool:
    """Whether ``text`` has a line setting a credential-named field to a value."""
    for line in text.splitlines():
        match = _KEY_VALUE_LINE.match(line)
        if (
            match is not None
            and match.group("name").casefold() in logging_redaction.SENSITIVE_PARAM_KEYS
            and match.group("value").strip().strip("\"'") not in ("", "null", "~")
        ):
            return True
    return False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RevisionStore:
    """SQLite store of document revisions and their audit trail."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS revisions ("
                " workspace TEXT NOT NULL, kind TEXT NOT NULL, doc_id TEXT NOT NULL,"
                " revision INTEGER NOT NULL, content TEXT NOT NULL, note TEXT,"
                " author TEXT NOT NULL, created_at TEXT NOT NULL, reverted_from INTEGER,"
                " idempotency_key TEXT,"
                " PRIMARY KEY (workspace, kind, doc_id, revision))"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_revisions_idempotency"
                " ON revisions(workspace, kind, doc_id, idempotency_key)"
                " WHERE idempotency_key IS NOT NULL"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS revision_audit ("
                " seq INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL,"
                " kind TEXT NOT NULL, doc_id TEXT NOT NULL, revision INTEGER NOT NULL,"
                " action TEXT NOT NULL, author TEXT NOT NULL, at TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)) as conn, conn:
            yield conn

    @staticmethod
    def _latest(conn: sqlite3.Connection, workspace: str, kind: str, doc_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(revision) FROM revisions WHERE workspace = ? AND kind = ? AND doc_id = ?",
            (workspace, kind, doc_id),
        ).fetchone()
        return int(row[0] or 0)

    def save(
        self,
        workspace: str,
        kind: str,
        doc_id: str,
        content: JsonValue,
        *,
        expected_revision: int,
        author: str,
        note: str | None = None,
        idempotency_key: str | None = None,
        reverted_from: int | None = None,
    ) -> Revision:
        """Add a revision on top of ``expected_revision``.

        Raises:
            RevisionConflict: The document is no longer at ``expected_revision``.
            CredentialInContent: The content carries a credential.
            ValueError: The content is too large.
        """
        paths = credential_paths(content)
        if paths:
            raise CredentialInContent(
                "credentials are never stored with a document; remove them from: "
                + ", ".join(paths)
            )
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True)
        if len(encoded.encode("utf-8")) > _MAX_CONTENT_BYTES:
            raise ValueError(f"content must be at most {_MAX_CONTENT_BYTES} bytes")
        with self._lock, self._connect() as conn:
            if idempotency_key is not None:
                row = conn.execute(
                    "SELECT revision FROM revisions WHERE workspace = ? AND kind = ?"
                    " AND doc_id = ? AND idempotency_key = ?",
                    (workspace, kind, doc_id, idempotency_key),
                ).fetchone()
                existing = (
                    self._get(conn, workspace, kind, doc_id, int(row[0]))
                    if row is not None
                    else None
                )
                if existing is not None:
                    return existing
            current = self._latest(conn, workspace, kind, doc_id)
            if current != expected_revision:
                raise RevisionConflict(current)
            revision = current + 1
            at = _now()
            conn.execute(
                "INSERT INTO revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace,
                    kind,
                    doc_id,
                    revision,
                    encoded,
                    note,
                    author,
                    at,
                    reverted_from,
                    idempotency_key,
                ),
            )
            conn.execute(
                "INSERT INTO revision_audit (workspace, kind, doc_id, revision, action, author, at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    workspace,
                    kind,
                    doc_id,
                    revision,
                    "revert" if reverted_from is not None else "save",
                    author,
                    at,
                ),
            )
        return Revision(kind, doc_id, revision, content, note, author, at, reverted_from)

    @staticmethod
    def _get(
        conn: sqlite3.Connection, workspace: str, kind: str, doc_id: str, revision: int
    ) -> Revision | None:
        row = conn.execute(
            "SELECT revision, content, note, author, created_at, reverted_from FROM revisions"
            " WHERE workspace = ? AND kind = ? AND doc_id = ? AND revision = ?",
            (workspace, kind, doc_id, revision),
        ).fetchone()
        if row is None:
            return None
        number, content, note, author, created_at, reverted_from = row
        return Revision(
            kind, doc_id, number, json.loads(content), note, author, created_at, reverted_from
        )

    def get(
        self, workspace: str, kind: str, doc_id: str, revision: int | None = None
    ) -> Revision | None:
        with self._connect() as conn:
            number = (
                revision if revision is not None else self._latest(conn, workspace, kind, doc_id)
            )
            return self._get(conn, workspace, kind, doc_id, number) if number else None

    def history(self, workspace: str, kind: str, doc_id: str) -> list[Revision]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision FROM revisions WHERE workspace = ? AND kind = ? AND doc_id = ?"
                " ORDER BY revision",
                (workspace, kind, doc_id),
            ).fetchall()
            revisions = [self._get(conn, workspace, kind, doc_id, int(r[0])) for r in rows]
        return [r for r in revisions if r is not None]

    def audit(self, workspace: str, kind: str, doc_id: str) -> list[dict[str, JsonValue]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT revision, action, author, at FROM revision_audit WHERE workspace = ?"
                " AND kind = ? AND doc_id = ? ORDER BY seq",
                (workspace, kind, doc_id),
            ).fetchall()
        return [{"revision": r[0], "action": r[1], "author": r[2], "at": r[3]} for r in rows]


__all__ = [
    "REVISION_KINDS",
    "CredentialInContent",
    "Revision",
    "RevisionConflict",
    "RevisionStore",
    "credential_paths",
]

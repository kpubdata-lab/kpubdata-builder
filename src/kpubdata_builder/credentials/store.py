"""Encrypted per-user Provider credential repository."""

from __future__ import annotations

import re
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from kpubdata_builder.sqlite_settings import BUSY_TIMEOUT_SECONDS

from .crypto import CredentialCipher
from .models import CredentialMetadata

_PROVIDER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MASK = "********"


def normalize_provider(provider: str) -> str:
    """Validate Provider path key as canonical lowercase."""
    normalized = provider.strip().lower()
    if not _PROVIDER_PATTERN.fullmatch(normalized):
        raise ValueError("provider must be a lowercase alphanumeric identifier")
    return normalized


def associated_data(owner_id: str, provider: str) -> bytes:
    """AES-GCM AAD. Bind credential to owner_id and provider.

    Must be **identical** regardless of backend (SQLite/CUBRID) for encrypt/decrypt compatibility —
    both stores share this single function.
    """
    return f"{owner_id}\0{provider}".encode()


def validate_owner_id(owner_id: str) -> None:
    """Stable owner_id required (empty values rejected, fail-closed)."""
    if not owner_id:
        raise ValueError("stable owner_id is required")


class CredentialRepository(Protocol):
    """Credential repository abstraction keyed by owner_id + provider."""

    def get_metadata(self, owner_id: str, provider: str) -> CredentialMetadata: ...

    def get_secret(self, owner_id: str, provider: str) -> str | None: ...

    def list_configured_providers(self, owner_id: str) -> Sequence[str]: ...

    def put(self, owner_id: str, provider: str, credential: str) -> CredentialMetadata: ...

    def delete(self, owner_id: str, provider: str) -> bool: ...


class SQLiteCredentialRepository:
    """Credential repository that records only ciphertext to SQLite."""

    def __init__(self, path: Path, cipher: CredentialCipher) -> None:
        self._path = path
        self._cipher = cipher
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_credentials (
                    owner_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    ciphertext BLOB NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (owner_id, provider)
                )
                """
            )

    def get_metadata(self, owner_id: str, provider: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT updated_at FROM provider_credentials WHERE owner_id = ? AND provider = ?",
                (owner_id, provider),
            ).fetchone()
        if row is None:
            return CredentialMetadata(provider, False, None, None)
        return CredentialMetadata(provider, True, _MASK, str(row[0]))

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT ciphertext FROM provider_credentials WHERE owner_id = ? AND provider = ?",
                (owner_id, provider),
            ).fetchone()
        if row is None:
            return None
        return self._cipher.decrypt(
            bytes(row[0]), associated_data=associated_data(owner_id, provider)
        )

    def list_configured_providers(self, owner_id: str) -> Sequence[str]:
        validate_owner_id(owner_id)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT provider FROM provider_credentials WHERE owner_id = ? ORDER BY provider",
                (owner_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def put(self, owner_id: str, provider: str, credential: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        if not credential or not credential.strip():
            raise ValueError("credential must be a non-empty string")
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        ciphertext = self._cipher.encrypt(
            credential, associated_data=associated_data(owner_id, provider)
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO provider_credentials(owner_id, provider, ciphertext, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_id, provider) DO UPDATE SET
                    ciphertext = excluded.ciphertext,
                    updated_at = excluded.updated_at
                """,
                (owner_id, provider, ciphertext, updated_at),
            )
        return CredentialMetadata(provider, True, _MASK, updated_at)

    def delete(self, owner_id: str, provider: str) -> bool:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM provider_credentials WHERE owner_id = ? AND provider = ?",
                (owner_id, provider),
            )
        return cursor.rowcount > 0


__all__ = [
    "CredentialRepository",
    "SQLiteCredentialRepository",
    "associated_data",
    "normalize_provider",
    "validate_owner_id",
]

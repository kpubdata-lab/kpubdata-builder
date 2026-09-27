"""CUBRID-based encrypted Provider credential repository (ADR 0016).

Implements same ``CredentialRepository`` Protocol as ``SQLiteCredentialRepository`` using SQLAlchemy
Core. Stores **ciphertext only**; AES-GCM AAD (``associated_data``) and owner validation
(``validate_owner_id``) share single function from store.py — encrypt/decrypt semantics identical
regardless of backend.

This module imported only in cubrid branch of ``_credential_repository_from_env`` —
imports ``sqlalchemy``, so doesn't pull optional dependency into default (sqlite) path.

Concurrency: receives single process-wide Engine (connection pool + pool_pre_ping),
borrows short connection per operation. put does upsert as delete+insert within single transaction,
dialect-independent. credential write is user action (unlike derived index),
    doesn't swallow exceptions.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Text,
    delete,
    insert,
    select,
)

from .crypto import CredentialCipher
from .models import CredentialMetadata
from .store import _MASK, associated_data, normalize_provider, validate_owner_id

if TYPE_CHECKING:
    from sqlalchemy import Engine


class CubridCredentialRepository:
    """Credential repository that records only ciphertext to CUBRID (ADR 0016)."""

    def __init__(self, engine: Engine, cipher: CredentialCipher) -> None:
        self._engine = engine
        self._cipher = cipher
        self._metadata = MetaData()
        self._table = Table(
            "provider_credentials",
            self._metadata,
            Column("owner_id", String(255), primary_key=True),
            Column("provider", String(64), primary_key=True),
            # Ciphertext stored as base64 text (CLOB). CUBRID doesn't allow NOT NULL on BLOBs
            # (errno -1014); pycubrid BLOB round-trip corrupts. Use base64 string for
            # deterministic round-trip + NOT NULL. SQLite uses raw BLOB; backend storage
            # representation can differ behind Protocol.
            Column("ciphertext", Text, nullable=False),
            Column("updated_at", String(40), nullable=False),
        )
        self._table.create(self._engine, checkfirst=True)

    def get_metadata(self, owner_id: str, provider: str) -> CredentialMetadata:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = select(self._table.c.updated_at).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return CredentialMetadata(provider, False, None, None)
        return CredentialMetadata(provider, True, _MASK, str(row[0]))

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = select(self._table.c.ciphertext).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is None:
            return None
        ciphertext = base64.b64decode(row[0])
        return self._cipher.decrypt(ciphertext, associated_data=associated_data(owner_id, provider))

    def list_configured_providers(self, owner_id: str) -> Sequence[str]:
        validate_owner_id(owner_id)
        stmt = (
            select(self._table.c.provider)
            .where(self._table.c.owner_id == owner_id)
            .order_by(self._table.c.provider)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
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
        ciphertext_b64 = base64.b64encode(ciphertext).decode("ascii")
        # delete+insert within single transaction — does not rely on dialect upsert.
        with self._engine.begin() as conn:
            conn.execute(
                delete(self._table).where(
                    self._table.c.owner_id == owner_id, self._table.c.provider == provider
                )
            )
            conn.execute(
                insert(self._table).values(
                    owner_id=owner_id,
                    provider=provider,
                    ciphertext=ciphertext_b64,
                    updated_at=updated_at,
                )
            )
        return CredentialMetadata(provider, True, _MASK, updated_at)

    def delete(self, owner_id: str, provider: str) -> bool:
        validate_owner_id(owner_id)
        provider = normalize_provider(provider)
        stmt = delete(self._table).where(
            self._table.c.owner_id == owner_id, self._table.c.provider == provider
        )
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
        return bool(result.rowcount and result.rowcount > 0)


__all__ = ["CubridCredentialRepository"]

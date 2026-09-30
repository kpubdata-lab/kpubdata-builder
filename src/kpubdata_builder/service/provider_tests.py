"""The last connection test per principal and provider (#842).

A connection test's result was returned and forgotten, so a Connections screen opened
later could not say when a provider was last checked or how that went. This stores the
last result of each (owner, provider): status, time, error category and response code —
never the key, which the test does not see here and which is not part of the result.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from kpubdata_builder.service.providers import ProviderTestResult
from kpubdata_builder.spec import JsonValue


class ProviderTestLog:
    """SQLite store of the last test result, one row per (owner, provider)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS provider_tests ("
                " owner_id TEXT NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL,"
                " checked_at TEXT NOT NULL, error_category TEXT, response_code INTEGER,"
                " dataset TEXT, PRIMARY KEY (owner_id, provider))"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=30)) as conn, conn:
            yield conn

    def record(self, owner_id: str, result: ProviderTestResult) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO provider_tests VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    owner_id,
                    result.provider,
                    result.status,
                    result.checked_at,
                    result.error_category,
                    result.response_code,
                    result.dataset,
                ),
            )

    def last_tests(self, owner_id: str) -> dict[str, dict[str, JsonValue]]:
        """Each provider's last result for ``owner_id``; a provider never tested is absent."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT provider, status, checked_at, error_category, response_code, dataset"
                " FROM provider_tests WHERE owner_id = ?",
                (owner_id,),
            ).fetchall()
        return {
            provider: {
                "status": status,
                "checked_at": checked_at,
                "error_category": error_category,
                "response_code": response_code,
                "dataset": dataset,
            }
            for provider, status, checked_at, error_category, response_code, dataset in rows
        }


__all__ = ["ProviderTestLog"]

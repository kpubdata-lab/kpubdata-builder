"""CubridArtifactStore — store with CUBRID as canonical for manifest documents (ADR 0016).

Delegate artifact bytes and run workspace to ``LocalArtifactStore`` (FS/block volume).
Only manifest documents use CUBRID ``manifests`` row as canonical, FS as mirror (cache) —
FS mirror always maintained, so bulk scan (datasets/list_builds) still works from FS,
single-run query (``get_manifest``) prioritizes CUBRID canonical.

This module imported only in cubrid branch of make_artifact_store() — sqlalchemy
import only happens here, not pulling optional deps into default (sqlite/local) path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Column, MetaData, String, Table, Text, delete, insert, select

from .local import LocalArtifactStore

if TYPE_CHECKING:
    from sqlalchemy import Engine


class CubridArtifactStore:
    """ArtifactStore keeping manifest canonical in CUBRID (bytes delegated to FS)."""

    def __init__(self, output_root: Path, engine: Engine) -> None:
        self._local = LocalArtifactStore(output_root)
        self._engine = engine
        self._metadata = MetaData()
        self._table = Table(
            "manifests",
            self._metadata,
            Column("run_id", String(255), primary_key=True),
            # Canonical manifest JSON document. Store as CLOB (Text, not large bytes).
            Column("manifest", Text, nullable=False),
            Column("updated_at", String(40), nullable=False),
        )
        self._table.create(self._engine, checkfirst=True)

    def run_dir(self, run_id: str) -> Path:
        # Bytes kept on FS (block volume) — query engine requires actual path.
        return self._local.run_dir(run_id)

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        # CUBRID canonical first, fallback to FS mirror if absent.
        stmt = select(self._table.c.manifest).where(self._table.c.run_id == run_id)
        with self._engine.connect() as conn:
            row = conn.execute(stmt).first()
        if row is not None:
            try:
                data = json.loads(row[0])
            except (ValueError, TypeError):
                data = None
            if isinstance(data, dict):
                return data
        return self._local.get_manifest(run_id)

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        payload = json.dumps(manifest, ensure_ascii=False, sort_keys=True)
        updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # delete+insert within single transaction — doesn't depend on dialect upsert.
        with self._engine.begin() as conn:
            conn.execute(delete(self._table).where(self._table.c.run_id == run_id))
            conn.execute(
                insert(self._table).values(run_id=run_id, manifest=payload, updated_at=updated_at)
            )
        # Maintain FS mirror (bulk scan·byte colocate·backup). Mirror only
        # after CUBRID record succeeds.
        self._local.put_manifest(run_id, manifest)

    def list_run_ids(self) -> list[str]:
        # Union of CUBRID canonical + FS mirror (includes runs only on FS due to promotion failure).
        with self._engine.connect() as conn:
            rows = conn.execute(select(self._table.c.run_id)).all()
        ids = {str(r[0]) for r in rows}
        ids.update(self._local.list_run_ids())
        return sorted(ids)


__all__ = ["CubridArtifactStore"]

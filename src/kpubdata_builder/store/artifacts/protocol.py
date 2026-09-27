"""ArtifactStore Protocol (ADR 0010/0016)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class ArtifactStore(Protocol):
    """산출물 워크스페이스 + manifest 문서 저장소 인터페이스."""

    def run_dir(self, run_id: str) -> Path:
        """run 산출물 워크스페이스 경로(FS). 바이트 읽기/쓰기·서빙·쿼리에 쓰인다."""
        ...

    def get_manifest(self, run_id: str) -> dict[str, object] | None:
        """manifest 문서를 반환한다(없거나 손상 시 None). CUBRID 백엔드는 정본 행 우선."""
        ...

    def put_manifest(self, run_id: str, manifest: dict[str, object]) -> None:
        """manifest 문서를 authoritative store 에 기록한다(CUBRID 행 + FS 미러)."""
        ...

    def list_run_ids(self) -> list[str]:
        """manifest 를 가진 run_id 목록."""
        ...


__all__ = ["ArtifactStore"]

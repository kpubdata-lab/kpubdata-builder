"""빌드 매니페스트 기록기 (Medallion 재구성: 기존 manifest.py에서 분리).

이 모듈은 BuildManifest를 결정적 JSON으로 디스크에 기록한다. UTC 기준 ISO
문자열과 정렬된 키를 사용해 직렬화 결과를 안정적으로 유지한다.

주요 함수:
    - manifest_writer / write_manifest: 디스크 기록 함수
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import timezone
from pathlib import Path

from ..errors import ManifestError
from .models import BuildManifest


def manifest_writer(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 결정적 JSON으로 디스크에 기록한다.

    매개변수:
        manifest: 기록할 매니페스트 데이터.
        output_path: 결과 JSON 파일 경로.

    예외:
        ManifestError: 디렉터리 생성 또는 파일 기록에 실패한 경우.
    """
    payload = {
        "schema_version": manifest.schema_version,
        "build_id": manifest.build_id,
        # additive (#481): run의 종단 상태와 부분 산출물 여부. legacy manifest에는
        # 이 키가 없고, reader는 부재 시 기존대로 errors 유무에서 status를 파생한다
        # (manifest.status_from_manifest가 그 규칙의 정본). 취소된 run이
        # 성공/실패와 구분되고, BuildIndex 재구축(store.rebuild_index)이
        # manifest만으로 cancelled를 복원할 수 있게 하는 것이 목적이다.
        "status": manifest.status,
        "partial": manifest.partial,
        "started_at": manifest.started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": manifest.finished_at.astimezone(timezone.utc).isoformat(),
        "build_environment": (
            asdict(manifest.build_environment) if manifest.build_environment is not None else None
        ),
        "inputs": list(manifest.inputs),
        "inputs_fingerprint": manifest.inputs_fingerprint,
        "created_by": manifest.created_by,
        # canonical stable owner identity (#505, additive) — created_by는 legacy
        # display 라벨로 계속 함께 기록된다. legacy 소비자는 이 키를 몰라도 무해하다.
        "owner_id": manifest.owner_id,
        "outputs": list(manifest.outputs),
        "warnings": list(manifest.warnings),
        "errors": list(manifest.errors),
        "row_counts": manifest.row_counts,
        "schema_summaries": {
            key: asdict(summary) for key, summary in manifest.schema_summaries.items()
        },
        "provenance": [asdict(entry) for entry in manifest.provenance],
        # additive (#486): source_key별 구조화된 quality/drift 결과. 빈 dict가
        # 기본값이므로 legacy 소비자가 이 키를 몰라도 무해하다.
        "quality_results": {
            key: [asdict(r) for r in results] for key, results in manifest.quality_results.items()
        },
        "schema_drift": {
            key: [asdict(f) for f in findings] for key, findings in manifest.schema_drift.items()
        },
        # additive (#700): whether each axis was evaluated. This key is what tells an
        # empty schema_drift list apart from "there was nothing to compare against" —
        # they are not the same answer.
        "drift_evaluation": {
            key: [asdict(e) for e in entries] for key, entries in manifest.drift_evaluation.items()
        },
        # additive (#506): composition(join) 실행 결과의 출처. composition 미사용
        # run은 null이다 — legacy 소비자가 이 키를 몰라도 무해하다.
        "composition": asdict(manifest.composition) if manifest.composition is not None else None,
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # 같은 디렉터리에 임시 파일로 쓴 뒤 os.replace로 원자적 교체한다. truncate-then-write는
        # 크래시/동시 접근 시 부분·손상된 매니페스트를 남길 수 있다 (#204).
        fd, tmp_name = tempfile.mkstemp(dir=output_path.parent, prefix=".manifest_", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                _ = handle.write(f"{serialized}\n")
            os.replace(tmp_name, output_path)
        except BaseException:
            # 교체 전 실패 시 임시 파일을 정리한다(이미 교체됐다면 무시).
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError as exc:
        raise ManifestError(f"Failed to write manifest to {output_path}: {exc}") from exc


def write_manifest(manifest: BuildManifest, output_path: Path) -> None:
    """빌드 매니페스트를 기록하는 공개 별칭 함수다.

    매개변수:
        manifest: 기록할 매니페스트 객체.
        output_path: 출력 경로.
    """
    manifest_writer(manifest, output_path)


__all__ = ["manifest_writer", "write_manifest"]

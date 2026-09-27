"""스키마·통계 드리프트 감지 (#445, DRIFT-1; dataset/source 범위 한정은 #486).

스케줄 워크플로가 주기적으로 재빌드할 때 소스 API가 조용히 형식을 바꿔도
감지된다. append-only 데이터셋에서는 오염이 누적되므로 직전 성공 run과
비교한다.

주요 구성:
    - DriftFinding: 단일 드리프트 관찰 (컬럼 추가/삭제/dtype 변경/행 수 급변)
    - detect_drift: 순수 함수 — 현재/이전 SchemaInfo+TableStatistics → findings
    - find_previous_silver: 동일 dataset_id·source_key의 직전 "성공" run에서 silver
      데이터를 찾는다 (#486) — 다른 dataset/source의 silver와 비교해 가짜 drift를
      만들지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml

from ...manifest import status_from_manifest
from ...spec.serializer import BUILDSPEC_SNAPSHOT_FILENAME
from ...tabular import SchemaInfo, TableStatistics
from ...tabular.types import ColumnInfo

# Reason strings, kept identical to warehouse.baseline.NotEvaluatedReason values.
# They are literals rather than an import so this stage does not depend on the
# warehouse package, while a report still sees one vocabulary. The catalog tests
# assert the two stay in step.
NO_COMMITTED_SNAPSHOT = "no_committed_snapshot"
OWNER_UNKNOWN = "owner_unknown"

# 행 수 급변 임계 (직전 대비 50% 이상 변화).
_ROW_COUNT_CHANGE_THRESHOLD = 0.5


@dataclass(frozen=True)
class DriftFinding:
    """단일 드리프트 관찰.

    속성:
        kind: column_added | column_removed | dtype_changed | row_count_jump.
        column: 관련 컬럼명. 테이블 전체 문제면 None.
        detail: 사람이 읽는 설명.
    """

    kind: str
    column: str | None
    detail: str


def detect_drift(
    current_schema: SchemaInfo,
    current_stats: TableStatistics,
    previous_schema: SchemaInfo,
    previous_stats: TableStatistics,
) -> list[DriftFinding]:
    """현재/이전 스키마·통계를 비교해 드리프트를 감지한다 (#445).

    순수 함수 — 파일 I/O 없이 데이터만 받아 비교한다.
    """
    findings: list[DriftFinding] = []
    current_cols = {c.name: c for c in current_schema.columns}
    previous_cols = {c.name: c for c in previous_schema.columns}

    # 컬럼 추가.
    for name in sorted(current_cols.keys() - previous_cols.keys()):
        findings.append(DriftFinding(kind="column_added", column=name, detail="new column"))
    # 컬럼 삭제.
    for name in sorted(previous_cols.keys() - current_cols.keys()):
        findings.append(DriftFinding(kind="column_removed", column=name, detail="column gone"))
    # dtype 변경.
    for name in sorted(current_cols.keys() & previous_cols.keys()):
        if current_cols[name].dtype != previous_cols[name].dtype:
            findings.append(
                DriftFinding(
                    kind="dtype_changed",
                    column=name,
                    detail=f"{previous_cols[name].dtype} → {current_cols[name].dtype}",
                )
            )

    # 행 수 급변.
    if previous_stats.row_count > 0:
        change = abs(current_stats.row_count - previous_stats.row_count) / previous_stats.row_count
        if change > _ROW_COUNT_CHANGE_THRESHOLD:
            findings.append(
                DriftFinding(
                    kind="row_count_jump",
                    column=None,
                    detail=f"{previous_stats.row_count} → {current_stats.row_count} ({change:.0%})",
                )
            )

    return findings


def _run_dataset_id(run_dir: Path) -> str | None:
    """run_dir의 buildspec.yaml snapshot에서 dataset_id만 가볍게 읽는다.

    service/datasets.py의 read_snapshot_dataset_id와 동일한 의도지만, silver
    stage는 service 계층에 의존할 수 없으므로(레이어링) 여기서 최소한으로
    다시 구현한다. snapshot이 없거나 읽을 수 없으면 None — 추측하지 않는다.
    """
    snapshot_path = run_dir / BUILDSPEC_SNAPSHOT_FILENAME
    if not snapshot_path.is_file():
        return None
    try:
        doc = yaml.safe_load(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(doc, dict):
        return None
    dataset_id = doc.get("dataset_id")
    return dataset_id if isinstance(dataset_id, str) and dataset_id else None


def _run_succeeded(run_dir: Path) -> tuple[bool, str, str | None]:
    """(성공 여부, finished_at 정렬키, owner_id)를 manifest.json에서 읽는다.

    manifest가 없거나 손상되었거나 성공으로 끝나지 않았으면 실패로 취급한다 —
    실패/부분 실패 run의 silver는 drift 비교 대상에서 제외한다(#486).

    상태 판정은 manifest 패키지의 정본 규칙에 맡긴다. errors 유무만 보던 시절에는
    **취소된 run이 기준 run이 됐다** — 취소는 errors를 남기지 않기 때문이다.
    중간에 끊긴 partial silver를 기준으로 삼으면 실제로는 없는 drift가 보고된다.
    """
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False, "", None
    if not isinstance(manifest, dict):
        return False, "", None
    if status_from_manifest(cast("dict[str, object]", manifest)) != "ok":
        return False, "", None
    finished_at = manifest.get("finished_at")
    owner_id = manifest.get("owner_id")
    return (
        True,
        finished_at if isinstance(finished_at, str) else "",
        owner_id if isinstance(owner_id, str) and owner_id else None,
    )


@dataclass(frozen=True)
class SilverBaseline:
    """A comparable previous silver was found.

    Attributes:
        schema: The previous run's SchemaInfo.
        stats: The previous run's TableStatistics.
        run_id: Which run is the baseline, so a report can show its evidence.
    """

    schema: SchemaInfo
    stats: TableStatistics
    run_id: str


@dataclass(frozen=True)
class NoSilverBaseline:
    """There is nothing comparable to compare against. **Not "no drift"** (#700).

    While this returned ``None``, the caller wrapped the comparison in ``if prev is
    not None:`` and otherwise left the finding list empty. An empty list serialises
    as an absent manifest key, which makes **"there was no baseline" and "compared,
    nothing changed" the same answer on the wire.** A consumer then shows a table as
    healthy that was never checked.

    Attributes:
        reason: The same vocabulary as ``warehouse.baseline.NotEvaluatedReason``.
        detail: Human-readable context, safe to show in a report.
    """

    reason: str
    detail: str


SilverBaselineOutcome = SilverBaseline | NoSilverBaseline
"""Either a baseline or a stated reason there is none. No third case, and in
particular no ``None``."""


def find_previous_silver(
    output_root: Path,
    current_run_id: str,
    *,
    dataset_id: str,
    source_key: str,
    owner_id: str | None = None,
) -> SilverBaselineOutcome:
    """Find the previous successful silver for this dataset, source and owner.

    Not "the previous run, whatever it was" (#486). Among runs meeting all of the
    following, the most recent by ``finished_at`` wins:

    - the buildspec snapshot's ``dataset_id`` matches exactly
    - the manifest status is ``ok``
    - that source key has both ``silver/schema.json`` and ``silver/stats.json``
    - **the manifest's ``owner_id`` matches ``owner_id``** (#700)

    The owner condition is the new one. Without it **another user's run could become
    the baseline.** No raw data leaks, but row count, schema and distribution
    changes are a metadata side channel — and that is exactly what drift reports.

    Passing ``owner_id=None`` skips the owner filter, which keeps single-user
    deployments and runs recorded before owners existed behaving as they did. When
    an owner *is* given, **a run that recorded no owner drops out**: it is not
    assumed to be ours.

    Returns:
        ``SilverBaseline`` or ``NoSilverBaseline``. **Never ``None``** — an optional
        return is what let a caller treat "nothing to compare" as "nothing wrong".
    """
    if not output_root.exists():
        return NoSilverBaseline(
            NO_COMMITTED_SNAPSHOT,
            f"no previous run exists under {output_root}",
        )
    source_segment = source_key.replace("/", "_")
    candidates: list[tuple[str, str, Path]] = []
    rejected_for_owner = 0
    unowned_runs = 0
    for run_dir in output_root.iterdir():
        if not run_dir.is_dir() or run_dir.name == current_run_id:
            continue
        # 파일 존재 확인(stat)이 snapshot/manifest 읽기보다 저렴하므로 먼저
        # 걸러서 run 수 × source 수만큼 반복되는 I/O를 줄인다.
        silver_dir = run_dir / "silver" / source_segment
        if not (silver_dir / "schema.json").is_file() or not (silver_dir / "stats.json").is_file():
            continue
        if _run_dataset_id(run_dir) != dataset_id:
            continue
        succeeded, sort_key, run_owner = _run_succeeded(run_dir)
        if not succeeded:
            continue
        if owner_id is not None and run_owner != owner_id:
            # Counted separately so the reason can say which it was: someone else's
            # run, or a run that never recorded an owner. The second is a recording
            # gap worth fixing; the first is the isolation working.
            rejected_for_owner += 1
            if run_owner is None:
                unowned_runs += 1
            continue
        candidates.append((sort_key, run_dir.name, silver_dir))
    if not candidates:
        if rejected_for_owner:
            return NoSilverBaseline(
                OWNER_UNKNOWN if unowned_runs else NO_COMMITTED_SNAPSHOT,
                f"{rejected_for_owner} previous run(s) of {dataset_id}/{source_key} exist "
                f"but none belong to {owner_id}"
                + (f"; {unowned_runs} recorded no owner" if unowned_runs else ""),
            )
        return NoSilverBaseline(
            NO_COMMITTED_SNAPSHOT,
            f"no successful previous run of {dataset_id}/{source_key} to compare against",
        )

    # finished_at 내림차순, 동일 시각은 run_id 내림차순으로 결정적 타이브레이크
    # (#488 sort_key semantics와 동일한 원칙).
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, baseline_run_id, prev_silver_dir = candidates[0]
    try:
        schema_data = json.loads((prev_silver_dir / "schema.json").read_text(encoding="utf-8"))
        stats_data = json.loads((prev_silver_dir / "stats.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return NoSilverBaseline(
            NO_COMMITTED_SNAPSHOT,
            f"the silver output of run {baseline_run_id} could not be read",
        )

    columns = tuple(
        ColumnInfo(
            name=c.get("name", ""),
            dtype=c.get("dtype", ""),
            nullable=c.get("nullable", True),
            unique_count=c.get("unique_count", 0),
        )
        for c in schema_data.get("columns", [])
    )
    schema = SchemaInfo(columns=columns)
    stats = TableStatistics(
        row_count=stats_data.get("row_count", 0),
        null_counts=stats_data.get("null_counts", {}),
        duplicate_rate=stats_data.get("duplicate_rate", 0.0),
    )
    return SilverBaseline(schema=schema, stats=stats, run_id=baseline_run_id)


__all__ = [
    "NO_COMMITTED_SNAPSHOT",
    "OWNER_UNKNOWN",
    "DriftFinding",
    "NoSilverBaseline",
    "SilverBaseline",
    "SilverBaselineOutcome",
    "detect_drift",
    "find_previous_silver",
]

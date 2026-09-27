"""Medallion 파이프라인 오케스트레이터 (#48).

BuildSpec의 각 소스를 Bronze → Silver → Gold 순서로 실행하고, 각 단계 산출물을
실행 워크스페이스에 저장한 뒤 빌드 매니페스트를 기록한다.

부분 성공 정책(BUILD_STATE.md): 소스 중 하나라도 실패하면 전체 상태는 failed로
기록하되, 성공한 소스의 산출물과 실패 정보를 매니페스트에 함께 남긴다.

BuildSpec.composition이 있으면(#506) 모든 source가 Bronze/Silver/Gold를 마친 뒤,
composition이 참조하는 두 source의 검증된 Silver를 join해 별도 결합 Gold
dataset(gold/{composition.name}/)을 추가로 만든다. 기존 source별 독립 Gold는
그대로 유지된다 — composition은 부가 산출물이지 대체가 아니다.

주요 구성:
    - SourceBuildOutcome: 소스별 실행 결과
    - CompositionOutcome: composition(join) 실행 결과 (#506)
    - BuildResult: 전체 실행 결과
    - run_build: 파이프라인 진입점
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import DatasetValidationError, ValidationError
from ..events import BuildEventRecorder, BuildEventStore
from ..exporters import get_exporter
from ..ingestion import IngestionError
from ..manifest import (
    BuildManifest,
    CompositionProvenance,
    SchemaSummary,
    SourceProvenance,
    build_schema_summary,
    build_source_provenance,
    capture_build_environment,
    compute_inputs_fingerprint,
    manifest_writer,
)
from ..quality import (
    DriftEvaluation,
    QualityCheckResult,
    SchemaDriftFinding,
    evaluate_quality,
)
from ..spec import BuildSpec, CompositionSpec, ExportTarget, SourceRef, write_buildspec_snapshot
from ..spec.validator import validate_spec
from ..stages.bronze.build import SourceClient
from ..stages.bronze.models import BronzeArtifact, utc_now
from ..stages.bronze.persist import persist_bronze_artifact
from ..stages.bronze.resolve import build_bronze_artifact_for_source, source_identity
from ..stages.gold.build import build_gold_package
from ..stages.gold.card import build_dataset_card, render_dataset_card
from ..stages.gold.compose import CompositionError, build_composed_gold_package
from ..stages.gold.persist import persist_gold_package
from ..stages.silver.build import build_silver_dataset
from ..stages.silver.drift import (
    DriftFinding,
    SilverBaseline,
    detect_drift,
    find_previous_silver,
)
from ..stages.silver.models import SilverDataset
from ..stages.silver.persist import persist_silver_dataset
from ..stages.silver.pii import scan_pii
from ..stages.silver.summarize import build_schema
from ..tabular import DEFAULT_PREVIEW_LIMIT
from ..uploads import UploadRepository
from .cancellation import BuildCancelled, CancellationProbe, raise_if_cancelled
from .context import BuildContext
from .export import export_gold_package

logger = logging.getLogger(__name__)

# 소스를 동시에 실행할 최대 스레드 수. 소스별 fetch/stage는 대부분 네트워크 I/O로
# 대기하므로 순차 실행 시 총 소요 시간이 소스 수에 비례해 늘어난다. 소스 수만큼
# 무제한으로 스레드를 만들지 않도록 상한을 둔다 (#247).
_MAX_PARALLEL_SOURCES = 4


def _dataset_card_license(spec: BuildSpec) -> str:
    """canonical license를 우선하고 문자열인 legacy metadata 값을 보조로 사용한다."""
    if spec.license is not None:
        return spec.license
    legacy_license = spec.metadata.get("license")
    return legacy_license if isinstance(legacy_license, str) else ""


def _gold_package_metadata(spec: BuildSpec) -> dict[str, str]:
    """Gold 패키지에 실을 metadata — exporter가 보는 유일한 출처다 (#629).

    exporter는 ``ArtifactDataset.metadata`` 하나만 본다. 그 하나를 만드는 곳이
    두 군데였기 때문에 둘이 갈렸고, 뒤가 앞을 덮었다. 이제 여기가 유일한
    출처다.

    ``license``는 **선언됐을 때만** 키를 싣는다. 빈 문자열을 실으면 Kaggle
    exporter의 기본값(CC-BY-4.0) 대신 빈 라이선스가 게시된다 — 선언하지 않은
    것과 빈 값으로 선언한 것은 다르다.
    """
    metadata = {
        "title": spec.title,
        "description": spec.description,
        # Kaggle exporter가 dataset-metadata.json의 id를 여기서 읽는다
        # (#550 정합화 — spec.dataset_id가 곧 게시 destination 식별자).
        "dataset_id": spec.dataset_id,
    }
    if spec.attribution:
        # 공공누리는 제1~4유형 모두 출처표시를 의무로 둔다. license 식별자만으로는
        # 그 의무를 채울 수 없어서(기관명·유형·원문 URL 이 함께 있어야 한다)
        # 별도 필드로 받아 카드까지 그대로 내려보낸다 (ADR 0018).
        metadata["attribution"] = spec.attribution
    declared_license = _dataset_card_license(spec)
    if declared_license:
        # 이것이 없으면 spec.license를 무엇으로 선언하든 Kaggle
        # dataset-metadata.json은 항상 CC-BY-4.0이었다 — 같은 빌드가 dataset
        # card와 Kaggle metadata에 서로 다른 라이선스를 적었다.
        metadata["license"] = declared_license
    return metadata


def _dataset_card_version(spec: BuildSpec) -> str:
    """metadata.version이 문자열일 때만 사용한다.

    metadata가 ``_parse_json_mapping``으로 임의 JSON 값을 허용하면서, null/숫자/list/dict
    값을 그대로 ``str()``에 넘기면 ``"None"``·``"{...}"`` 같은 문자열이 dataset card에
    그대로 노출된다. license와 동일하게 문자열이 아니면 빈 값으로 취급해
    ``card.version or "unversioned"`` fallback이 정상 동작하게 한다.
    """
    version = spec.metadata.get("version")
    return version if isinstance(version, str) else ""


@dataclass(frozen=True)
class SourceBuildOutcome:
    """단일 소스에 대한 파이프라인 실행 결과.

    속성:
        source_key: 소스 식별자.
        status: "ok", "failed", 또는 "cancelled"(#481 — 안전한 stage 경계에서
            협력적 취소가 관찰되어 다음 단계를 시작하지 않은 소스). "cancelled"는
            실패가 아니므로 ``error``를 채우지 않으며, cancellation probe를 받는
            async run에서만 만들어진다(동기 ``POST /build``/CLI는 probe가 없어
            기존과 동일하게 ok/failed만 나온다).
        stages_completed: 성공적으로 끝난 단계 이름 순서 (bronze/silver/gold).
            취소된 소스도 취소 시점까지 실제로 완료한 단계만 담는다 — 실행되지
            않은 단계를 성공으로 가장하지 않는다.
        error: 실패 시 오류 메시지.
    """

    source_key: str
    status: str
    stages_completed: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class CompositionOutcome:
    """composition(join) 실행 결과 (#506).

    SourceBuildOutcome과 별도 타입으로 둔다 — composition에는 bronze/silver/gold
    stage 개념이 적용되지 않고(두 source의 Silver를 결합할 뿐), manifest에서도
    "combined result"로 명확히 구분되어야 하기 때문이다.

    속성:
        name: 결합 Gold dataset 이름 (CompositionSpec.name).
        status: "ok" | "failed"(join 자체 실행 실패) | "skipped"(참조한 source가
            실패해 join을 시도조차 못함).
        error: 실패/스킵 사유.
    """

    name: str
    status: str
    error: str | None = None


@dataclass(frozen=True)
class BuildResult:
    """전체 빌드 실행 결과.

    속성:
        context: 실행 컨텍스트.
        status: 전체 상태 ("ok", "failed", 또는 "cancelled"). "cancelled"는
            cancellation probe(#481)를 넘긴 호출자에서만 나온다 — 동기
            ``POST /build``/CLI 경로는 기존대로 ok/failed만 반환한다.
        outcomes: 소스별 실행 결과.
        manifest_path: 기록된 빌드 매니페스트 경로.
        composition_outcome: composition 실행 결과. BuildSpec.composition이
            없으면 None(#506).
    """

    context: BuildContext
    status: str
    outcomes: tuple[SourceBuildOutcome, ...]
    manifest_path: Path
    spec_digest: str
    composition_outcome: CompositionOutcome | None = None


def _fetch_source_key(source: SourceRef) -> str:
    """Bronze fetch identity 키를 반환한다 (kind별 canonical identity, #498)."""
    provider, dataset = source_identity(source)
    return f"{provider}.{dataset}"


def _output_source_key(source: SourceRef) -> str:
    """워크스페이스/결과 기록에 사용할 사용자 노출 키를 반환한다."""
    return source.alias if source.alias else _fetch_source_key(source)


def _retag_bronze_artifact(artifact: BronzeArtifact, *, output_key: str) -> BronzeArtifact:
    """fetch provenance는 유지하고 산출물 경로용 source_key만 교체한다."""
    return BronzeArtifact(
        source_key=output_key,
        raw_records=artifact.raw_records,
        fetch_params=artifact.fetch_params,
        fetched_at=artifact.fetched_at,
        provenance=artifact.provenance,
    )


def _record_output_paths(outputs: list[str], *paths: Path) -> None:
    """생성된 산출물 경로를 manifest outputs에 모두 기록한다."""
    outputs.extend(str(path) for path in paths)


def _quality_failure_messages(fail_results: Sequence[QualityCheckResult]) -> list[str]:
    """FAIL로 판정된 QualityCheckResult를 DatasetValidationError 메시지로 변환한다 (#486)."""
    messages: list[str] = []
    for r in fail_results:
        location = f" @ {r.column}" if r.column else ""
        detail = f", detail={r.detail!r}" if r.detail else ""
        messages.append(
            f"quality check failed: {r.rule}{location} (actual={r.actual!r}, "
            f"threshold={r.threshold!r}{detail})"
        )
    return messages


def _to_schema_drift_findings(findings: Sequence[DriftFinding]) -> tuple[SchemaDriftFinding, ...]:
    """deterministic DriftFinding을 API/manifest용 SchemaDriftFinding으로 변환한다 (#486)."""
    return tuple(
        SchemaDriftFinding(kind=f.kind, column=f.column, detail=f.detail) for f in findings
    )


def _execute_exports(
    gold_dir: Path,
    artifact: ArtifactDataset,
    exports: tuple[ExportTarget, ...],
) -> list[Path]:
    """내보내기 도구를 실행하고 생성된 파일 경로를 반환한다.

    매개변수:
        gold_dir: Gold 패키지 디렉터리.
        artifact: 내보내기 도구가 소비할 조립 산출물.
        exports: 내보내기 대상 목록.

    반환값:
        생성된 파일 경로 목록.

    """
    output_paths: list[Path] = []
    for export_target in exports:
        exporter = get_exporter(export_target.kind)
        result = exporter.export(artifact, export_target, gold_dir)
        output_paths.append(result.output_path)
        logger.info(
            "exported %s to %s (size: %d bytes)",
            export_target.kind,
            result.output_path,
            result.file_size,
        )
    return output_paths


@dataclass(frozen=True)
class _SourcePipelineResult:
    """단일 소스 파이프라인 실행의 로컬 결과.

    여러 소스를 스레드 풀로 동시에 실행할 때(#247), _run_source_pipeline이 공유
    가변 상태(outputs/row_counts/schema_summaries/provenance)를 직접 건드리지
    않고 자신의 결과만 반환하게 한다. 병합은 run_build에서 모든 스레드가 끝난
    뒤 단일 스레드로 수행한다.
    """

    outcome: SourceBuildOutcome
    output_paths: tuple[str, ...] = ()
    row_count: int | None = None
    schema_summary: SchemaSummary | None = None
    provenance_entry: SourceProvenance | None = None
    quality_results: tuple[QualityCheckResult, ...] = ()
    quality_evaluated: bool = False
    schema_drift: tuple[SchemaDriftFinding, ...] = ()
    # Whether drift was compared at all, separate from what it found (#700). An
    # empty schema_drift alone cannot tell "nothing changed" from "no baseline".
    drift_evaluation: tuple[DriftEvaluation, ...] = ()
    silver: SilverDataset | None = None


def _run_source_pipeline(
    source: SourceRef,
    *,
    client: SourceClient,
    context: BuildContext,
    recorder: BuildEventRecorder,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
    baseline_owner_id: str | None = None,
    capture_silver: bool = False,
    cancellation: CancellationProbe | None = None,
) -> _SourcePipelineResult:
    """한 소스를 Bronze → Silver → Gold로 실행하고 산출물을 저장한다.

    공유 가변 컨테이너를 인자로 받는 대신 결과를 로컬로 모아 반환하므로,
    여러 소스에 대해 동시에(스레드 풀에서) 안전하게 호출할 수 있다 (#247).

    ``upload_repository``/``owner_id`` 는 ``kind="file"`` source에서만 쓰인다
    (#498) — 업로드 소유권 확인에 필요한 stable principal id다.

    ``recorder``는 이 소스의 실제 실행 boundary(fetch/stage/quality)에서만
    structured event를 남긴다(#496) — 실행되지 않은 단계를 완료로 가장하지
    않는다. 어느 단계까지 성공했는지는 기존 ``completed`` 목록으로 판정하고,
    export처럼 ``completed``에 반영되지 않는 단계는 ``export_started`` 플래그로
    별도 추적한다 — 기존 partial-run outcome 계약(``stages_completed``)은
    바꾸지 않는다.

    매개변수:
        capture_silver: True면 검증을 통과한 SilverDataset을 결과에 함께 담는다.
            composition(#506)이 이 소스를 참조할 때만 켜서, composition을 쓰지
            않는 일반 빌드는 Silver 테이블을 불필요하게 오래 들고 있지 않는다.
        cancellation: 협력적 취소 probe (#481). ``None``이면(동기 ``POST /build``,
            CLI) 취소 점검 자체가 없어 기존 동작과 100% 동일하다. probe가 있으면
            **stage 사이의 안전 경계**에서만 점검한다 — 실행 중인 stage를 중간에
            끊지 않고(부분 object를 남기지 않는다), 직전 stage의 산출물이 디스크에
            기록된 *뒤* 다음 stage를 새로 시작하기 *전*에만 확인한다.
    """
    output_key = _output_source_key(source)
    completed: list[str] = []
    outputs: list[str] = []
    provenance_entry: SourceProvenance | None = None
    evaluated_row_count: int | None = None
    captured_silver: SilverDataset | None = None
    # 예외가 발생해도(schema 검증 실패, quality FAIL 등) 이미 계산된 구조화된
    # 결과는 살아남아 실패 outcome에도 실린다 (#486) — quality_results가 예외
    # 때문에 사라지지 않는다.
    quality_results: tuple[QualityCheckResult, ...] = ()
    quality_evaluated = False
    schema_drift: tuple[SchemaDriftFinding, ...] = ()
    drift_evaluation: tuple[DriftEvaluation, ...] = ()
    # completed 목록에 없는 export 단계 진행 여부를 별도로 추적한다(#496) —
    # export는 gold 완료 후 실행되지만 기존 outcome 모델(stages_completed)에는
    # 반영되지 않는다. fetch_completed는 "bronze" not in completed만으로는
    # source fetch 성공 여부와 bronze persist 성공 여부를 구분할 수 없어서
    # (persist가 fetch *이후*에 실패할 수 있다) 별도로 추적한다.
    export_started = False
    fetch_completed = False
    try:
        # 경계 0 (#481): 이 소스는 아직 아무 것도 시작하지 않았다. worker pool
        # (#247, 최대 4개)에서 순서를 기다리다 이제 막 시작하려는 소스는 취소가
        # 이미 요청됐다면 fetch 자체를 시작하지 않는다 — 취소 후 새 원격 I/O를
        # 시작하지 않기 위해서다.
        raise_if_cancelled(cancellation)
        # kind(public_api/file/url)에 맞는 resolver로 원시 레코드를 가져온다
        # (#498) — 이후 Silver/Gold는 kind를 전혀 알 필요가 없다. bronze stage와
        # source fetch는 이 호출을 공유 경계로 삼는다(#496) — public_api/file/url
        # 모두 동일한 event 어휘를 쓴다.
        recorder.stage_started(output_key, "bronze")
        recorder.source_fetch_started(output_key)
        bronze = build_bronze_artifact_for_source(
            source,
            client=client,
            upload_repository=upload_repository,
            owner_id=owner_id,
        )
        recorder.source_fetch_completed(output_key, record_count=len(bronze.raw_records))
        fetch_completed = True
        bronze = _retag_bronze_artifact(bronze, output_key=output_key)
        bronze_paths = persist_bronze_artifact(
            bronze, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("bronze")
        recorder.stage_completed(
            output_key,
            "bronze",
            message="Bronze written",
            metrics={"records": len(bronze.raw_records)},
        )
        _record_output_paths(outputs, bronze_paths.records_path, bronze_paths.metadata_path)
        # bronze 성공 직후 확정한다: 이후 단계가 실패해도(부분 실패) provenance는 남는다
        # (병렬화 이전 shared list에 즉시 append하던 것과 동일한 동작을 유지) (#247).
        provenance_provider, provenance_dataset = source_identity(source)
        provenance_entry = build_source_provenance(
            provider=provenance_provider,
            dataset=provenance_dataset,
            fetched_at=bronze.fetched_at,
            records=bronze.raw_records,
            # bronze.fetch_params는 kind별 resolver가 이미 secret/path 없이
            # 채운 값이다(#498) — file은 upload_id/format/encoding, url은 query
            # string이 제거된 endpoint/method, public_api는 기존 source.params
            # 그대로다.
            params=bronze.fetch_params,
        )

        # 경계 1 (#481): Bronze 산출물이 디스크에 기록되고 provenance까지 확정된
        # 뒤다. 여기서 취소가 관찰되면 Bronze는 그대로 보존되고 Silver는 아예
        # 시작하지 않는다.
        raise_if_cancelled(cancellation)

        recorder.stage_started(output_key, "silver")
        required_columns = source.schema.required if source.schema else ()
        column_dtypes = source.schema.dtypes if source.schema else None
        silver = build_silver_dataset(
            bronze,
            required_columns=required_columns,
            casts=source.schema.casts if source.schema else None,
            rename=source.schema.rename if source.schema else None,
            derived=source.schema.derived if source.schema else (),
            read_as=source.schema.read_as if source.schema else None,
            null_tokens=source.schema.null_tokens if source.schema else (),
            column_null_tokens=(source.schema.column_null_tokens if source.schema else None),
            coalesce=source.schema.coalesce if source.schema else None,
            zfill=source.schema.zfill if source.schema else None,
            column_dtypes=column_dtypes,
        )
        evaluated_row_count = silver.statistics.row_count

        # 구조화된 Quality/Schema 평가 (#486). Preview와 동일한 공통 evaluator를
        # 쓴다 — 예외가 아래에서 발생해도 quality_results는 이미 채워져 있으므로
        # 실패 outcome의 manifest에도 보존된다.
        quality_results = evaluate_quality(
            silver,
            context.spec.quality,
            source_key=output_key,
            required_columns=required_columns,
            column_dtypes=column_dtypes,
        )
        quality_evaluated = True
        # quality checkpoint event(#496)는 #486 판정 결과를 그대로 반영한다 —
        # 이 아래에서 FAIL로 소스가 실패해도(quality gate) 평가가 실제로
        # 일어났다는 사실과 그 결과는 이미 기록된다(partial run 가시성).
        recorder.quality_evaluated(output_key, quality_results)

        # 검증에 실패한 Silver 데이터셋이 Gold/패키징으로 흘러가지 않도록 소스를
        # 실패 처리한다. 검증은 더 이상 권고용이 아니라 게이트다 (#189). 기존 오류
        # 계약(메시지 형식)은 하위 호환을 위해 그대로 유지한다.
        if not silver.validation.ok:
            # ValidationProblem 객체를 DatasetValidationError가 기대하는 문자열 목록으로 변환 (#261)
            problem_messages = [problem.message for problem in silver.validation.problems]
            raise DatasetValidationError(problem_messages)

        # PII 스캔 게이트 (#441, QG-1). 원본 값은 결과/로그에 담지 않는다.
        # block: 검출 시 빌드 실패, warn: manifest/로그 경고, allow: 통과.
        if context.spec.pii is not None:
            findings = [
                f for f in scan_pii(silver.table) if f.column not in context.spec.pii.allow_columns
            ]
            if findings:
                if context.spec.pii.mode == "block":
                    raise DatasetValidationError(
                        [f"PII 검출({f.kind}) @ {f.column}: {f.count}건" for f in findings]
                    )
                if context.spec.pii.mode == "warn":
                    logger.warning(
                        "PII 의심 컬럼 (warn): %s",
                        ", ".join(f"{f.kind}@{f.column}({f.count})" for f in findings),
                    )

        # 품질 WARN/FAIL 게이트 (#446, #486). WARN은 로그만 남기고 계속 진행하며,
        # FAIL은 Gold 진입 전에 소스를 실패 처리한다. quality_results는 이미 위에서
        # 채워졌으므로 여기서 raise해도 manifest에 보존된다.
        fail_results = [r for r in quality_results if r.status == "fail"]
        if fail_results:
            raise DatasetValidationError(_quality_failure_messages(fail_results))
        for r in quality_results:
            if r.status == "warn":
                logger.warning(
                    "품질 위반(warn): %s%s actual=%s threshold=%s (#486)",
                    r.rule,
                    f" @ {r.column}" if r.column else "",
                    r.actual,
                    r.threshold,
                )

        # Drift detection (#445, DRIFT-1). Compared only against the previous
        # successful run of the same dataset_id and source_key, so another
        # dataset's silver cannot manufacture drift (#486). The baseline is also
        # restricted to the same owner (#700): another user's run as the baseline
        # turns row count and schema changes into a metadata side channel.
        baseline = find_previous_silver(
            context.output_root,
            context.run_id,
            dataset_id=context.spec.dataset_id,
            source_key=output_key,
            owner_id=baseline_owner_id,
        )
        if isinstance(baseline, SilverBaseline):
            drift_findings = detect_drift(
                silver.schema, silver.statistics, baseline.schema, baseline.stats
            )
            schema_drift = _to_schema_drift_findings(drift_findings)
            for f in drift_findings:
                logger.warning("드리프트 감지: %s @ %s — %s (#445)", f.kind, f.column, f.detail)
            drift_evaluation = (
                DriftEvaluation(
                    axis="schema",
                    evaluated=True,
                    baseline_snapshot_id=baseline.run_id,
                    detail=f"compared against run {baseline.run_id}",
                ),
            )
        else:
            # Leaving schema_drift empty drops the key from the manifest, and then
            # "there was no baseline" and "compared, nothing changed" are the same
            # answer on the wire (#700). Record the failure to evaluate explicitly.
            drift_evaluation = (
                DriftEvaluation(
                    axis="schema",
                    evaluated=False,
                    reason=baseline.reason,
                    detail=baseline.detail,
                ),
            )
            logger.info("드리프트 미평가: %s — %s (#700)", output_key, baseline.detail)

        silver_paths = persist_silver_dataset(
            silver, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("silver")
        recorder.stage_completed(
            output_key,
            "silver",
            message="Silver written",
            metrics={"row_count": evaluated_row_count},
        )
        # Silver 게이트(schema/PII/quality)를 모두 통과한 시점에만 담는다 — composition(#506)이
        # 이 값을 join에 그대로 쓰므로, 검증 실패한 데이터가 흘러들면 안 된다.
        if capture_silver:
            captured_silver = silver
        _record_output_paths(
            outputs,
            silver_paths.table_path,
            silver_paths.schema_path,
            silver_paths.stats_path,
            silver_paths.preview_path,
            silver_paths.validation_path,
        )

        # 경계 2 (#481): Silver 산출물이 모두 기록된 뒤다. 취소가 관찰되면
        # Bronze+Silver를 보존하고 Gold는 시작하지 않는다.
        raise_if_cancelled(cancellation)

        recorder.stage_started(output_key, "gold")
        gold = build_gold_package(
            silver,
            dataset_name=output_key,
            exports=context.spec.exports,
            metadata=_gold_package_metadata(context.spec),
            splits_spec=context.spec.splits,
        )
        gold_paths = persist_gold_package(
            gold, output_root=context.output_root, run_id=context.run_id
        )
        completed.append("gold")
        recorder.stage_completed(
            output_key, "gold", message="Gold written", metrics={"row_count": len(gold.table)}
        )
        _record_output_paths(
            outputs,
            gold_paths.table_path,
            gold_paths.package_path,
            *gold_paths.splits_paths.values(),
        )

        # 경계 3 (#481): Gold 산출물이 기록된 뒤, export를 시작하기 전이다.
        # 이 소스의 medallion 단계는 모두 끝났고 export만 남았다.
        raise_if_cancelled(cancellation)

        # export 단계(#496): SourceBuildOutcome.stages_completed에는 반영되지
        # 않는 기존 모델을 유지하되(#488 stage API와 동일 계약), 실제 export
        # 실행 boundary에서 started/completed/failed를 별도로 기록한다.
        export_started = True
        recorder.stage_started(output_key, "export")
        export_paths = export_gold_package(gold, output_dir=gold_paths.gold_dir)
        _record_output_paths(outputs, *export_paths)

        card = build_dataset_card(
            title=context.spec.title,
            description=context.spec.description,
            sources=(output_key,),
            fields=(
                (column.name, column.dtype, column.nullable) for column in silver.schema.columns
            ),
            sample_rows=silver.preview.rows,
            license=_dataset_card_license(context.spec),
            version=_dataset_card_version(context.spec),
        )
        card_path = gold_paths.gold_dir / "README.md"
        _ = card_path.write_text(render_dataset_card(card), encoding="utf-8")
        _record_output_paths(outputs, card_path)

        # BuildSpec.exports는 위 export_gold_package가 이미 전부 실행했다 —
        # package.export_plan.targets가 곧 spec.exports이기 때문이다 (#629).
        # 예전에는 같은 타깃을 같은 디렉터리에 한 번 더 썼고, 두 번째가 만든
        # ArtifactDataset에는 schema가 없어서 게시되는 산출물에서 schema가
        # 사라졌다. manifest outputs에는 같은 경로가 중복됐고 file_count는 두
        # 배였다.
        recorder.stage_completed(
            output_key,
            "export",
            message="Export written",
            metrics={"file_count": len(export_paths)},
        )

        schema_summary = build_schema_summary(
            (column.name, column.dtype, column.nullable) for column in silver.schema.columns
        )
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key, status="ok", stages_completed=tuple(completed)
            ),
            output_paths=tuple(outputs),
            row_count=evaluated_row_count,
            schema_summary=schema_summary,
            provenance_entry=provenance_entry,
            quality_results=quality_results,
            quality_evaluated=quality_evaluated,
            schema_drift=schema_drift,
            drift_evaluation=drift_evaluation,
            silver=captured_silver,
        )
    except BuildCancelled:
        # 협력적 취소는 실패가 아니다 (#481) — 아래 공통 except보다 먼저 잡아
        # "이 소스 실패"로 해석되지 않게 한다. 실패 event(stage_failed/
        # source_fetch_failed)를 남기지 않고, error도 채우지 않는다. 이미 완료된
        # 단계(completed)와 그 산출물(outputs)은 그대로 보존해 partial manifest에
        # 실린다 — 실행되지 않은 단계는 어디에도 성공으로 기록되지 않는다.
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key,
                status="cancelled",
                stages_completed=tuple(completed),
            ),
            output_paths=tuple(outputs),
            row_count=evaluated_row_count,
            provenance_entry=provenance_entry,
            quality_results=quality_results,
            quality_evaluated=quality_evaluated,
            schema_drift=schema_drift,
            drift_evaluation=drift_evaluation,
            silver=captured_silver,
        )
    except Exception as exc:  # stage 실패를 결과로 변환하여 매니페스트에 기록
        # 검증 오류(ValidationError, DatasetValidationError)는 파일시스템 경로를
        # 포함하지 않으므로 메시지를 그대로 전달한다. IngestionError(#498)도
        # 마찬가지로 안전한 메시지만 담도록 설계되어 있다(raw 응답 본문/내부
        # 스택 없음) — SSRF 차단·오버사이즈·손상 파일 사유를 사용자가 바로
        # 확인할 수 있어야 한다.
        # ExportError/ManifestError 등 다른 BuildError 하위 예외는 목적지 경로
        # 같은 내부 정보를 메시지에 포함할 수 있으므로, 상세 내용은 서버 경고로
        # 기록하고 클라이언트에는 일반 메시지만 반환한다 (#225).
        if isinstance(exc, (ValidationError, DatasetValidationError, IngestionError)):
            error_msg = str(exc)
        else:
            logger.error(
                "source pipeline failed for %r: %s",
                output_key,
                exc,
                exc_info=exc,
            )
            error_msg = f"pipeline failed for source {output_key!r}"
        # 실제로 도달한 마지막 boundary 기준으로 실패 event를 남긴다(#496) —
        # completed 목록이 이미 "어느 stage까지 성공했는지"의 정본이므로, 그
        # 다음 stage가 실패한 stage다. 시도조차 되지 않은 stage는 실패로
        # 가장하지 않는다(이벤트를 아예 남기지 않는다).
        if "bronze" not in completed:
            if not fetch_completed:
                recorder.source_fetch_failed(output_key, message=error_msg)
            recorder.stage_failed(output_key, "bronze", message=error_msg)
        elif "silver" not in completed:
            recorder.stage_failed(output_key, "silver", message=error_msg)
        elif "gold" not in completed:
            recorder.stage_failed(output_key, "gold", message=error_msg)
        elif export_started:
            recorder.stage_failed(output_key, "export", message=error_msg)
        return _SourcePipelineResult(
            outcome=SourceBuildOutcome(
                source_key=output_key,
                status="failed",
                stages_completed=tuple(completed),
                error=error_msg,
            ),
            output_paths=tuple(outputs),
            row_count=evaluated_row_count,
            provenance_entry=provenance_entry,
            quality_results=quality_results,
            quality_evaluated=quality_evaluated,
            schema_drift=schema_drift,
            drift_evaluation=drift_evaluation,
            silver=captured_silver,
        )


@dataclass(frozen=True)
class _CompositionPipelineResult:
    """composition 실행의 로컬 결과 (#506). _SourcePipelineResult와 동일한 이유로
    분리한다 — 모든 source 스레드가 끝난 뒤 단일 스레드에서만 실행되지만, 반환값을
    바로 매니페스트 병합 루프에 꽂을 수 있게 같은 모양을 유지한다."""

    outcome: CompositionOutcome
    output_paths: tuple[str, ...] = ()
    row_count: int | None = None
    schema_summary: SchemaSummary | None = None
    provenance: CompositionProvenance | None = None


def _run_composition(
    composition: CompositionSpec,
    *,
    silver_by_key: Mapping[str, SilverDataset],
    context: BuildContext,
) -> _CompositionPipelineResult:
    """composition(join)을 실행하고 결합 Gold 산출물을 저장한다 (#506).

    join key 존재/dtype 호환성 검증과 duplicate-key explosion 감지는 여기서
    호출하는 ``build_composed_gold_package``(빌드 파이프라인의 런타임 검증
    게이트)가 담당한다 — spec.validator는 alias 참조 같은 구조만 검증한다.

    참조된 source 중 하나라도 Silver를 통과하지 못했으면(``silver_by_key``에
    없으면) join을 시도하지 않고 "skipped"로 반환한다.
    """
    join = composition.join
    missing = [alias for alias in (join.left, join.right) if alias not in silver_by_key]
    if missing:
        return _CompositionPipelineResult(
            outcome=CompositionOutcome(
                name=composition.name,
                status="skipped",
                error=(
                    f"source(s) {missing} did not complete Silver successfully; "
                    "composition was not attempted"
                ),
            )
        )

    try:
        package, stats = build_composed_gold_package(
            left_silver=silver_by_key[join.left],
            right_silver=silver_by_key[join.right],
            join=join,
            dataset_name=composition.name,
            exports=context.spec.exports,
            metadata=_gold_package_metadata(context.spec),
        )
    except CompositionError as exc:
        return _CompositionPipelineResult(
            outcome=CompositionOutcome(name=composition.name, status="failed", error=str(exc))
        )

    if stats.duplicate_key_warning:
        # on_duplicate_key="fail"이었다면 build_composed_gold_package가 이미 CompositionError를
        # 던졌으므로 여기 도달했다는 건 severity="warn"(기본)이라는 뜻이다 — 로그만 남기고 진행.
        logger.warning(
            "composition %r: duplicate join keys on both sides may have multiplied rows "
            "(left=%s distinct_keys=%d/%d rows, right=%s distinct_keys=%d/%d rows, "
            "output_rows=%d) (#506)",
            composition.name,
            join.left,
            stats.left_distinct_key_count,
            stats.left_row_count,
            join.right,
            stats.right_distinct_key_count,
            stats.right_row_count,
            stats.output_row_count,
        )

    outputs: list[str] = []
    gold_paths = persist_gold_package(
        package, output_root=context.output_root, run_id=context.run_id
    )
    _record_output_paths(
        outputs,
        gold_paths.table_path,
        gold_paths.package_path,
        *gold_paths.splits_paths.values(),
    )
    export_paths = export_gold_package(package, output_dir=gold_paths.gold_dir)
    _record_output_paths(outputs, *export_paths)

    combined_schema = build_schema(package.table)
    card = build_dataset_card(
        title=context.spec.title,
        description=context.spec.description,
        sources=(join.left, join.right),
        fields=((col.name, col.dtype, col.nullable) for col in combined_schema.columns),
        sample_rows=package.table.head(DEFAULT_PREVIEW_LIMIT).to_dicts(),
        license=_dataset_card_license(context.spec),
        version=_dataset_card_version(context.spec),
    )
    card_path = gold_paths.gold_dir / "README.md"
    _ = card_path.write_text(render_dataset_card(card), encoding="utf-8")
    _record_output_paths(outputs, card_path)

    # 단일 소스 경로와 같은 이유로 여기서도 다시 export하지 않는다 (#629).

    schema_summary = build_schema_summary(
        (col.name, col.dtype, col.nullable) for col in combined_schema.columns
    )
    provenance = CompositionProvenance(
        name=composition.name,
        left=join.left,
        right=join.right,
        join_type=join.type,
        left_key=join.left_key,
        right_key=join.right_key,
        left_row_count=stats.left_row_count,
        left_distinct_key_count=stats.left_distinct_key_count,
        right_row_count=stats.right_row_count,
        right_distinct_key_count=stats.right_distinct_key_count,
        output_row_count=stats.output_row_count,
        duplicate_key_warning=stats.duplicate_key_warning,
    )
    return _CompositionPipelineResult(
        outcome=CompositionOutcome(name=composition.name, status="ok"),
        output_paths=tuple(outputs),
        row_count=stats.output_row_count,
        schema_summary=schema_summary,
        provenance=provenance,
    )


def run_build(
    spec: BuildSpec,
    *,
    client: SourceClient,
    output_root: Path,
    run_id: str | None = None,
    created_by: str | None = None,
    owner_id: str | None = None,
    manifest_owner_id: str | None = None,
    upload_repository: UploadRepository | None = None,
    event_store: BuildEventStore | None = None,
    cancellation: CancellationProbe | None = None,
) -> BuildResult:
    """BuildSpec을 Medallion 파이프라인으로 실행한다.

    매개변수:
        spec: 실행할 빌드 명세.
        client: Bronze fetch에 사용할 kpubdata 호환 클라이언트.
        output_root: 실행 워크스페이스 루트.
        run_id: 실행 식별자. 생략 시 타임스탬프 기반으로 생성.
        created_by: 빌드를 요청한 principal의 display/legacy 라벨(#388).
        owner_id: ``kind="file"`` source resolver가 업로드 소유권 확인에
            쓰는 canonical stable owner identity(#505/#498) — "이 run이
            참조하는 업로드의 소유자"다. sync ``/build``는 언제나 제출
            principal 본인이 곧 그 소유자이므로 그대로 재사용해도 안전하지만,
            async ``/builds``는 지금까지처럼 ``None``으로 남겨 file-backed
            source resolver에 stable identity를 노출하지 않는다(#498 async
            limitation, 그대로 유지).
        manifest_owner_id: persisted run manifest(및 그 manifest를 그대로
            읽어 채우는 BuildIndex, #505 SSOT)에 기록할 canonical stable
            owner identity — "이 run을 제출한 principal"이다. 생략(``None``)
            하면 ``owner_id``를 그대로 쓴다(기존 sync 호출자와 100% 동일하게
            동작). async 호출자는 file resolver용 ``owner_id``는 ``None``으로
            둔 채 이 필드만 채워, "누가 이 run을 제출했는가"(persisted
            ownership)와 "file resolver가 무엇을 소유권 확인에 쓸 수
            있는가"를 서로 다른 값으로 분리한다.
        upload_repository: ``kind="file"`` source의 업로드 content를 조회할
            저장소 (#498). None이면 file source가 있는 build는 실패한다.
        event_store: structured run event timeline 저장소 (#496). None이면
            (CLI 직접 호출 등) event를 전혀 기록하지 않는다 — 기존 호출자는
            아무 것도 바꾸지 않아도 된다.
        cancellation: 협력적 취소 probe (#481). None이면(동기 ``POST /build``,
            CLI) 취소 점검이 전혀 일어나지 않아 기존 동작과 100% 동일하다.
            probe가 있으면 각 소스의 stage 경계와, 모든 소스가 끝난 뒤
            composition/manifest finalize 직전의 마지막 경계(``commit()``)에서
            점검한다. ``commit()``이 성공하면 그 시점부터는 정상 종료로
            확정되어 이후 취소 요청은 거절된다 — 성공 manifest를 쓴 run이
            나중에 cancelled로 뒤집히는 모순을 구조적으로 막는다.

    반환값:
        BuildResult: 전체 상태("ok"/"failed"/"cancelled"), 소스별 결과,
        매니페스트 경로. "cancelled"는 cancellation probe가 있는 호출자에서만
        나온다.

    예외:
        ValidationError: spec이 최소 실행 요건을 만족하지 못한 경우.
        ValueError: run_id에 안전하지 않은 문자가 포함된 경우.
    """
    effective_manifest_owner_id = owner_id if manifest_owner_id is None else manifest_owner_id
    # 진입점에서 spec을 먼저 검증한다(fail-fast). 검증을 호출자에게만 맡기면 잘못된
    # spec이 단계 깊숙이 들어가 cryptic 에러로 터지므로, 단계 진입 전에 막는다 (#212).
    validate_spec(spec)
    context = BuildContext.create(spec, output_root=output_root, run_id=run_id)
    # run_id가 확정된(BuildContext.create가 생략 시 생성) 직후에만 recorder를
    # 만들 수 있다 — "started"는 실제로 실행이 시작되는 이 지점의 실제 semantics다
    # (#496). validate_spec 실패는 run_id/워크스페이스가 아예 없으므로 event를
    # 남기지 않는다 — 기존에도 그 실패에 대해서는 워크스페이스가 만들어지지 않는다.
    recorder = BuildEventRecorder(event_store, run_id=context.run_id)
    recorder.run_started()
    # 검증된 실제 실행 입력을 pipeline보다 먼저 고정한다. 이후 source 단계가 실패해도
    # run 감사 정보는 남고, validation 실패 입력은 snapshot으로 기록되지 않는다.
    _, spec_digest = write_buildspec_snapshot(
        spec, output_root=context.output_root, run_id=context.run_id
    )

    # composition(#506)이 참조하는 alias의 Silver만 스레드 결과에 담아 살려둔다 —
    # composition을 쓰지 않는 빌드는 기존과 동일하게 아무 것도 추가로 보존하지 않는다.
    composition_aliases: frozenset[str] = (
        frozenset({spec.composition.join.left, spec.composition.join.right})
        if spec.composition is not None
        else frozenset()
    )

    def _worker(source: SourceRef) -> _SourcePipelineResult:
        return _run_source_pipeline(
            source,
            client=client,
            context=context,
            recorder=recorder,
            upload_repository=upload_repository,
            owner_id=owner_id,
            baseline_owner_id=effective_manifest_owner_id,
            capture_silver=_output_source_key(source) in composition_aliases,
            cancellation=cancellation,
        )

    # 소스별 fetch/stage는 대부분 네트워크 I/O 대기이므로 스레드 풀로 동시에 실행해
    # 총 소요 시간을 줄인다 (#247). executor.map은 완료 순서가 아니라 spec.sources
    # 순서로 결과를 반환하므로 이후 병합 결과(매니페스트)가 결정적으로 유지된다.
    max_workers = min(len(spec.sources), _MAX_PARALLEL_SOURCES)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = list(executor.map(_worker, spec.sources))

    outcomes = tuple(result.outcome for result in results)

    # 모든 소스 실행이 끝난 뒤 단일 스레드에서 병합한다 — 스레드 간 공유 가변
    # 상태가 없으므로 이 단계는 병렬화 이전과 동일하게 안전하다 (#247).
    outputs: list[str] = []
    row_counts: dict[str, int] = {}
    schema_summaries: dict[str, SchemaSummary] = {}
    provenance: list[SourceProvenance] = []
    quality_results: dict[str, tuple[QualityCheckResult, ...]] = {}
    schema_drift: dict[str, tuple[SchemaDriftFinding, ...]] = {}
    drift_evaluation: dict[str, tuple[DriftEvaluation, ...]] = {}
    silver_by_key: dict[str, SilverDataset] = {}
    for result in results:
        outputs.extend(result.output_paths)
        if result.row_count is not None:
            row_counts[result.outcome.source_key] = result.row_count
        if result.schema_summary is not None:
            schema_summaries[result.outcome.source_key] = result.schema_summary
        if result.provenance_entry is not None:
            provenance.append(result.provenance_entry)
        # quality_evaluated는 evaluate_quality가 실제로 호출됐는지를 나타낸다(#486) —
        # FAIL로 소스가 실패해도 결과는 보존된다. bronze/silver 자체가 실패해
        # evaluate_quality에 도달하지 못한 소스는 매니페스트에 키가 생기지 않는다
        # (0건 평가와 "평가 자체가 없었음"을 구분한다).
        if result.quality_evaluated:
            quality_results[result.outcome.source_key] = result.quality_results
        if result.schema_drift:
            schema_drift[result.outcome.source_key] = result.schema_drift
        # Recorded unconditionally, unlike schema_drift: "not evaluated" is the case
        # that has to survive to the manifest, and dropping a falsy value is exactly
        # how it used to disappear (#700).
        if result.drift_evaluation:
            drift_evaluation[result.outcome.source_key] = result.drift_evaluation
        if result.silver is not None:
            silver_by_key[result.outcome.source_key] = result.silver

    # ---- 마지막 안전 경계 (#481) --------------------------------------------
    #
    # 여기가 이 run의 "point of no return"이다. 두 가지 경우에 취소로 확정한다.
    #
    #   (a) 어떤 소스든 stage 경계에서 이미 취소를 관찰한 경우.
    #   (b) 모든 소스가 끝났지만 composition/manifest finalize에 진입하기 직전에
    #       취소 요청이 도착한 경우 — ``commit()``이 False를 반환한다.
    #
    # ``commit()``이 True를 반환하면 그 시점부터 취소 요청은 거절되므로
    # (``CancellationProbe`` 계약), 아래 정상 경로가 성공/실패 manifest를 쓰는
    # 동안 job 상태가 cancelling으로 바뀌는 일이 없다 — "성공 manifest를 쓴 run이
    # cancelled로 뒤집히는" 모순과 "cancelling -> succeeded" 전이를 구조적으로
    # 막는다. (a)에서는 이미 취소가 요청된 것이 확정이라 commit을 호출하지 않는다.
    cancelled = any(outcome.status == "cancelled" for outcome in outcomes)
    if not cancelled and cancellation is not None and not cancellation.commit():
        cancelled = True

    # composition(#506)은 모든 source가 끝난 뒤, 참조된 두 source의 검증된 Silver를
    # 가지고 단일 스레드에서 실행한다 — join 자체는 병렬화 대상이 아니다.
    # 취소된 run에서는 아예 시작하지 않는다 — 취소 이후 새 stage를 시작하지 않는
    # 원칙이 source 단계와 동일하게 적용되고, manifest도 실행되지 않은
    # composition을 성공으로 기록하지 않는다(composition은 null로 남는다).
    composition_outcome: CompositionOutcome | None = None
    composition_provenance: CompositionProvenance | None = None
    if spec.composition is not None and not cancelled:
        composition_result = _run_composition(
            spec.composition, silver_by_key=silver_by_key, context=context
        )
        composition_outcome = composition_result.outcome
        composition_provenance = composition_result.provenance
        outputs.extend(composition_result.output_paths)
        if composition_result.row_count is not None:
            row_counts[composition_result.outcome.name] = composition_result.row_count
        if composition_result.schema_summary is not None:
            schema_summaries[composition_result.outcome.name] = composition_result.schema_summary

    errors = tuple(
        f"{outcome.source_key}: {outcome.error}"
        for outcome in outcomes
        if outcome.status == "failed" and outcome.error is not None
    )
    if composition_outcome is not None and composition_outcome.status != "ok":
        errors = (*errors, f"{composition_outcome.name}: {composition_outcome.error}")
    if cancelled:
        # 취소는 실패가 아니므로 run_failed를 남기지 않고, 정상 완료도 아니므로
        # run_finished도 남기지 않는다 (#481). 종결 event(``run_cancelled``)는
        # job의 terminal 전이를 실제로 확정하는 service 계층이 정확히 한 번
        # 남긴다 — 여기서도 남기면 queued 취소(파이프라인이 아예 실행되지 않는
        # 경로)와 event 개수가 달라지고, 같은 run에 종결 event가 둘 생긴다.
        #
        # 이미 실패한 소스가 있었다면 그 사유는 errors에 그대로 남긴다 — 취소가
        # 실패를 삼키지 않는다. 다만 run의 종단 상태는 "cancelled"다.
        status = "cancelled"
    elif errors:
        status = "failed"
        recorder.run_failed(failed_source_count=len(errors))
    else:
        status = "ok"
        recorder.run_finished()

    manifest = BuildManifest(
        build_id=context.run_id,
        status=status,
        # partial(#481)은 "정상 완료 전에 종료되어 outputs가 부분 산출물"이라는
        # 뜻이다. 취소된 run에만 True이며, 실패한 run의 부분성은 기존대로
        # status/errors로 표현한다(기존 소비자 의미를 바꾸지 않는다).
        partial=cancelled,
        started_at=context.started_at,
        finished_at=utc_now(),
        inputs=tuple(_output_source_key(source) for source in spec.sources),
        outputs=tuple(outputs),
        # recorder가 흡수한 event 기록 실패(#496)를 여기 싣는다 — 새 API 필드를
        # 추가하지 않고 기존 authoritative warnings 채널을 재사용해, event
        # timeline에 실제로 구멍이 있었는지 API 소비자가 알 수 있게 한다.
        warnings=recorder.dropped_events(),
        errors=errors,
        row_counts=row_counts,
        schema_summaries=schema_summaries,
        provenance=tuple(provenance),
        build_environment=capture_build_environment(),
        inputs_fingerprint=compute_inputs_fingerprint(provenance),
        created_by=created_by,
        owner_id=effective_manifest_owner_id,
        quality_results=quality_results,
        schema_drift=schema_drift,
        drift_evaluation=drift_evaluation,
        composition=composition_provenance,
    )
    manifest_path = context.output_root / context.run_id / "manifest.json"
    manifest_writer(manifest, manifest_path)

    return BuildResult(
        context=context,
        status=status,
        outcomes=outcomes,
        manifest_path=manifest_path,
        spec_digest=spec_digest,
        composition_outcome=composition_outcome,
    )

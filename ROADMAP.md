# 로드맵 — kpubdata-builder

> **이 문서는 작업 진행상태의 정본이 아니다.** 방향(NOW / NEXT / LATER)만 적는다.
> Issue 의 Status · Priority · Epic · Target Release 는 GitHub Project 에서 관리한다 — [POLICY.md](https://github.com/kpubdata-lab/kpubdata/blob/main/docs/governance/POLICY.md) 2.1절.
> 데이터셋 지원 상태는 이 저장소가 소유하지 않는다 — [kpubdata 의 생성 문서](https://github.com/kpubdata-lab/kpubdata/blob/main/SUPPORTED_DATA.md)가 기준이다(POLICY 3절).

> **✅ 는 "그 범위의 작업이 끝났다" 는 뜻이고, 릴리스 여부가 아니다.**
>
> 선언과 태그는 이제 맞는다 — `pyproject.toml` 의 `0.4.0` 과 태그·GitHub Release
> `v0.4.0`(2026-09-28). 그래도 **로드맵 번호와 패키지 버전은 다른 것을 센다.** 아래
> v0.5 는 작업 범위의 이름이고, 그 작업이 담긴 릴리스 번호가 아니다. 어느 릴리스에
> 무엇이 들어갔는지는 [CHANGELOG](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/CHANGELOG.md) 가 기준이다.
>
> 둘이 다시 어긋나지 않게 **게이트가 막는다**(#690). `scripts/check_version_consistency.py`
> 가 `pyproject.toml` 을 정본으로 CHANGELOG 와 git 태그를 대조하고, 개발 버전
> (`.dev`/`a`/`b`/`rc`)으로는 태그 발행 자체를 거부한다. `release.yml` 과
> `docker.yml` 이 태그·이미지를 만들기 전에 이것을 돌리므로, 라벨과 내용이 어긋난
> 이미지는 발행되지 않는다. 이미지 안의 `kpubdata-builder --version` 과 manifest 의
> `build_environment.builder_version` 은 둘 다 설치된 배포판 메타데이터에서 읽는다.

> kpubdata-builder는 **원시 공공데이터를 정제된, 검증된, 배포 가능한 데이터셋으로 변환하는 빌드 엔진**입니다.

## 개발 축

| 축 | 설명 |
| :--- | :--- |
| **Build Pipeline** | spec 파싱 → orchestrator → export → manifest |
| **Medallion Pipeline** | Bronze/Silver/Gold stage, 승격 규칙 |
| **Export & Publish** | 출력 형식 + 배포 대상 |
| **Service** | HTTP 서비스 모드, 인증, 카탈로그 |

---

## v0.1 ✅ 완료

Medallion 파이프라인 기반 구축.

- ✅ BuildSpec 계약 안정화 (YAML 파싱, 검증)
- ✅ Medallion 디렉터리 재구성 (stages/bronze, silver, gold)
- ✅ Bronze/Silver/Gold stage 구현
- ✅ Polars 기반 tabular engine
- ✅ Pipeline orchestrator
- ✅ manifest 스키마 안정화

## v0.2 ✅ 완료

Export 확장, Dataset Identity, CLI.

- ✅ Markdown / JSONL / Parquet / HuggingFace layout exporter
- ✅ stage-aware exporters (Gold 기반)
- ✅ Publish command — 로컬 → 원격 배포
- ✅ Manifest를 dataset release record로 승격
- ✅ Schema summary + provenance tracking
- ✅ Build / Validate / Preview CLI command

## v0.3 ✅ 완료

Plugin 생태계와 고급 빌드 기능.

- ✅ Plugin exporter API (register_exporter_factory/instance, ADR 0004)
- ✅ Split 지원 (train/validation/test, by key)
- ✅ Kaggle dataset export
- ✅ Snapshot-aware builds
- ✅ Build diff/compare tools

## v0.4 ✅ 완료 — `v0.4.0` (2026-09-28)

인증, 카탈로그, BuildSpec 어시스턴트.

- ✅ 컨테이너 배포 — Dockerfile fail-closed, HEALTHCHECK, SIGTERM
- ✅ 인증 B2-B5 — Principal, Google OIDC Bearer, 허용 목록, 계약 1.1.0
- ✅ 인가 C1/C2 — manifest principal 기록, ENFORCE_OWNERSHIP
- ✅ BuildSpec 어시스턴트 BL1-BL4 — GET /catalog, problems 구조화, 계약 1.2.0
- ✅ read-only query backend — Silver/Gold `/query` sandbox (#504, 계약 1.7.0)
- ✅ ADR 0008 (async job), 0009 (auth), 0010 (state backend), 0011 (assistant)
- ✅ Azure Bicep IaC, 배포 가이드, request ID 추적
- ✅ 비동기 job 모델 구현 — 제출/polling/멱등(#480, #513), job registry·worker(#482),
  backpressure(#483), 협력적 취소·partial manifest(#481), owner 게이트(계약 1.18.0)

## v0.5 ✅ 완료 — UI vNext 지원 API (#484)

- ✅ BuildSpec·Preview 계약 정합(#485), Quality 구조화·gate·history(#486)
- ✅ Run별 canonical BuildSpec snapshot(#487), Dataset Catalog/Detail/Stage API(#488)
- ✅ Public API·File·URL source 통합 ingestion + SSRF 방어(#498)
- ✅ Silver/Gold read-only `/query`(#504), Stable Principal ID(#505)
- ✅ `/catalog` 탐색 metadata(#490), Publish readiness·HTTP publish(#491)
  + kaggle/local target(#550) + receipt reconcile/reset(#551) — 계약 1.20.0
- ✅ Provider Credential·Status/Test(#492), Run Event Timeline(#496)
- ✅ Preview diff/sampling(#497), Monitoring API(#516), Multi-source Join(#506)
- ✅ Email/Password IdP ADR 0015(Keycloak, #515 — ADR 0009 대체)
- ✅ cancelled run retention hooks(#549), Windows 테스트 결정성(#553)

## v1.0 기준

- ✅ BuildSpec 계약 안정
- ✅ 4개 exporter 안정 (Markdown, JSONL, Parquet, HuggingFace)
- ✅ 2개 publish 대상 (Hugging Face, Kaggle)
- ✅ Dataset card + manifest 자동 생성
- ✅ Plugin exporter API로 외부 확장 가능
- ✅ kpubdata-studio에서 전체 워크플로우 제어 — vNext UI 에픽(#484/#246) 완료,
  Studio E2E 34개 + cross-repo 실연동 슈트(studio PR #310)
- 🔲 Keycloak 전환 실행(ADR 0015 후속: realm 구축·owner_key 마이그레이션·Studio provider)
- 🔲 cross-repo E2E CI 자동화(kpubdata#282 — 워크플로 수정 권한 필요)

---

## 진행 중 — 2026-09-30 결정

작업 범위의 이름이고 릴리스 번호가 아니다. 다음 Builder·Studio 릴리스는 2026-10 창
(10/26–11/01) 이다 — kpubdata 호환성 문서 §5.1.

- 🔄 **DuckDB 로 tabular 엔진 전환** — [ADR 0021](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/0021-duckdb-tabular-engine.md),
  #864–#877. 단계마다 `tests/parity/` 기준선과 같아야 한다. 끝나면 `src/` 의 엔진은
  DuckDB 하나이고 레거시 publish 경로만 Polars 를 쓴다. 다중 테이블 SQL(#704)은 그 다음
- 🔄 **배포 형태에 따른 자격 증명 수명** — [ADR 0020](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/0020-credential-lifetime-by-deployment.md),
  ADR 0012 개정. 다중 사용자 배포는 키를 저장하지 않고 소유권을 강제한다
- 🔄 **레거시 publish 파이프라인 이관** — [ADR 0018](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/0018-legacy-publish-pipeline.md)
  선택지 C: config 를 하나씩 BuildSpec 으로 옮긴다
- ✅ kpubdata 0.8 핀 (`>=0.8.0,<0.9`, #882), Builder 소유 wire 어휘(#831), kpubdata
  private import 게이트(#830)

---

## ADR 인덱스

ADR 목록과 상태는 [docs/adrs/README.md](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/docs/adrs/README.md) 한 곳에서 관리한다 — 이 문서에
사본을 두면 어긋난다(0017–0021 이 빠져 있었고, 0008·0011 의 상태가 달랐다).

# 변경 이력

## v0.4

### 추가됨
- **CUBRID 상태 백엔드 (ADR 0016, #579)**: 영속 백엔드를 선택형으로 추상화한다 — `KPUBDATA_BUILDER_STORAGE_BACKEND`(`sqlite` 기본 / `cubrid`) + `KPUBDATA_BUILDER_CUBRID_URL`(`cubrid+pycubrid://…`). `BuildIndex`/`CredentialRepository`/`ArtifactStore` Protocol 뒤에 SQLAlchemy(`sqlalchemy-cubrid[pycubrid]`) 기반 CUBRID 구현체를 추가(optional `cubrid` extra — 기본 sqlite/local 경로는 SQLAlchemy 의존 없이 무외부의존 유지). manifest 문서는 CUBRID 행을 정본으로 + FS 미러로 이관하고(산출물 바이트는 블록 볼륨 유지, ADR 0003의 "manifest.json 정본" 조항을 cubrid 백엔드에 한해 supersede), OCI Compute VM + Docker Compose 배포(`infra/oci/`)를 추가한다. 실 CUBRID 통합 계약 테스트(`pytest -m cubrid`, `.github/workflows/cubrid.yml`)로 검증. 로컬 개발은 sqlite, 배포는 cubrid 전략.
- **Dashboard aggregate contract (#488/#486 후속, additive)**: `GET /datasets` 응답에 `total`(canonical grouping + ownership 이후, pagination 이전의 접근 가능한 distinct dataset_id 개수) 추가 — Studio Home DATASETS KPI가 `datasets` 길이/limit을 total로 오인하지 않게 한다. 새 `GET /quality/summary?window=24h`는 최근 24h 안 접근 가능한 run의 structured quality를 `total_runs`/`evaluated_runs`/`pass_runs`/`warn_runs`/`fail_runs`로 요약(도메인 Quality의 bounded cross-run aggregate — 시스템 observability `/monitoring/*`와 분리). 두 조회 모두 ownership을 적용하며 unavailable/0-check run을 PASS로 세지 않는다. API 계약 1.21.0 → 1.22.0
- **Multi-source Join/Composition (#506)**: `BuildSpec.composition`(`CompositionSpec`/`JoinSpec`)으로 두 source의 검증된 Silver를 equi-join해 결합 Gold dataset(`gold/{composition.name}/`) 생성. alias 필수/중복 검증, join key 존재/dtype 일치 런타임 게이트, duplicate-key many-to-many 폭증 warn/fail 게이트, manifest `composition`(`CompositionProvenance`, additive)과 `POST /build` 응답 `composition` 키로 결합 결과를 source별 결과와 구분해 노출. API 계약 1.11.0 → 1.12.0
- **Built Dataset Catalog·Detail·Stage Summary API (#488)**: `GET /datasets`, `GET /datasets/{dataset_id}`, `GET /datasets/{dataset_id}/runs`로 `BuildSpec.dataset_id` 단위 grouping/latest run/run history 조회. `GET /builds/{run_id}/stages`, `GET /builds/{run_id}/stages/{stage}`로 source별 Bronze/Silver/Gold 상태와 안전한 summary/preview 조회
- **인증 시스템 (B2-B5)**: Principal 추상화(#384), Google OIDC Bearer 검증(#385), 허용 목록 게이트(#386), API 계약 bearerAuth(#387)
- **인가 (C1/C2)**: manifest·BuildIndex에 principal 기록(#388), ENFORCE_OWNERSHIP 플래그(#389)
- **BuildSpec 어시스턴트 (BL1-BL4)**: ADR 0011(#415), GET /catalog(#416), /validate problems 구조화(#417), API 1.2.0(#418)
- **무인증 /healthz** + Dockerfile HEALTHCHECK (#372)
- **SIGTERM 우아운 종료** + max_workers env/CLI (#374)
- **CORS Authorization 헤더** + 파일 응답 Origin (#382)
- **Dockerfile ARG EXTRAS** (#373)
- **Azure Bicep IaC** (#378)
- **배포 가이드** docs/deploy.md (#390)
- **request ID 추적** (#379)
- **ADR 0008** 비동기 job 모델 (#334)
- **ADR 0009** 사용자 인증 Google OIDC (#383)
- **ADR 0010** ArtifactStore + 상태 백엔드 (#375)
- **ADR 0011** BuildSpec 어시스턴트 그라운딩 (#415)
- **환경변수 대조 테스트** (#424)
- 컨테이너 진입점 fail-closed (ADR 0006)

### 변경됨
- **quality 도메인 서비스 분리 (#596 다섯 번째 조각)**: run별 structured quality(#486/#514)와 24h window 집계를 `service/quality_api.py` 의 `QualityApiService` 로 옮기고 `BuilderService` 는 얇은 위임으로 남긴다. 24h 집계의 run 집합은 `DatasetsApiService` 의 canonical record 수집을 **재사용**한다 — 앞 조각에서 그 헬퍼를 public 으로 둔 이유다. `monitoring_summary` 는 여기 들어오지 않는다: 도메인 quality 와 시스템 observability 를 한 응답에 섞지 않는다는 경계를 유지한다. wire 계약 변화 없음
- **dataset 도메인 서비스 분리 (#596 네 번째 조각)**: built dataset 조회 표면(`/datasets`, `/datasets/{id}`, `/runs`, quality 이력)을 `service/datasets_api.py` 의 `DatasetsApiService` 로 옮기고 `BuilderService` 는 얇은 위임으로 남긴다. run record 수집 헬퍼는 quality 도메인이 함께 쓰므로 **public 으로 노출**해 다음 조각이 복제 대신 의존할 수 있게 했다. ownership 필터를 grouping/latest 선정보다 먼저 적용하는 규칙(#488 semantics D)은 파일 docstring 에 명시했다. wire 계약 변화 없음
- **query 도메인 서비스 분리 (#596 세 번째 조각)**: `POST /query` 를 `service/query_service_api.py` 의 `QueryApiService` 로 옮기고 `BuilderService` 는 얇은 위임으로 남긴다. 요청 본문 파서도 함께 옮겨, 권한·아티팩트 부재·문맥 오류·안전하지 않은 SQL·혼잡·타임아웃·실행 실패가 각각 어떤 상태 코드와 `code` 가 되는지 한 곳에서 읽힌다. 모듈명이 `query_service_api` 인 이유는 `kpubdata_builder.query.service` 에 이미 실행 엔진 쪽 `QueryService` 가 있어서다. wire 계약 변화 없음
- **upload 도메인 서비스 분리 (#596 두 번째 조각)**: `create_upload`/`get_upload`/`delete_upload` 를 `service/uploads_service.py` 의 `UploadsService` 로 옮기고 `BuilderService` 는 얇은 위임으로 남긴다. 저장소를 객체가 아니라 **호출 가능한 provider**(람다)로 넘겨, upload 를 쓰지 않는 워크스페이스에 `.service/uploads.sqlite3` 가 생기지 않는 지연 생성(#498)을 그대로 보존한다. wire 계약 변화 없음
- **provider 도메인 서비스 분리 (#596 첫 조각)**: `BuilderService` 가 providers/uploads/query/builds/datasets/quality 를 한 클래스에 들고 있던 구조를 도메인별로 나누기 시작한다. provider 목록·연결 테스트·credential CRUD 를 `service/providers_service.py` 의 `ProvidersService` 로 옮기고, `BuilderService` 의 해당 메서드는 얇은 위임으로 남긴다. 새 서비스는 **자기 의존성만** 받는다(credential resolver·client 팩토리·provider test 설정) — `BuilderService` 를 통째로 주입받으면 클래스만 늘고 결합은 그대로다. wire 계약(상태 코드·본문 키·라우팅·인증 게이트)은 바뀌지 않는다
- **CUBRID CI 가 실제로 CUBRID 를 검증하도록 고정 (#587)**: 전용 잡의 engine fixture 는 `KPUBDATA_BUILDER_CUBRID_URL` 이 없으면 조용히 in-memory SQLite 로 내려앉는다 — URL 주입이 빠지면 **CUBRID dialect 를 한 줄도 건드리지 않은 채 잡이 초록으로 통과**했다. `KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID=1`(잡이 설정) 이면 폴백을 금지하고, dialect/driver 가 `cubrid`/`pycubrid` 인지 단언하는 테스트를 추가했다. 잡에 누락돼 있던 `KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid` 도 주입하고, 기동 전 설정 검증(fail-closed) 테스트와 `cubrid` 마커 설명의 ADR 번호 오기(0013 → 0016)를 함께 고쳤다
- **패키지 버전을 CHANGELOG 라인에 맞춤 (#592)**: `pyproject.toml` 의 `version` 을 `0.1.0` → `0.4.0.dev0` 으로 올려 이 문서의 v0.4 절과 일치시킨다. `kpubdata_builder.__version__` 은 하드코딩 문자열을 버리고 설치된 배포판 메타데이터에서 파생하므로 버전 정본은 `pyproject.toml` 한 곳뿐이다 — 그동안 GHCR 이미지 태그·`--version` 출력·manifest 의 `builder_version` 이 모두 0.1.0 을 주장하고 있었다. `tests/unit/test_version.py` 가 세 값의 재이탈을 막는다
- API 계약 1.21.0 → 1.22.0 (`GET /datasets`에 `total` 추가, `GET /quality/summary` 추가, additive, #488/#486 후속)
- README에 Provider credential store(`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`) 운영 절 추가 — master key 필수/재사용/rotation 시 기존 credential 복호화 불가, 503(store 미구성)과 `configured:false`(미등록) 구분, secret 비노출 원칙
- API 계약 1.0.0 → 1.2.0 (/healthz + bearerAuth + /catalog + StructuredProblem)
- API 계약 1.4.0 → 1.5.0 (Dataset Catalog·Detail·Stage Summary API 추가, additive, #488)
- BuildIndex 스키마 v2 → v3 (created_by)
- BuildIndex 스키마 v3 → v4 (dataset_id 파생 검색 컬럼, #488)
- BuildManifest에 created_by 필드
- ValidationError에 structured_problems 추가
- README 인증 서술 fail-closed 정책에 맞게 수정 (#423)

### 수정됨
- **라이브 퍼블리시가 `--no-sources` 누락으로 항상 실패하던 것 (#625)**: `publish-dataset.yml` 의 마지막 스텝만 `uv run` 을 맨손으로 불렀다. `uv run` 은 실행 전에 환경을 다시 해석하므로 바로 위 `uv sync --no-sources` 가 무시한 `[tool.uv.sources]` 의 editable `../kpubdata` 오버라이드가 그 줄에서 되살아나고, 러너에 형제 디렉터리가 없어 `Distribution not found` 로 죽는다. 시크릿이 없으면 그 앞 가드가 `exit 0` 으로 빠지기 때문에 **시크릿을 넣는 순간부터** 드러나는 결함이었다. `[tool.uv.sources]` 는 그대로 둔다 — CONTRIBUTING.md 가 로컬 개발 메커니즘으로 문서화했고 `cross-repo-contract.yml` 이 그 위에 선다. 함께, `kpubdata` 핀을 `>=0.5.0,<0.6` 으로 적어 둔 `CONTRIBUTING.md` 3곳과 `cubrid.yml` 주석을 실제 `pyproject.toml` 핀(`>=0.6.0,<0.7`)에 맞췄다. ADR 0007 의 같은 문자열은 그 시점의 결정 기록이므로 고치지 않는다
- **`uv.lock` 드리프트를 잡는 CI 가드 (#625)**: `uv.lock` 은 `--no-sources` 해상도(CI·배포가 설치하는 것)를 기록하는데, CONTRIBUTING 이 안내하는 로컬 명령 `uv sync --extra dev` 는 sources 를 켜고 돌아 lock 의 `kpubdata` 를 `registry` → `editable "../kpubdata"` 로 뒤집고 배포 해시를 지운다. 그 상태가 커밋되면 CI 가 설치하는 것과 lock 이 서술하는 것이 갈리는데 이를 잡는 검사가 없었다. lint 잡에 `uv lock --check --no-sources` 를 추가하고, 로컬에서 더럽혀진 lock 을 커밋하지 않는 방법을 CONTRIBUTING 에 적었다
- `stages/_path_safety.ensure_within`이 Windows에서 여러 source를 병렬(ThreadPoolExecutor)로 빌드할 때 간헐적으로 traversal 오탐하던 버그 수정 — `root`/`target` 중 한쪽만 `Path.resolve()`의 `\\?\` 확장 프리픽스를 얻는 비대칭이 원인 (#506 조사 중 발견, composition과 무관한 기존 버그)

### 제거됨
- **루트에 커밋돼 있던 coverage 데이터와 개인 에이전트 설정 (#627)**: `.coverage.devbox.pid2864159.*`(coverage.py 병렬 모드가 남긴 80 KiB SQLite, #600 에서 유입)와 `.claude/settings.local.json`(다른 기여자의 절대경로 포함)을 추적 해제했다. `.gitignore` 에는 `.coverage` 만 있어 병렬 모드가 붙이는 `.<host>.<pid>.<rand>` 접미사를 잡지 못했으므로 `.coverage.*` 를 추가했다. `.claude/` 전체가 아니라 `settings.local.json` 만 무시한다 — 공유 설정과 스킬은 계속 추적할 수 있어야 한다
- .omc/state/sessions 추적 해제 (#380)
- PLAN.md를 .github/로 이동 (#425)

## v0.3

Plugin 생태계와 고급 빌드 기능.

- Plugin exporter API — register_exporter_factory/instance (#310, ADR 0004)
- Exporter / Publisher 경계 분리 (#28)
- Split 지원 (train/validation/test, by key)
- Kaggle dataset export
- Snapshot-aware builds (#15)
- Build diff/compare tools (#16)
- Reusable build templates (#14)

## v0.2

Export 확장, Dataset Identity, CLI build 실행.

- Markdown / JSONL / Parquet / HuggingFace layout exporter
- stage-aware exporters (Gold 기반)
- Publish command (#10)
- Manifest를 dataset release record로 승격 (#7)
- Schema summary in manifest (#11)
- Provenance tracking (#12)
- Dataset card 생성
- Build / Validate / Preview CLI command (#1-4)
- Polars 기반 tabular engine
- 서울 아파트 실거래가 end-to-end 예제

## v0.1

Medallion 파이프라인 기반 구축.

- BuildSpec 계약 안정화 (YAML 파싱, 검증)
- Medallion 디렉터리 구조 (stages/bronze, stages/silver, stages/gold)
- Bronze/Silver/Gold stage 구현
- Pipeline orchestrator
- BuildError 에러 계층
- manifest 스키마 안정화

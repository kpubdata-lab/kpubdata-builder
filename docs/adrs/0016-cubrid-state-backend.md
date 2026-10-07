# ADR 0016 — CUBRID 상태 백엔드 + manifest 정본 이전

- 상태: 수용됨(Accepted)
- 관련 이슈/문서: [ADR 0003](./0003-persistent-build-store.md)(영속 Build 저장소), [ADR 0010](./0010-artifactstore-state-backend.md)(ArtifactStore·백엔드 분리 — 이 ADR 을 쓸 때는 제안됨, 지금은 승인됨·구현 완료), [ADR 0012](./0012-provider-credential-boundary.md)(credential 경계)
- 드라이버: `sqlalchemy-cubrid[pycubrid]` (v1.7.1+, SQLAlchemy `>=2.0,<2.3`, 순수 파이썬 `pycubrid` 1.7.1, Python 3.10+)
  - `[pycubrid]` extra 가 필수다. `sqlalchemy-cubrid` 만 설치하면 dialect 만 생기고 드라이버는 없다.
  - **URL 은 반드시 `cubrid+pycubrid://`** 다. sqlalchemy-cubrid 는 dialect 를 `cubrid`,
    `cubrid.cubrid`, `cubrid.cubriddb`, `cubrid.pycubrid` 넷으로 등록하는데 앞의 셋은 모두
    legacy C-extension(`CUBRID-Python`, import 이름 `CUBRIDdb`)을 쓴다. 드라이버를 생략한
    `cubrid://` 는 연결 시점에 `ImportError: Could not import CUBRIDdb` 로 죽으므로,
    `store/backend.py` 가 기동 시 정규화·검증한다.

## 맥락

조직 요구사항으로 Builder 의 **영속 상태 백엔드를 CUBRID(한국형 RDBMS)로** 전환한다.
스케일링이 목적이 아니다 — 배포는 **OCI Compute VM + Docker 단일 인스턴스**(단일 replica)이며,
CUBRID 사용 자체가 요구사항이다. ADR 0010 이 제안한 백엔드 추상화를 본 ADR 이 실제 구현으로
확정한다.

기존 상태는 세 곳으로 나뉜다:

1. **BuildIndex**(ADR 0003) — 완료 이력 파생 캐시. 단일 파일 SQLite(`_builds.sqlite`).
2. **Credential 저장소**(ADR 0012) — 암호화 ciphertext. SQLite.
3. **산출물 + manifest.json** — 로컬 파일시스템. ADR 0003 은 `manifest.json` 을 정본으로 규정한다.

## 결정

### 1. 백엔드 선택 (기본 무외부의존)

`KPUBDATA_BUILDER_STORAGE_BACKEND` = `sqlite`(기본) | `cubrid`, `KPUBDATA_BUILDER_CUBRID_URL`
= SQLAlchemy URL. 기본값 sqlite/local 은 SQLAlchemy 의존이 없다(AGENTS.md 무외부의존·결정성).
`sqlalchemy` import 는 `store/backend.py` 의 cubrid 분기 안에서만 이뤄진다. cubrid 컴포넌트는
**프로세스 전역 단일 Engine**(커넥션 풀 + `pool_pre_ping`)을 공유하고, 연산마다 짧은 커넥션을
빌린다(멀티스레드 async job 대비). serve 시작 시 `validate_storage_config()` 로 cubrid URL·드라이버를
fail-closed 검증한다.

### 2. BuildIndex / Credential — Protocol 뒤의 CUBRID 구현체

- `BuildIndex` 를 Protocol 로 승격, 기존 concrete 를 `SqliteBuildIndex` 로 리네임(ADR 0010 권고),
  `CubridBuildIndex`(SQLAlchemy Core) 추가. `make_build_index()` 가 백엔드로 선택.
- `CredentialRepository`(ADR 0012)는 이미 Protocol — `CubridCredentialRepository` 추가.
- upsert 는 dialect 독립적으로 **단일 트랜잭션 내 delete+insert** 로 처리한다(CUBRID dialect 의
  MERGE/ON DUPLICATE 지원 여부에 의존하지 않음). BuildIndex 쓰기 예외는 ADR 0003 규칙4대로
  삼키고, credential 쓰기(사용자 액션)는 전파한다.

### 3. manifest 정본을 CUBRID 로 이전 (ADR 0003 supersede)

`ArtifactStore` Protocol(ADR 0010)을 도입한다. **핵심 설계 결정**:

- **산출물 바이트는 두 백엔드 모두 로컬 파일시스템(OCI 블록 볼륨)에 둔다.** 근거:
  (a) `query/engine.py` 가 별도 서브프로세스에서 `pl.scan_parquet(경로)` 로 lazy 스캔하므로
  실제 파일 경로가 필요하다; (b) 대용량 parquet/CSV 를 RDBMS BLOB 에 넣는 것은 안티패턴이며
  드라이버 성숙도 리스크가 크다; (c) 단일 replica 라 공유 오브젝트 스토어 이점이 없다.
- **manifest 문서만** 백엔드가 다르다. `CubridArtifactStore` 는 CUBRID `manifests` 행을 정본으로
  삼고 FS 를 미러(캐시)로 유지한다. 이는 ADR 0003 의 "manifest.json 이 정본" 을 cubrid 백엔드에
  한해 뒤집는다 → **본 ADR 이 해당 조항을 supersede** 한다. `LocalArtifactStore`(기본)는 FS 파일
  자체가 정본이라 기존 동작과 바이트 동일하다.

FS 미러가 항상 유지되므로 벌크 스캔(datasets/list_builds)·바이트 접근·쿼리는 계속 FS 에서
동작하고, 단일-run manifest 조회(`get_manifest`)만 CUBRID 정본을 우선(FS 폴백)한다.

## 마이그레이션 (FS → CUBRID)

1. `KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid` + `KPUBDATA_BUILDER_CUBRID_URL` 설정.
2. `kpubdata-builder rebuild-index` — FS `manifest.json` 스캔으로 CUBRID BuildIndex 재구축
   (백엔드 인지: cubrid 는 truncate+reinsert).
3. 기존 run 의 manifest 문서는 다음 빌드부터 `put_manifest` 로 CUBRID 에 승격된다. 기존 run 을
   즉시 CUBRID 정본으로 올리려면 각 run 의 `manifest.json` 을 `put_manifest` 로 재적재한다
   (FS 미러가 있으므로 `get_manifest` 는 그 전에도 FS 폴백으로 동작).
4. credential 은 master key + AAD 바인딩이라 자동 이관되지 않는다 — 재`put` 필요.

## 리스크

- **정본 이전**: manifest 정본이 CUBRID 로 이동(본 ADR). FS 미러로 하위 호환·벌크 스캔·백업을
  보존한다. `store/build_index.py`·`service/datasets.py` 의 "manifest.json 이 정본" 서술은
  local 백엔드 기준이며, cubrid 백엔드에서는 본 ADR 이 우선한다.
- **드라이버 성숙도**: `sqlalchemy-cubrid`/`pycubrid` 는 신생이다. upsert·LOB/CLOB·DDL 타입매핑·
  `LIMIT`·트랜잭션 동작을 통합 테스트(docker-compose CUBRID)로 검증한다. 실제 문제 발견 시
  `github.com/cubrid-lab/sqlalchemy-cubrid` 에 이슈를 낸다.
- **fail-closed/결정성**: cubrid 선택 시 serve 시작에서 조기 검증하되, 인덱스/manifest 승격 쓰기는
  best-effort(FS 정본/미러가 durable). 기본(sqlite/local) 경로는 결정적·무외부의존을 유지한다.

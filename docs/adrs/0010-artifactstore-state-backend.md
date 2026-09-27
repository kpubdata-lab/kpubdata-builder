# ADR 0010 — ArtifactStore 추상화 + BuildIndex 백엔드 분리

- 상태: 승인됨(Accepted) — 구현 완료. 백엔드 선택은 ADR 0016 이 확장

> `store/__init__.py` 의 `make_build_index()` 가 `KPUBDATA_BUILDER_STORAGE_BACKEND`
> 에 따라 백엔드를 고른다. CUBRID 백엔드는 ADR 0016 에서 더해졌다.
- 관련 이슈: #375, #334(ADR 0008), #309(ADR 0003)
- 관련 문서: [ADR 0003](./0003-persistent-build-store.md), ADR 0008(PR #381, 비동기 build job), [ARCHITECTURE.md](../ARCHITECTURE.md)

> 본 ADR은 **ADR 0008(비동기 build job)과 한 묶음으로 설계**한다 (#375). 비동기 job의 상태·산출물이 replica 간에 공유되려면 저장소가 로컬 파일시스템을 벗어나야 하므로, 두 관심사는 함께 결정되어야 한다.

## 결정 필요 사항 (이슈 #375)

1. 산출물(artifacts) 저장소 추상화 — `ArtifactStore` 인터페이스 도입 여부와 백엔드(local FS / Azure Blob / S3).
2. `BuildIndex`(ADR 0003, 완료 이력 SQLite) 백엔드 분리 — 단일 파일 SQLite vs 외부 DB.
3. 멀티 replica 전제 — `minReplicas: 1, maxReplicas: 1` 한계를 언제, 어떤 백엔드로 푸는가.

## 배경

현재 산출물은 로컬 파일시스템(`/data`, `output_root`)에 고정되어 있고, `BuildIndex`(ADR 0003)는 단일 파일 SQLite(`_builds.sqlite`)다. 이 구조는 **클라우드에서 replica를 2개 이상 띄울 수 없다**.

- 각 replica가 자기 `/data`만 보므로 `GET /builds`, `GET /artifacts/{run_id}` 응답이 replica마다 다르다.
- Azure Files 같은 네트워크 볼륨에 SQLite를 올리면 파일 잠금이 불안정하다(WAL 모드라도 NFS/파일 락 시맨틱에 의존).
- ACA scale-to-zero + 볼륨 미구성이면 산출물 유실.
- `POST /build`는 동기 실행(`service/app.py:185`)이고 소켓 타임아웃은 30초(`http.py:35`)라 긴 빌드가 ACA ingress idle timeout(기본 240초)에도 걸린다.

ADR 0008(비동기 job)이 cloud 배포의 선행 조건이지만, job 상태·산출물이 replica 간 공유되지 않으면 멀티 replica는 여전히 불가하다 → 본 ADR이 그 기반을 제공한다.

## 검토한 대안

### 1. ArtifactStore (산출물)

- **A. 로컬 파일시스템(현행)** — 단일 인스턴스 전용. 결정적·단순. replica 불가.
- **B. 추상화 + 다중 백엔드** — `ArtifactStore` 프로토콜(local / Azure Blob / S3). `local`은 현행과 동일(무외부의존 유지), `blob`/`s3`는 optional extra. 동일 인터페이스로 단일→멀티 replica 전환.

### 2. BuildIndex (완료 이력)

- **A. 단일 파일 SQLite(현행, ADR 0003)** — 무외부의존. 단일 인스턴스 전용.
- **B. 백엔드 분리** — 인터페이스 분리(`BuildIndex` 프로토콜), `sqlite` 구현체(현행) + `postgres` 구현체(optional). manifest 정본 원칙(ADR 0003)은 유지 — 인덱스는 어디서든 파생·재구축 가능.
- **C. ArtifactStore 기반 K/V** — Blob의 메타데이터에 인덱스를 두어 별도 DB 회피. 단순하지만 쿼리·페이지네이션이 빈약.

### 3. 동기 vs 비동기 상호작용

- 동기 `/build`(현행)는 요청 스레드에서 실행 → replica 1개 전제. ADR 0008의 비동기 `/builds`가 job 큐 + 워커 모델을 도입하면, 워커가 공유 `ArtifactStore`/`BuildIndex`에 쓰고 어떤 replica의 ingress든 읽을 수 있다.

## 권고 (제안)

본 ADR은 **제안됨** 상태로, 아래 방향을 기준선으로 제시한다. 백엔드 선택(어떤 클라우드/DB)은 배포 타깃 확정 후 소유자가 결정한다.

1. **`ArtifactStore` 프로토콜 도입** — `put(run_id, path, bytes)` / `get(run_id, path)` / `list(run_id)`. 기본 구현체 `LocalArtifactStore`(현행 동작, 무외부의존). `BlobArtifactStore`(azure-storage-blob) / `S3ArtifactStore`(boto3)는 optional extra로, 신뢰경계 검토 후.
2. **`BuildIndex` 인터페이스 분리** — ADR 0003의 SQLite 구현체를 `SqliteBuildIndex`로 리네임, 프로토콜 분리. manifest 정본 원칙·스키마 버전·rebuild 커맨드는 유지. 추가 구현체(Postgres 등)는 후속.
3. **`ArtifactStore` 주입** — `BuilderService`가 생성자에서 `ArtifactStore`를 받아, `output_root` 직접 접근을 전부 경유시킨다. 단계적 리팩터(기본은 `LocalArtifactStore(output_root)`).
4. **멀티 replica는 ADR 0008(async) 이후** — 본 ADR이 *인터페이스*를 제공하면, async job 도입과 함께 `Blob`/`Postgres` 백엔드로 전환. 그 전까지 `minReplicas: 1, maxReplicas: 1` (#378 Bicep 전제).
5. **Azure Files + SQLite 금지 명문화** — 네트워크 볼륨 위 SQLite는 잠금 불안정으로 배포 가이드(#390)에 명시.

> 근거: 인터페이스를 먼저 두면 단일→멀티 replica 전환이 점진적이고, `LocalArtifactStore`로 무외부의존·결정성(AGENTS.md 원칙)을 보존한다.

## 영향

- `BuilderService`·`serve_artifact_file`·`store/build_index.py`가 `ArtifactStore`/`BuildIndex` 프로토콜을 경유.
- `output_root` 타입이 `Path`에서 `ArtifactStore`로 (점진적 — 래퍼 도입 후 호출부 전환).
- #378(Bicep)은 본 ADR 인터페이스가 확정되기 전까지 `minReplicas: 1, maxReplicas: 1`.
- ADR 0008(async job) 워커가 공유 백엔드에 쓴다 — job 소유권(C1/#388)의 principal도 같은 백엔드에 기록.

## 미해결 질문

- 1차 클라우드 백엔드: Azure Blob(배포 타깃이 Azure) vs S3(범용) — #378 배포 타깃 확정 시 결정.
- 인덱스 DB: Postgres vs 인덱스 없이 Blob 메타데이터(K/V) — 빌드 수·쿼리 패턴에 따라.
- 동기 `/build`를 인터페이스 전환과 동시에 비동기(ADR 0008)로 넘길 것인가, 아니면 인터페이스 먼저?
- `LocalArtifactStore` 경로 안전(`_path_safety`)이 원격 백엔드에서도 동등하게 강제되는가(key 검증)?

# 배포 가이드

Builder HTTP 서비스를 로컬 개발 이상으로 운영하기 위한 배포·인증 스토리. 본 문서는 [ADR 0006](./adrs/0006-service-auth-and-deployment.md)(인증·배포), ADR 0009(사용자 인증, PR #398 — [ADR 0015](./adrs/0015-email-password-oidc-idp-keycloak.md)로 대체됨)의 운영 가이드를 통합한다.

> **상태**: ADR 0009는 제안됨(Proposed). 인증(Bearer) 구현은 B3(#385)/B4(#386) 진행 중이며, 본 문서의 Bearer 관련 절은 구현 완료 후 적용된다. 컨테이너 배포(fail-closed, HEALTHCHECK)는 이미 구현되었다.

## 1. 기본 배포 형태 — internal ingress

**Builder는 공개 인터넷 인그레스를 갖지 않는다** (ADR 0009 결정 3). Studio가 같은 네트워크/VPC에서 호출하는 구조가 기본형이다. 공격면을 최소화하고, 허용 목록을 심층 방어로 둔다.

- ACA(Azure Container Apps) / K8s에서 Builder 서비스를 클러스터 내부 서비스로 노출.
- Studio(정적 SPA)는 같은 네트워크에서 Builder를 호출. 외부 인터넷은 Studio 프론트만 접근.

## 2. 인증 — 두 경로 병행

| 소비자 | 인증 | 비고 |
| :--- | :--- | :--- |
| 스케줄 워크플로(데이터 갱신) | `X-API-Key` | Google 로그인 불가 → 서비스 키 병행 유지 |
| Studio(사람 사용자) | `Authorization: Bearer <OIDC access token>` — IdP 가 이 API 용으로 발급한 access 토큰(kpubdata-studio#722). audience 에 `OIDC_AUDIENCE` 가 있어야 한다 | ADR 0009, ADR 0015, ADR 0006의 "다중 소비자" 후속 |
| 로컬 개발 | `KPUBDATA_BUILDER_DEV_MODE=1` | 컨테이너 외부에서만 (fail-closed) |

두 경로 모두 `Principal`(`service`/`oidc`/`dev`)로 정규화된다 (B2/#384).

## 3. Google OAuth client 설정

> **전환 안내(ADR 0015)**: 사람 사용자 인증 IdP는 self-hosted Keycloak(email/password-capable OIDC)로 확정되었고 본 절의 Google 직접 audience 구성은 대체되었다. IdP 전환 절차·설정 분리는 [ADR 0015](./adrs/0015-email-password-oidc-idp-keycloak.md)를 따른다. 아래는 ADR 0009 시대의 기록으로 남긴다.

1. Google Cloud Console → APIs & Services → Credentials → **OAuth client ID**.
2. **Web application** 타입으로 생성.
3. **Authorized JavaScript origins**에 Studio 오리진 등록:
   - 로컬: `http://localhost:5173`
   - 실배포: `https://<studio-host>`
4. Client ID는 `VITE_GOOGLE_CLIENT_ID`로 Studio 빌드에 주입. **Client ID는 public 값**이라 번들 포함 무방.

> **절대 금지**: Builder API 키(`KPUBDATA_BUILDER_API_KEY`)를 `VITE_*` 환경변수로 Studio에 주입하지 말 것. `VITE_*`는 빌드 타임에 번들에 **평문으로 박힌다**. Studio는 토큰을 발급받지 않고(서버가 없으므로) Builder가 직접 Bearer를 검증한다 (ADR 0009).

## 4. 오리진 정합 — Google Console ↔ Builder CORS

**같은 오리진 목록**을 양쪽에 등록해야 한다:

- **Google Console**: Authorized JavaScript origins (§3)
- **Builder**: `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 환경변수 (CORS default-deny, `service/http.py`)

두 값이 어긋나면 증상이 **CORS 오류**로 나타나 원인 추적이 어렵다. 로컬과 실배포 오리진을 모두 양쪽에 등록할 것.

> GitHub Pages 데모(`https://kpubdata-lab.github.io`)는 mock 모드(`VITE_USE_REAL_BUILDER` 미설정)라 Builder를 호출하지 않으므로 등록 대상이 아니다.

## 5. SPA 토큰 보관

- Studio는 토큰을 **메모리(zustand 스토어, persist 미들웨어 없음)**에만 보관한다.
- `localStorage`/`sessionStorage`에 토큰을 쓰지 않는다 — XSS 한 번이면 탈취된다.
- 새로고침 시 재로그인(GIS 자동 로그인으로 마찰 완화).

## 6. 상태 저장 제약 — 단일 replica 전제

Builder 는 **프로세스 하나**로 돈다. replica 를 2개 이상 띄울 수 없다. 배포 시:

- `minReplicas: 1, maxReplicas: 1` 고정.
- `/data` 는 **로컬 블록 볼륨**으로 마운트한다 — 산출물·인덱스·SQLite 저장소가 여기에 있다.
  compose 는 named volume `builder-data` 를 쓴다.
- **네트워크 파일시스템(NFS, Azure Files) 위에 SQLite를 올리지 말 것** — 파일 잠금이
  불안정하다 (ADR 0010 §5). 예전 이 절은 `/data` 에 Azure Files 를 마운트하라고 하면서
  같은 곳에서 그 위에 SQLite 를 올리지 말라고 했다. 두 문장은 함께 지킬 수 없다 —
  인프라 쪽 정리는 #1097 이다.

replica 를 늘릴 수 없는 이유는 저장소만이 아니다. ADR 0010 의 백엔드 분리(`ArtifactStore`,
`make_build_index()`)는 구현되어 있고 CUBRID 백엔드(ADR 0016)도 있지만, 아래는 여전히
**프로세스 메모리**에 있다(2026-10-07):

| 프로세스 안에만 있는 것 | 둘 이상이면 |
|---|---|
| 비동기 job 레지스트리와 큐 (`service/jobs.py`) | 다른 replica 에 제출된 job 의 상태를 답하지 못한다 |
| job 에 묶인 provider 키 (`JobCredentials`, #683) | 키를 받은 replica 만 그 job 을 돌릴 수 있다 |
| 동시에 도는 build 의 자리 수 (`BuildSlots`) 와 preview 상한 | 상한이 replica 수만큼 곱해진다 |
| 인증 실패 스로틀, probe 간격 | 클라이언트가 replica 를 바꿔 가며 한도를 피한다 |

그래서 재시작하면 대기·실행 중이던 job 은 이어지지 않는다. 종료 신호를 받았을 때의 처리는
§7, 다중 사용자 배포에서 중단된 run 의 표시는 #683 을 본다.

### 6.0 이전 릴리스로 되돌렸을 때 (#1096)

상태 저장소는 자기 스키마의 버전을 적어 둔다. 실행 중인 릴리스가 아는 것보다 **새로운 버전**의
저장소를 만나면 그 저장소를 열지 않고 건드리지도 않는다. 셋 모두 기동할 때 확인하므로, 서버가
`error:` 한 줄을 남기고 뜨지 않는다(종료 코드 1). 아직 없는 저장소는 지금처럼 처음 쓰일 때 만든다.

| 저장소 | 새 버전을 만나면 | 할 수 있는 일 |
|---|---|---|
| 빌드 인덱스 (`_builds.sqlite`, 또는 CUBRID 의 `builds`) | 기동 거부. 예전에는 표를 지우고 다시 만들어서, 새 릴리스로 돌아가면 인덱스가 비어 있었다 | 그 저장소를 쓴 릴리스를 띄우거나, `kpubdata-builder rebuild-index` 로 이 릴리스의 인덱스를 manifest 에서 다시 만든다 — 인덱스는 파생물이다 |
| run 이벤트 (`_build_events.sqlite`) | 기동 거부. 예전에는 버전을 보지 않고 열었다 | 그 저장소를 쓴 릴리스를 띄우거나, 업그레이드 전 백업에서 출력 디렉터리를 복구한다 — 이벤트는 다른 것에서 다시 만들 수 없다 |
| 테이블 카탈로그 (웨어하우스의 `_warehouse.sqlite`) | 기동 거부. 예전에는 처음 쓰일 때 열려서, 기동이 아니라 그 요청이 실패했다 | 위와 같다 — 카탈로그도 다시 만들 수 없다 |

**오래된 버전**: 카탈로그는 버전 사슬을 따라 올린다. 올리기 직전에 카탈로그를 SQLite 로 복사해
옆에 둔다(`_warehouse.sqlite.v<이전 버전>.before-migration`). 마이그레이션은 한 트랜잭션이라 도중에
실패하면 이전 버전 그대로 남고, 다음 기동이 다시 시도한다. 사본은 지우지 않으며, 같은 버전의
사본이 이미 있으면 덮어쓰지 않는다. **사본은 카탈로그뿐이다** — 스냅샷 파일은 들어 있지 않으므로,
그 뒤에 쓰인 것이 있는 웨어하우스에 사본만 되돌려 놓으면 그만큼을 잃는다. 전체 복구는 `warehouse
backup` 으로 받은 백업으로 한다. 인덱스는 `serve` 가 요청을 받기 전에
manifest 에서 다시 만든다 — 인덱스 파일이 아예 없을 때도 같다. 예전에는 비운 채로 떠서
`rebuild-index` 를 따로 돌리기 전까지 이전 run 이 목록에 보이지 않았다. run 이 많으면 그만큼
기동이 늦어진다(run 마다 manifest 하나를 읽는다). 그 밖의 SQLite 저장소
(자격 증명, 업로드, 게시 영수증, 가입 원장 등)는 버전을 적지 않으며 이 표에 없다.

### 6.0.1 SQLite 상태 저장소 목록과 연결 방식 (#1096)

Builder 가 두는 SQLite 파일은 열 개다. 목록의 정본은 `src/kpubdata_builder/store/inventory.py` 이고,
`tests/unit/test_state_store_inventory.py` 가 그 목록을 코드(SQLite 를 여는 모듈, 각 모듈의 timeout·저널
모드·버전 관리)와 아래 표에 대조한다. 저장소를 더하거나 연결 방식을 바꾸면 그 파일과 이 표를 함께
고쳐야 테스트가 통과한다.

| 저장소 | 위치 | 잠금 대기 | 저널 모드 | 스키마 버전 | 잃으면 |
|---|---|---|---|---|---|
| 빌드 인덱스 | 출력 디렉터리의 `_builds.sqlite` | 30초 | WAL | 버전 표 | 잃어도 된다 — manifest 에서 다시 만든다(`serve` 가 기동할 때, 또는 `rebuild-index`) |
| run 이벤트와 제출 기록 | 출력 디렉터리의 `_build_events.sqlite` | 30초 | WAL | 버전 표 | run 의 타임라인과 제출자 기록을 잃는다. 다시 만들 수 없다 |
| 게시 영수증 | 출력 디렉터리의 `_publish_receipts.sqlite` | 30초 | WAL | 없음 — 빠진 열을 열 때 더한다 | 어떤 run 을 어디에 게시했는지와, 같은 게시가 두 번 나가는 것을 막는 근거를 잃는다. 다시 만들 수 없다 |
| provider 자격 증명 (암호화) | 출력 디렉터리의 `.service/provider-credentials.sqlite3` | 5초 | 기본(rollback journal) | 없음 | 사용자가 저장한 provider 키를 잃는다. 각자 다시 입력해야 한다 |
| 업로드 | 출력 디렉터리의 `.service/uploads.sqlite3` | 5초 | 기본(rollback journal) | 없음 — 빠진 열을 열 때 더한다 | 올린 파일과 그 목록을 잃는다(큰 파일의 내용은 옆의 `uploads.sqlite3.blobs/` 에 있다) |
| provider 연결 테스트의 마지막 결과 | 출력 디렉터리의 `.service/provider_tests.sqlite3` | 30초 | 기본(rollback journal) | 없음 | 잃어도 된다 — 연결 테스트를 다시 하면 채워진다 |
| 문서 revision 과 감사 기록 | 출력 디렉터리의 `.service/revisions.sqlite3` | 30초 | 기본(rollback journal) | 없음 | BuildSpec 과 표시 주석의 저장 이력을 잃는다. 다시 만들 수 없다 |
| 가입 원장 | 출력 디렉터리의 `.service/users.sqlite3` | 30초 | 기본(rollback journal) | 없음 | 가입 승인·거절 기록을 잃는다. allowlist 에 없는 사용자는 다시 승인을 기다린다 |
| 저장한 분석 | 출력 디렉터리의 `.service/analyses.sqlite3` | 30초 | 기본(rollback journal) | 없음 — 빠진 열을 열 때 더한다 | 저장한 SQL 과 그것이 읽은 스냅샷의 기록을 잃는다. 다시 만들 수 없다 |
| 테이블 카탈로그 | 웨어하우스의 `_warehouse.sqlite` | 30초 | WAL | 버전 표 | 어떤 스냅샷이 어느 테이블의 현재 것인지를 잃는다. 다시 만들 수 없다 |

CUBRID 백엔드(§6.1)를 쓰면 빌드 인덱스와 provider 자격 증명은 CUBRID 에 있고 위 파일을 쓰지 않는다.

**지금의 방식.** 이 표는 정해 둔 규칙이 아니라 지금 코드가 하는 일이다. 저장소마다 다른 점은 다음과 같다.

- **잠금 대기 30초가 대부분이고, 둘만 5초다**(provider 자격 증명, 업로드). 5초인 이유는 코드에 적혀 있지
  않다. 30초로 맞추면 잠금이 걸렸을 때 요청이 실패하는 대신 더 오래 기다리게 되므로, 맞출지는 정해야 할
  일이다.
- **WAL 은 넷이다**(빌드 인덱스, run 이벤트, 게시 영수증, 테이블 카탈로그). 나머지는 SQLite 기본 모드다.
- **가입 원장은 일부러 WAL 이 아니다.** WAL 데이터베이스는 쓸 수 없는 디렉터리에서 읽지 못한다(연결마다
  `-shm` 파일을 만들어야 한다). 원장은 디스크가 읽기 전용이 되어도 이미 가입한 사용자를 들여보내야 해서
  기본 모드로 둔다(#1121). 그래서 "전부 WAL" 은 답이 아니다.
- **읽기 전용 연결과 백업 사본은 따로다.** 저장소의 버전만 읽는 연결(`store/schema_version.py`)은 저널
  모드를 정하지 않고, 카탈로그의 사본(`warehouse/backup.py`, 마이그레이션 전 사본)은 기본 모드로 돌려 둔다.
- **버전 표가 있는 것은 셋이다.** 이 셋은 더 새로운 릴리스가 쓴 저장소를 열지 않는다(§6.0). 나머지 일곱은
  버전을 적지 않으므로 그런 확인이 없다 — 그중 셋은 빠진 열을 열 때 더하는 방식으로만 바뀌어 왔다.

### 6.1 CUBRID 상태 백엔드 (ADR 0016)

기본 백엔드는 sqlite/local(무외부의존)이다. 조직 요구로 CUBRID 를 쓰려면
(VM 한 대 + Docker 단일 인스턴스 전제):

```bash
# 이미지에 CUBRID extra 포함
docker build --build-arg EXTRAS="publish cubrid" -t kpubdata-builder:cubrid .

# 실행 (env 로 백엔드 선택)
docker run --rm -p 8000:8000 \
  -e KPUBDATA_BUILDER_API_KEY="$API_KEY" \
  -e KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid \
  -e KPUBDATA_BUILDER_CUBRID_URL="cubrid+pycubrid://user:pass@cubrid-host:33000/kpubdata?charset=utf8" \
  # URL scheme 은 `cubrid+pycubrid://` 여야 한다 — 드라이버를 생략한 `cubrid://` 는
  # legacy C-extension(CUBRIDdb) dialect 로 해석된다. 생략하면 기동 시 경고와 함께
  # pycubrid 로 정규화하고, `cubrid+cubriddb://` 처럼 명시하면 기동을 거부한다(ADR 0016).
  -v /mnt/blockvol/data:/data \
  kpubdata-builder:cubrid
```

- **BuildIndex·Credential·manifest 문서**가 CUBRID 에 저장된다. **산출물 바이트는 여전히
  `/data`(블록 볼륨)** 에 둔다 — 쿼리 엔진이 실제 parquet 경로를 요구하고 대용량 BLOB 을
  RDBMS 에 넣지 않기 위함(ADR 0016). 따라서 `/data` 볼륨 마운트는 CUBRID 백엔드에서도 필수다.
- serve 시작 시 `KPUBDATA_BUILDER_CUBRID_URL` 미설정·드라이버 미설치면 fail-closed 로 기동을 거부한다.
- 마이그레이션(FS→CUBRID)·정본 이전·리스크는 [ADR 0016](./adrs/0016-cubrid-state-backend.md) 참조.
- CUBRID 는 같은 VM 에 컨테이너로 함께 띄운다(docker-compose, `infra/cubrid/` 참조).

## 7. 헬스체크·종료

- `GET /healthz` — 무인증 liveness probe (#372). 프로브가 API 키를 못 실을 때 사용.
- `Dockerfile` `HEALTHCHECK` — urllib로 `/healthz` 폴링 (#372).
- `SIGTERM` — 우아운 종료(진행 중 요청 drain, #374). ACA/K8s 롤링 업데이트 대응.
  비동기 빌드는 이렇게 끝난다(#1118). 대기 중인 작업은 시작하지 않고 `failed`
  (`interrupted: …`)로 끝나며 키를 버린다 — 가져오거나 쓴 것이 없으므로 새 `run_id` 로 다시
  제출하면 된다. 그 뒤의 제출은 503 `shutting_down` 이다. 실행 중인 빌드는
  `KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS`(기본 90초) 동안 끝나기를 기다리고, 그때까지 남은
  빌드에는 사용자의 취소와 같은 방식으로 멈추라고 한 뒤 10초를 더 준다(다음 단계 경계에서
  `cancelled` 로 끝나고 이벤트가 이유를 적는다). **컨테이너의 종료 대기 시간은 그 합보다 길게**
  둔다 — compose 의 `stop_grace_period: 120s`. Docker 의 기본값 10초로는 실행 중인 빌드가
  SIGKILL 로 끊긴다. 그렇게 끊긴 run 은 manifest 없이 남고, 다중 사용자 배포는 다음 기동 때
  `credentials_required` 로 표시한다(#683).

## 8. 동시성·풀·백프레셔

단일 Builder process 안에는 역할이 다른 실행 제한이 세 개 있다. 숫자가 같더라도 하나의
공유 pool이 아니며 서로 대신하지 않는다.

| 계층 | 구현 | 기본값 | 포화 시 동작 |
| :--- | :--- | :--- | :--- |
| HTTP 요청 | 별도 `ThreadPoolExecutor` (`kpubdata-http`) | service 기본 10, ACA template 4 | executor의 무제한 내부 queue에서 대기. 명시적 `429`가 아니므로 앞단 ingress timeout/connection limit가 필요 |
| 비동기 build | 별도 in-process `ThreadPoolExecutor` (`kpubdata-build`) | CLI가 HTTP와 같은 worker 설정 사용(service 기본 10, ACA 4), queued job 10 | queued 상태가 10건이면 제출을 `429`로 거절. 실행 중 job은 queue 수에 포함하지 않음 |
| query | `BoundedSemaphore` + 요청마다 `spawn` child process | service 기본 2, ACA template 1 | permit을 기다리지 않고 즉시 `429 query_busy`; 성공·실패 후 permit 반환 |

`POST /build`는 동기식이므로 전체 pipeline이 끝날 때까지 HTTP worker 하나를 점유한다.
`POST /query`도 child process의 결과 또는 timeout을 기다리는 동안 HTTP worker 하나를
점유한다. 반면 비동기 `POST /builds`의 HTTP worker 점유는 queue 제출까지로 짧고, 실제
build는 별도 build pool에서 실행된다. 따라서 query semaphore가 남아 있어도 모든 HTTP
worker가 동기 build/query에 묶이면 새 요청은 HTTP executor queue에서 기다린다.

HTTP executor queue에는 길이 제한이 없다. 외부 ingress에서 요청 수, body 크기, idle/request
timeout을 제한하고, 장시간 동기 호출의 클라이언트 timeout을 서버 query timeout보다 길게
잡는다. 비동기 build queue의 검사는 process-local이므로 단일 replica 전제와 결합되며,
재시작하면 대기/실행 상태를 복구하지 못한다.

ADR 0008은 여전히 **제안됨(Proposed)** 상태다. 현재 비동기 build pool과 active registry는
그 ADR의 일부 방향만 선행 구현한 것이며, 취소, persistent queue, crash recovery, partial
manifest 정책까지 승인·완료됐다는 뜻이 아니다.

## 9. 리소스 예산과 튜닝

최소 메모리 예산은 다음 항목을 실측해 합산한다.

```text
memory >= base process
        + HTTP_workers * per_HTTP_thread
        + active_build_workers * per_build_working_set
        + query_concurrency * (per_query_child + 최대 8 MiB IPC payload)
        + filesystem/cache headroom
```

CPU 수요는 대략 `active_build_workers * build_CPU` +
`query_concurrency * query_child_CPU` + HTTP overhead다. query child 하나도 DuckDB 연결의
thread(`KPUBDATA_DUCKDB_THREADS`)를 여럿 쓸 수 있으므로 `query_concurrency`를 vCPU 수처럼 간주하면 안 된다. 작은 ACA
인스턴스는 `infra/main.bicep` 기본값인 1 vCPU/2 GiB, HTTP worker 4, async build worker 4,
query concurrency 1에서 시작한다. HTTP와 build는 같은 설정값을 받지만 서로 다른 pool이라
동시에 각각 4개까지 실행될 수 있다. 따라서 CPU/memory 중심 build를 많이 제출하는 환경에서는
이 기본 ACA 크기만으로 안전하다고 가정하지 말고 working set과 throttling을 관찰해야 한다.

한 호스트에 동시에 존재할 수 있는 실행 단위 전체 — 각각 따로 설정되므로 **합**을 호스트와
대조해야 한다(#701):

| 실행 단위 | 상한 | 설정 |
| :--- | :--- | :--- |
| HTTP worker thread | 기본 10 | `KPUBDATA_BUILDER_MAX_WORKERS` / `serve --max-workers` |
| 동시 build (동기 `POST /build` + 비동기 job **합계**) | 기본은 HTTP worker 수, queued 10 | `KPUBDATA_BUILDER_MAX_BUILDS` / `serve --max-builds` (#1028) |
| 동시 preview | 기본 무제한(요청 스레드가 상한) | `KPUBDATA_BUILDER_MAX_PREVIEWS` / `serve --max-previews` (#1028) |
| build 하나 안의 source fetch thread | **build 당** 최대 4 (`_MAX_PARALLEL_SOURCES`) | 코드 상수 |
| query child process | 기본 2 | `KPUBDATA_QUERY_MAX_CONCURRENCY` |
| query child 하나의 메모리 | **기본 무제한**. 설정하면 child 의 address space 를 제한해 초과한 질의만 실패(`400 query_failed`)하고 서버와 다른 요청은 계속된다 | `KPUBDATA_QUERY_MAX_MEMORY_MB` |
| 동시에 도는 query child 들의 메모리 합 | **기본 없음**(개수 상한만). 설정하면 질의마다 자기 child 메모리 상한만큼 예산에서 예약하고, 예산이 모자라면 기다리지 않고 `429 query_busy` 로 거부한다. 성공·실패·timeout·취소 어느 경로로 끝나도 예약을 돌려준다. SQL·행 읽기·집계·내보내기·프로파일 모두 같은 예산을 쓴다 | `KPUBDATA_QUERY_MEMORY_BUDGET_MB` |
| DuckDB thread | **연결 하나당** 기본 2. build 는 실행 중인 source 마다 연결 하나 | `KPUBDATA_DUCKDB_THREADS` |
| DuckDB buffer memory | **연결 하나당** 기본 `1GB`. 넘으면 spill 한다 | `KPUBDATA_DUCKDB_MEMORY_LIMIT` |
| DuckDB spill(임시 디스크) | **연결 하나당** 기본 `10GB`. 넘으면 그 질의·source 만 실패하고 디스크를 채우지 않는다 | `KPUBDATA_DUCKDB_MAX_TEMP_SIZE` |

Builder 는 Polars 를 쓰지 않으므로(#876) `POLARS_MAX_THREADS` 는 더 이상 효과가 없다.
query child 의 spill 디렉터리는 parent 가 질의마다 만들어 `KPUBDATA_QUERY_TEMP_DIR` 로 child 에
넘기고, child 가 어떻게 끝나든 parent 가 지운다 — 운영자가 설정하는 값이 아니다.

`KPUBDATA_QUERY_MAX_MEMORY_MB` 는 address space 상한(`RLIMIT_AS`)이라 RSS 보다 크게 잡아야
한다 — child 의 interpreter 와 DuckDB 가 import 시점에 가상 메모리를 예약하므로 너무 작으면
모든 질의가 실패한다. admission 기준은 **메모리**다(#701, 소유자 결정 D3): `KPUBDATA_QUERY_MEMORY_BUDGET_MB`
를 두면 질의 하나가 `KPUBDATA_QUERY_MAX_MEMORY_MB` 만큼 예약한다. 질의별 상한 없이 예산만
두면 질의 하나가 예산 전체를 예약하므로 한 번에 하나씩 돈다 — 둘을 함께 설정하는 것이 맞다.
CPU·임시 디스크·프로세스·스레드 수는 위 표의 문서화 항목이고 admission 기준이 아니다. 동시
실행 개수 상한(`KPUBDATA_QUERY_MAX_CONCURRENCY`)은 보조 상한으로 그대로 남는다.

DuckDB 의 세 설정은 **연결 하나당** 상한이다(ADR 0021 D9, #701). 한 호스트의 합은 동시에 열려 있는
연결 수를 곱해 구한다 — build 는 실행 중인 source 마다 연결 하나를 연다(build 하나에 최대 4,
`_MAX_PARALLEL_SOURCES`). spill quota 는 DuckDB 가 실제로 지키도록 연결을 연 뒤 `SET` 으로 건다: connect
config 로 넘기면 값은 보이지만 지켜지지 않았다(4 MB quota 에서 400 MB 넘게 썼다).

```text
DuckDB 연결 수   = KPUBDATA_BUILDER_MAX_BUILDS × 4 + KPUBDATA_BUILDER_MAX_PREVIEWS + composition 1
DuckDB thread    = DuckDB 연결 수 × KPUBDATA_DUCKDB_THREADS
DuckDB 메모리    = DuckDB 연결 수 × KPUBDATA_DUCKDB_MEMORY_LIMIT        (RSS 에 더한다)
임시 디스크      = DuckDB 연결 수 × KPUBDATA_DUCKDB_MAX_TEMP_SIZE       (run 디렉터리의 _duckdb_tmp)
query 메모리     = KPUBDATA_QUERY_MEMORY_BUDGET_MB                      (질의마다 MAX_MEMORY_MB 예약)
프로세스 수      = 1 + KPUBDATA_QUERY_MAX_CONCURRENCY
```

**동시 build 수는 요청 스레드 수와 따로 정한다** (#1028). 예전에는 `KPUBDATA_BUILDER_MAX_WORKERS`
하나가 HTTP worker 와 비동기 build worker 를 함께 정했다. 그 값을 2 로 두면 동기 `POST /build` 가 요청
스레드에서 2개, 비동기 job 이 2개 — build 4개가 돌 수 있었고, 나머지 요청 전부가 스레드 2개를 나눠 썼다.
지금은 `KPUBDATA_BUILDER_MAX_BUILDS` 가 두 경로를 합친 상한이다: 한도에 닿으면 동기 build 는 자리를
`KPUBDATA_BUILDER_BUILD_WAIT_SECONDS`(기본 30초)까지 기다리고, 그래도 없으면 요청 스레드를 돌려주며 429
`build_queue_full` 로 답한다(#1040). 비동기 job 은 자리를 얻을 때까지 `queued` 로 남는다. `KPUBDATA_BUILDER_MAX_PREVIEWS` 를 주면 preview 도 그 수까지만
함께 돌고 나머지는 기다린다. `MAX_BUILDS` 를 주지 않으면 `MAX_WORKERS` 값을 따른다 — 그 값만 설정해 둔
배포의 비동기 worker 수는 그대로이고, 이제 그 수가 두 경로 합계의 상한이다.

**1 vCPU / 2 GiB 예시** (`infra/main.bicep` 기본 크기). 동시 build 를 둘로 묶고 나머지를 맞춘다.

| 항목 | 설정 | 합 |
| :--- | :--- | :--- |
| 동시 build | `KPUBDATA_BUILDER_MAX_BUILDS=2` | 2 (동기·비동기 합계) |
| DuckDB 연결 (build 2 × source 4 = 8) | `KPUBDATA_DUCKDB_THREADS=1`, `KPUBDATA_DUCKDB_MEMORY_LIMIT=96MB`, `KPUBDATA_DUCKDB_MAX_TEMP_SIZE=1GB` | thread 8, 메모리 768 MB, 임시 디스크 8 GB |
| query child | `KPUBDATA_QUERY_MAX_CONCURRENCY=1`, `KPUBDATA_QUERY_MAX_MEMORY_MB=768`, `KPUBDATA_QUERY_MEMORY_BUDGET_MB=768` | 프로세스 2, query 메모리 768 MB |
| 기본 프로세스·HTTP·여유 | 실측 | 약 400 MB |

메모리 합은 약 1.9 GB 다(DuckDB 768 MB + query 768 MB + 기본 약 400 MB). 2 GiB 에 여유가 거의 없으므로
실측 RSS 가 크면 DuckDB 메모리나 query 예산을 줄인다. DuckDB 메모리는 넘으면 spill 하므로 작게 잡아도
build 가 실패하지 않고 느려진다. thread 8 + query 1 은 1 vCPU 를 넘지만 thread 는 CPU 를 나눠 쓸 뿐
메모리를 늘리지 않는다. 임시 디스크 8 GB 는 컨테이너의 쓰기 가능한 디스크 안에 있어야 한다.

**운영 compose(`docker-compose.prod.app.yml`)의 기본값** (#993). 컨테이너 한도는 `3G` 이고 아래 합이
그 안에 들어온다. `tests/unit/test_prod_compose_budget.py` 가 compose 파일에서 같은 식을 계산해, 한도가
합보다 작아지면 실패한다.

| 항목 | 기본값 | 합 |
| :--- | :--- | :--- |
| DuckDB 연결 (build 2 × source 4 + preview 1 + composition 1 = 10) | `KPUBDATA_BUILDER_MAX_BUILDS=2`, `KPUBDATA_BUILDER_MAX_PREVIEWS=1`, `KPUBDATA_DUCKDB_MEMORY_LIMIT=128MB` | 1280 MB |
| query child | `KPUBDATA_QUERY_MAX_CONCURRENCY=1`, `KPUBDATA_QUERY_MAX_MEMORY_MB=768`, `KPUBDATA_QUERY_MEMORY_BUDGET_MB=768` | 768 MB |
| 기본 프로세스·HTTP·여유 | 검사가 쓰는 고정값 | 400 MB |
| **합** | | **2448 MB ≤ 3072 MB** |

`KPUBDATA_DUCKDB_MEMORY_LIMIT` 을 `64MB` 로 두면 20행짜리 테이블의 빌드도
`the table needs more memory or temporary disk than this deployment allows` 로 실패한다(parity
시나리오로 실측, `96MB` 부터 통과). 그래서 운영 기본값은 `128MB` 이고, 위 "1 vCPU / 2 GiB 예시"의
`96MB` 는 여유가 거의 없는 하한이다. 2 GiB 호스트에 맞추려면 `KPUBDATA_DUCKDB_MEMORY_LIMIT` 을 더
내리지 말고 `KPUBDATA_BUILDER_MAX_BUILDS` 나 query 예산을 줄인다. warehouse 는
`KPUBDATA_BUILDER_WAREHOUSE=/data/warehouse` 로 켜져 있다 — 없으면 `/warehouse/*` 가
`warehouse_not_configured` 로 답한다.

query worker 의 종료는 네 경로 모두에서 child process 를 남기지 않는다: 성공, 실패(child 가 스스로
끝나지 않아도 1초 뒤 terminate), timeout(terminate 를 무시하면 kill), 요청 취소(대기 중인 요청 thread 가
중단되면 child 도 멈춘다). 클라이언트가 연결을 끊은 것은 서버가 따로 감지하지 않는다 — 그 질의는 timeout
까지 돌고 같은 정리를 거친다. preview(`/query`)는 SQL 결과를 `limit + 1` 행에서 자르므로 전체 결과를 만들지
않고, 전체 결과 export 는 query 와 다른 timeout(`EXPORT_TIMEOUT_SECONDS`, 60초)으로 같은 메모리 예산을 쓴다.

query timing은 다음 경계를 사용한다.

- `execution_ms`: parent의 `QueryEngine.execute` 진입부터 payload 수신·검증과 child join까지.
- `startup_ms`: 같은 parent 시작점부터 spawned child의 import와 잠긴 DuckDB 연결(`dataset` view)
  준비 완료, 질의 직전까지.
- `engine_execution_ms`: child의 질의 직전부터 DuckDB 실행·결과 fetch 완료까지.
- 구조화 로그의 `ipc_serialization_ms`: 위 두 child 구간을 end-to-end에서 뺀 nonnegative
  remainder. row 변환, JSON 크기 확인, Pipe 직렬화/전송, scheduling, join과 ms 반올림을
  포함하므로 세 필드가 정확히 합산된다고 가정하지 않는다.

고정 성능 threshold를 CI에 두지 않는다. 실제 배포와 같은 CPU/memory에서 동일 parquet와
canonical SQL을 준비하고, cold query(새 child)와 warm filesystem-cache query를 각각 30회
실행한다. 첫 실행을 별도로 보존하고 세 timing의 p50/p95, RSS, CPU throttling, `429` 비율을
함께 기록한 뒤 concurrency를 한 단계씩 올린다. 데이터, image digest, ACA SKU, 반복 횟수를
결과와 함께 남겨야 비교 가능하다.

## 10. Process 격리 선택

query는 의도적으로 `spawn`을 사용한다. thread가 이미 실행 중인 service process를 `fork`하면
다른 thread가 잡은 lock, logging/runtime 상태, DuckDB native thread 상태를 child가
불완전하게 상속할 수 있다. startup이 짧아 보인다는 이유로 `fork`로 바꾸는 것은 안전한
대체가 아니다.

장기적으로 pre-spawn query worker pool을 두면 import startup과 process 생성 비용을 줄일 수
있지만, worker별 메모리가 상시 필요하고 손상된 native state/사용자 query 간 상태 격리,
timeout 시 worker 교체, queue 공정성, 배포 drain을 새로 설계해야 한다. 현재 per-query child는
startup 비용 대신 강한 취소·수명 격리를 선택한다. 비동기 build pool을 별도 process/service로
분리하면 HTTP process 장애와 GIL/메모리 경쟁을 줄이지만 외부 queue, 상태 영속성, credential
전달 신뢰경계와 운영 복잡도가 증가한다. 이 tradeoff는 ADR 0008 승인 과정에서 결정한다.

## 11. 컨테이너 이미지 취약점 스캔 게이트 (Trivy)

`docker.yml` 워크플로가 serve 이미지를 빌드해 Trivy로 스캔한다 (#376 도입, #552 정책 문서화).

**게이트 정책** (변경 시 이 문서와 함께 갱신할 것):

| 항목 | 값 | 근거 |
| :--- | :--- | :--- |
| 대상 severity | `CRITICAL,HIGH` | MEDIUM 이하는 배포를 막는 신호로 쓰지 않는다 |
| 실패 동작 | `exit-code: 1` (job 실패 → GHCR publish 차단) | 취약점이 있는 이미지가 배포되지 않게 fail-closed |
| `ignore-unfixed` | `true` | 업스트림 패치가 없는 finding은 PR 신호를 오염시킨다 |
| 예외 처리 | 원칙적으로 없음. 불가피한 경우 `.trivyignore`에 CVE·만료일·근거 주석 명시 | 무기한 예외 금지 |

**base image 취약점 대응 이력**:

- CVE-2026-53615 (Debian `util-linux` 계열 HIGH 9건, 2026-08): 베이스 이미지의
  `apt-get upgrade` 레이어를 Dockerfile에 추가해 해소 (PR #547). 이후 기능 PR들이
  이미지 스캔 실패로 오염되지 않도록, **베이스 패치는 기능 PR과 별도 커밋**으로
  Dockerfile에 반영하는 것이 원칙이다.
- Trivy 자체·action 버전 bump는 dependabot이 따른다 (워크플로 수정이라 병합에
  `workflow` 스코프 토큰 또는 웹 UI가 필요할 수 있다).

**CI 신호 분리**: docker 워크플로는 `Dockerfile`/`docker-entrypoint.sh`/`.dockerignore`/
`pyproject.toml`/`uv.lock`/`src/**`/워크플로 자체 변경 시에만 실행된다(이미지에
포함되는 파일이 바뀌어야 재스캔이 의미 있음). 문서·테스트 전용 변경은 스캔을
트리거하지 않으므로 기능 PR과 독립적인 신호를 유지한다.

## 12. 단일 VM 프로덕션 배포 (ADR 0017)

풀스택(Studio 프론트엔드 + Builder 백엔드)을 VM 한 대에 배포하는 참조 토폴로지는
[ADR 0017](./adrs/0017-fullstack-oci-deployment.md)에 정의되어 있다. 특정 클라우드를 전제하지 않는다
(kpubdata#812) — Docker Compose 가 도는 Linux VM 이면 된다. 핵심은 our-tax의
split-topology(별도 CUBRID DB VM)와 달리 **DB 서버 없이 단일 app VM + `/data` 볼륨**만
쓴다는 점이다 — Builder는 매니페스트(source of truth) + 파생 SQLite 인덱스를 파일로
영속화하기 때문이다(§6, ADR 0003/0010).

| 구성 요소 | 호스팅 | 산출물 |
| :--- | :--- | :--- |
| Studio(프론트엔드) | Cloudflare Pages 정적 배포 | studio 저장소 (`VITE_BUILDER_API_URL`=Builder 의 공개 주소) |
| Builder(백엔드) | VM 의 Docker + Caddy | `docker-compose.prod.app.yml`, `ops/caddy/Caddyfile` |
| 상태 | VM 에 붙인 디스크 `/data` | `builder-data` 볼륨 (네트워크 FS 금지, §6) |
| CI/CD | GitHub Actions → GHCR (이미지까지) | `.github/workflows/docker.yml` — VM 으로의 자동 배포는 없다(아래) |

배포 산출물:

- `docker-compose.prod.app.yml` — Builder + (opt-in) Caddy 스택. migration/ETL/DB 없음.
- `ops/caddy/Caddyfile` — Cloudflare → Caddy → `builder:8000` 리버스 프록시.
- `.env.app.example` — VM-local `.env` 템플릿(placeholder secret만). `.env`는 커밋 금지.
- 자동 배포 워크플로(`deploy.yml`)와 rollout 스크립트는 **제거됐다**(2026-10-05). 그 워크플로가
  대상으로 하던 VM 에 배포하지 않기로 했고, 그 워크플로는 `main` 에 머지될 때마다 실행돼 SSH 단계에서 실패하고 있었다.
  이미지는 `docker.yml` 이 GHCR 에 올린다. VM 에 올리려면 이 절의 compose 를 손으로 실행한다.
- 어떤 경로가 어떤 태그를 올리는지는 `docker.yml` 머리 주석에 있다. 요약하면 다음과 같다.
  - 풀 리퀘스트: 올리지 않는다.
  - `main` push: `main`, `latest`, `sha-…`.
  - 릴리스: `release.yml` 이 `publish: true` 로 부른다. 릴리스 풀 리퀘스트를 머지한 경우도 같다.
  - 릴리스 태그의 이미지 digest 가 없으면 `release.yml` 의 `Image published` 작업이 실패한다(#1142).

VM 최초 준비:

```bash
cp .env.app.example .env   # 값 채우고 chmod 600
docker compose -f docker-compose.prod.app.yml up -d            # IP-only
docker compose -f docker-compose.prod.app.yml --profile caddy up -d  # 공개 TLS 진입
```

> fail-closed(§2, ADR 0006): 인증 수단이 하나도 없으면 컨테이너가 기동을 거부한다.
> `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`는 재기동 사이에 동일 값을 유지해야 한다(ADR 0012).

### 인증 구성별 기동 (#1122)

컨테이너 진입점(`docker-entrypoint.sh`)과 `serve` 가 함께 판정한다. 진입점은 인증 수단이
있는지와 서비스 키의 모양을, `serve` 는 OIDC 설정 전체를 본다.

| 구성 | 기동 | 누가 들어오는가 |
|---|---|---|
| `OIDC_ISSUER` 만 (API 키 비움) | 기동 — `serve` 가 `OIDC_AUDIENCE`, allowlist 또는 `KPUBDATA_BUILDER_ADMIN_SUBJECTS`, pyjwt 를 검사하고 하나라도 빠지면 거부 | 로그인한 사용자. 토큰 없는 요청은 `401 sign-in required` |
| API 키만 | 기동 | `X-API-Key` 를 가진 소비자(단일 사용자 배포) |
| 둘 다 | 기동 | 두 경로 모두 |
| 둘 다 없음 | **거부** (dev-mode 가 아니면) | — |
| `KPUBDATA_BUILDER_DEV_MODE=1` + OIDC 또는 `ENFORCE_OWNERSHIP` | **거부** (`serve`) — dev-mode 로 인증 검사를 건너뛰지 않는다 | — |

서비스 키를 설정했다면 진입점이 다음을 거부하고, 값은 출력하지 않는다.

- 32자 미만 — `python -c "import secrets; print(secrets.token_urlsafe(32))"` 는 43자다.
- 문서·예시 파일에 실린 값 (`replace-with-strong-random-api-key`, `change-me-strong-secret`,
  `your-secret-key`, `<secret>`) — 공개된 값이라 누구나 안다.

**관리 작업과 최소 권한.**

| 작업 | OIDC 전용 배포에서 | 권한 |
|---|---|---|
| 가입 승인·거절, 관리 화면(`/admin/*`) | `KPUBDATA_BUILDER_ADMIN_SUBJECTS` 에 있는 사용자가 로그인해서 | 그 사용자만 관리자 |
| 인덱스 재구축, 취소된 run 정리 | 컨테이너 안의 CLI: `docker exec kpubdata-builder kpubdata-builder rebuild-index` / `prune-cancelled`. 재구축은 서버가 떠 있어도 된다 — 인덱스를 그 자리에서 다시 채운다(#1157). 다만 인덱스의 표가 손상되어 파일째 새로 만든 경우에는 서버를 재시작해야 새 파일을 본다 | 호스트에서 컨테이너를 다룰 수 있는 사람 — HTTP 로 열리지 않는다 |
| 스케줄 워크플로(데이터 갱신) | 서비스 키가 필요하다 — 이 소비자가 있으면 키를 함께 둔다 | 서비스 키는 관리자(`is_admin`)다. 다른 사용자의 run 은 읽지 못한다(#1072) |

**서비스 키 회전과 노출 면적.** 키는 인스턴스당 하나이고 관리자 권한을 갖는다.

- 키가 닿는 곳: 호스트의 `.env`(`chmod 600`), 컨테이너 환경변수(`docker inspect` 로 보인다),
  그 키를 쓰는 소비자의 secret 저장소. Builder 는 키 값을 로그·`owner_id`·응답 어디에도 쓰지
  않는다(#505). Studio 번들(`VITE_*`)에는 절대 넣지 않는다(§2).
- 회전: 새 값을 `.env` 에 쓰고 소비자의 secret 을 바꾼 뒤 `docker compose ... up -d` 로
  컨테이너를 다시 만든다. 한 번에 하나의 키만 유효하므로, 바꾸는 동안 옛 키로 오는 요청은
  `401` 이다 — 소비자를 먼저 멈추거나 실패를 감수한다.
- 소비자가 없으면 키를 두지 않는 것이 가장 좁다 — OIDC 전용으로 띄운다.

## 인증 실패 스로틀

인증 게이트는 클라이언트별(TCP peer 주소, 또는 신뢰하는 프록시가 알려 준 주소) 인증 실패를 슬라이딩 윈도로 세고, 한도를
넘으면 인증을 시도하기 전에 `429`(`code: "auth_throttled"`, `retry_after_seconds`)로
끊는다. 정적 API 키 추측과 무효 토큰 서명 검증 CPU 소모를 공짜로 반복하지 못하게 하는
것이 목적이다. 인증에 성공하면 그 클라이언트의 실패 기록은 즉시 비워진다.

- `KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT` (기본 `60`, `0` 이하면 비활성)
- `KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS` (기본 `60`)
- `KPUBDATA_BUILDER_TRUSTED_PROXIES` (기본 미설정) — 아래 "리버스 프록시 뒤에서".
- 401만 센다 — 403(유효 토큰의 인가 실패)과 503(JWKS 일시 장애)은 카운트하지 않는다.
- **만료된 토큰(`code: "token_expired"`)은 401 이지만 세지 않는다**(#1031). 서명은
  검증됐고 오래됐을 뿐이며, 클라이언트가 할 일은 토큰 갱신이다. 세면 한 프록시 뒤 여러
  사용자의 평범한 만료만으로 한도에 닿는다.
- `/healthz`는 인증 게이트 밖이라 스로틀과 무관하게 항상 응답한다.

### 리버스 프록시 뒤에서

기본 식별자는 TCP peer 주소다. `X-Forwarded-For`는 누구나 써 보낼 수 있으므로 기본으로는
읽지 않는다. 그래서 프록시 뒤에서는 모든 사용자가 프록시의 주소로 도착해 **한 버킷을
공유**한다 — 몇 사람의 실패가 모두를 429 로 막는다(#1031).

`KPUBDATA_BUILDER_TRUSTED_PROXIES` 에 프록시의 주소나 CIDR 블록을 콤마로 적으면, TCP
peer 가 그중 하나일 때에만 `X-Forwarded-For` 를 읽는다. 오른쪽부터 읽어 신뢰하는 프록시가
아닌 첫 주소를 클라이언트로 본다 — 프록시가 덧붙인 부분만 쓰고, 클라이언트가 직접 써
보낸 왼쪽 부분에는 닿지 않는다. 주소로 읽히지 않는 항목은 경고 로그(값이 아니라 몇 번째 항목인지)와 함께 버린다(호스트
이름은 쓸 수 없다).

```bash
# compose 의 Caddy 가 Builder 와 같은 Docker 네트워크에 있을 때: 그 네트워크의 대역
KPUBDATA_BUILDER_TRUSTED_PROXIES=172.18.0.0/16
```

- 적을 값은 **Builder 가 보는 프록시의 주소**다. compose 에서는 Caddy 컨테이너가 붙은
  네트워크의 대역이다(`docker network inspect kpubdata-builder-app-net` 의 `Subnet`).
- **프록시가 여러 겹이면 각 겹이 앞 겹을 신뢰해야 한다.** Cloudflare → Caddy → Builder
  에서 Caddy 는 기본적으로 앞단을 신뢰하지 않고 `X-Forwarded-For` 를 자기가 본 peer
  (Cloudflare 의 주소)로 바꿔 쓴다. 그 상태로 이 변수만 켜면 Builder 는 사용자 대신
  Cloudflare 엣지 주소로 묶는다. 실제 클라이언트 주소까지 내려오게 하려면 Caddy 의
  `trusted_proxies` 에 Cloudflare 대역을 적어야 한다 — 저장소의 `ops/caddy/Caddyfile` 은
  그렇게 설정돼 있지 않다.
- Builder 포트가 프록시를 거치지 않고도 닿는 배포에서는 이 변수에 그 경로의 주소가
  들어가지 않게 한다. 신뢰하는 주소에서 온 요청은 헤더로 자기 식별자를 정할 수 있다.
- 설정하지 않으면 동작은 전과 같다. 프록시가 클라이언트 주소를 넘기지 않는 배포에서는
  여전히 한도를 `0`으로 두고 프록시 계층에서 스로틀을 거는 편이 낫다.
>
> 카운터는 프로세스 로컬이다. 인스턴스를 여러 개 띄우면 인스턴스별로 센다(정확한 전역
> 한도가 아니라 남용 완화가 목적).


## 관련

- [ADR 0006](./adrs/0006-service-auth-and-deployment.md) — 인증·배포(fail-closed, Docker)
- [ADR 0017](./adrs/0017-fullstack-oci-deployment.md) — 풀스택 배포 토폴로지(단일 VM + Cloudflare Pages, 제안됨)
- ADR 0009(PR #398) — 사용자 인증(Google OIDC, 제안됨)
- ADR 0010(PR #399) — 상태 백엔드 분리(제안됨)
- [ADR 0008](./adrs/0008-async-build-job-model.md) — 비동기 build job 모델(제안됨)
- [API_CONTRACT.md](./API_CONTRACT.md) — `/healthz`, 401/403/503 응답
# Keycloak 공개 사용자 설정

클라우드 배포에서 Studio는 public SPA로 Keycloak의 Authorization Code + PKCE(S256)를 사용한다.
Builder는 `OIDC_ISSUER`와 `OIDC_AUDIENCE`가 모두 설정된 정상 OIDC 토큰만 수락하며,
issuer·audience·JWKS 서명·만료 검증은 항상 fail-closed로 유지한다.

**OIDC 배포는 다중 사용자 배포다**(ADR 0012 2026-09-30 개정, #635). 그래서 두 가지가 강제된다.

- 가입은 허용 목록 또는 **Builder 가입 승인 원장**(#785)으로만 열린다. 환경변수 허용 목록
  (`OIDC_ALLOWED_HD`, `OIDC_ALLOWED_SUBJECTS`, `OIDC_ALLOWED_EMAILS`)에 있는 사용자는 로그인만으로
  들어온다. 목록의 항목은 토큰의 issuer 와 함께 비교한다(#1074): `OIDC_ISSUER` 에 issuer 가 둘
  이상이면 항목마다 `<issuer>|<값>` 으로 어느 issuer 의 계정인지 적어야 하고, 그렇지 않은 항목이
  있으면 기동을 거부한다. 같은 sub 나 이메일이라도 다른 issuer 가 발급한 것은 다른 계정이다 —
  소유자 식별(`owner_id`)과 관리자 목록이 이미 그렇게 묶는다. issuer 가 하나인 배포는 지금처럼
  값만 적으면 된다. 목록에 없는 사용자는 첫 로그인 때 원장에 `pending` 으로 기록되고, 관리자
  (`KPUBDATA_BUILDER_ADMIN_SUBJECTS`)가 `POST /admin/users/{user_id}/approve` 로 승인하기 전까지
  모든 요청이 `403 signup_pending` 이다. `.../reject` 는 목록에 있는 사용자도 재시작 없이 막는다
  (`403 signup_rejected`). 관리자는 막히지 않는다. 허용 목록도 관리자도 없으면 아무도 들어올 수
  없으므로 `serve` 가 기동을 거부한다 — 공개 가입은 지원하지 않는다. 원장은
  `<output_root>/.service/users.sqlite3` 에 되돌릴 수 없는 해시 id·표시 이름(이메일)·상태만 둔다.
  이 원장은 요청마다 **읽고**, 달라진 것이 있을 때만 쓴다(#1121) — 첫 로그인, 표시 이름 변경,
  목록에 새로 오른 `pending` 사용자, 그리고 `last_seen_at` 은 한 시간에 한 번. 그래서 디스크가
  가득 차거나 읽기 전용이 돼도 이미 원장에 있는 사용자는 종전 상태 그대로 들어오거나 막힌다(쓰지
  못한 것은 로그에 남고 다음 요청이 쓴다). 원장을 **읽지 못하면** 아무도 들이지 않고, 첫 로그인을
  기록하지 못해도 들이지 않는다 — 둘 다 `503 signup_ledger_unavailable` 이며 관리자는 막히지 않는다.
  거절·승인은 캐시 없이 다음 요청부터 반영된다. 쓰기 경합은 30초까지 기다린다. 이 파일은 다른
  SQLite 저장소와 달리 **WAL 모드를 쓰지 않는다**: WAL 파일은 읽기만 하는 연결도 디렉터리에
  보조 파일(`-shm`)을 만들어야 해서, 파일시스템이 읽기 전용이 되면 읽기까지 실패한다. 한 가지
  예외: 목록에 새로 오른 `pending` 사용자는 그 승인을 기록하지 못한 요청에서도 들어오며, 바로 그
  순간 관리자가 거절했더라도 그 요청 한 번은 통과한다 — 다음 요청부터 거절이 적용된다.
  이것으로 디스크가 가득 찬 상태가 해결되는 것은 아니다: 업로드·빌드 등 다른 쓰기는 여전히 실패한다.
  (예전의 `OIDC_LEGACY_REQUIRE_ALLOWLIST` 스위치와 공개 가입 경고는 이것으로 대체됐다.)
- `ENFORCE_OWNERSHIP`는 환경변수 값과 무관하게 **켜진다**. 남의 run은 없는 run과 같은 404다(#796).

OIDC 없이 `ENFORCE_OWNERSHIP`도 설정하지 않은 단일 사용자 배포는 바뀌지 않는다.

`KPUBDATA_BUILDER_DEV_MODE`는 **인증을 통째로 우회**하므로 로컬 개발 전용이다. 켜진 채로
기동하면 경고 로그를 남기고, `OIDC_ISSUER`가 함께 설정돼 있으면 (사용자 인증을 구성해두고
인증을 우회하는 모순된 조합이므로) `serve`가 기동을 거부한다.

**Builder 가 받는 것은 access 토큰이다**(kpubdata-studio#722). Studio 는 Keycloak 의 access 토큰을
`Authorization: Bearer` 로 보낸다. Builder 는 토큰의 종류(`typ`·`azp`·`nonce`)를 보지 않고 클레임만
검증한다 — RS256 서명, `iss`, `aud` 에 `OIDC_AUDIENCE` 포함, `exp`/`iat`/`sub`, `email_verified: true`.
기본 Keycloak realm 의 access 토큰은 `aud` 가 `account` 뿐이라 **거부된다**(401). realm 에 두 가지를
설정해야 한다:

1. **Audience mapper** — `kpubdata-studio` client(또는 그 client 의 dedicated scope)에 mapper 를
   추가한다: Mapper type `Audience`, Included Client Audience(또는 Included Custom Audience)에
   `OIDC_AUDIENCE` 값(예: `kpubdata-builder`), **Add to access token: ON**. 이것이 없으면 access
   토큰의 `aud` 에 Builder 가 없다.
2. **`email` client scope 를 Default 로** — access 토큰에 `email` 과 `email_verified` 가 실려야 한다.
   사용자 계정의 Email verified 가 꺼져 있으면 `401 email not verified` 다. realm 의 Verify email 을
   켜면 가입 과정에서 채워진다.

Keycloak Admin Console에서 realm의 User registration과 Verify email을 켜고 적절한
password policy를 설정한다. Google Identity Broker를 사용하려면 broker의 Store Tokens는
꺼 둔다. Studio가 Google token을 Builder에 직접 전달하지 않으며, signup/password UI는
Keycloak hosted UI가 담당한다. 스케줄러 등 service principal은 기존 `X-API-Key`를 계속 사용한다.

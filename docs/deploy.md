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
| Studio(사람 사용자) | `Authorization: Bearer <Google ID token>` | ADR 0009, ADR 0006의 "다중 소비자" 후속 |
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

> GitHub Pages 데모(`https://yeongseon.github.io`)는 mock 모드(`VITE_USE_REAL_BUILDER` 미설정)라 Builder를 호출하지 않으므로 등록 대상이 아니다.

## 5. SPA 토큰 보관

- Studio는 토큰을 **메모리(zustand 스토어, persist 미들웨어 없음)**에만 보관한다.
- `localStorage`/`sessionStorage`에 토큰을 쓰지 않는다 — XSS 한 번이면 탈취된다.
- 새로고침 시 재로그인(GIS 자동 로그인으로 마찰 완화).

## 6. 상태 저장 제약 — 단일 replica 전제

산출물(artifacts)·완료 이력(`BuildIndex`, ADR 0003)이 로컬 FS/SQLite에 고정되어 있어 **replica를 2개 이상 띄울 수 없다** (ADR 0010/#375). 배포 시:

- `minReplicas: 1, maxReplicas: 1` 고정.
- `/data` 볼륨(Azure Files 등) 마운트 필수 — 산출물·인덱스 영속화.
- **Azure Files 위에 SQLite를 올리지 말 것** — 파일 잠금이 불안정하다 (ADR 0010 §5).

멀티 replica는 ADR 0010(`ArtifactStore` 추상화 + 백엔드 분리) 이행 후 가능하다.

### 6.1 CUBRID 상태 백엔드 (ADR 0016)

기본 백엔드는 sqlite/local(무외부의존)이다. 조직 요구로 CUBRID 를 쓰려면
(OCI Compute VM + Docker 단일 인스턴스 전제):

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
- CUBRID 는 OCI 관리형 서비스가 없으므로 같은 VM 에 컨테이너로 함께 띄운다(예: docker-compose,
  `infra/oci/` 참조).

## 7. 헬스체크·종료

- `GET /healthz` — 무인증 liveness probe (#372). 프로브가 API 키를 못 실을 때 사용.
- `Dockerfile` `HEALTHCHECK` — urllib로 `/healthz` 폴링 (#372).
- `SIGTERM` — 우아운 종료(진행 중 요청 drain, #374). ACA/K8s 롤링 업데이트 대응.

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
        + query_concurrency * (per_Polars_child + 최대 8 MiB IPC payload)
        + filesystem/cache headroom
```

CPU 수요는 대략 `active_build_workers * build_CPU` +
`query_concurrency * Polars_child_CPU` + HTTP overhead다. Polars child 하나도 내부적으로 여러
thread를 사용할 수 있으므로 `query_concurrency`를 vCPU 수처럼 간주하면 안 된다. 작은 ACA
인스턴스는 `infra/main.bicep` 기본값인 1 vCPU/2 GiB, HTTP worker 4, async build worker 4,
query concurrency 1에서 시작한다. HTTP와 build는 같은 설정값을 받지만 서로 다른 pool이라
동시에 각각 4개까지 실행될 수 있다. 따라서 CPU/memory 중심 build를 많이 제출하는 환경에서는
이 기본 ACA 크기만으로 안전하다고 가정하지 말고 working set과 throttling을 관찰해야 한다.

한 호스트에 동시에 존재할 수 있는 실행 단위 전체 — 각각 따로 설정되므로 **합**을 호스트와
대조해야 한다(#701):

| 실행 단위 | 상한 | 설정 |
| :--- | :--- | :--- |
| HTTP worker thread | 기본 10 | `KPUBDATA_BUILDER_MAX_WORKERS` / `serve --max-workers` |
| 비동기 build worker thread | HTTP 와 같은 값, queued 10 | 같은 설정 |
| build 하나 안의 source fetch thread | **build 당** 최대 4 (`_MAX_PARALLEL_SOURCES`) | 코드 상수 |
| query child process | 기본 2 | `KPUBDATA_QUERY_MAX_CONCURRENCY` |
| query child 하나의 메모리 | **기본 무제한**. 설정하면 child 의 address space 를 제한해 초과한 질의만 실패(`400 query_failed`)하고 서버와 다른 요청은 계속된다 | `KPUBDATA_QUERY_MAX_MEMORY_MB` |
| Polars 내부 thread | 프로세스마다 기본 CPU 코어 수 — build worker 와 query child 모두 | `POLARS_MAX_THREADS` |

`KPUBDATA_QUERY_MAX_MEMORY_MB` 는 address space 상한(`RLIMIT_AS`)이라 RSS 보다 크게 잡아야
한다 — Polars 가 import 시점에 가상 메모리를 넉넉히 예약하므로 너무 작으면 모든 질의가
실패한다. 예산 기준 admission control(개수가 아니라 메모리·CPU 합으로 받는 것)은 무엇으로
잴지부터 정해야 하는 결정으로 남아 있다(#701).

query timing은 다음 경계를 사용한다.

- `execution_ms`: parent의 `QueryEngine.execute` 진입부터 payload 수신·검증과 child join까지.
- `startup_ms`: 같은 parent 시작점부터 spawned child의 Polars import와 bounded SQL setup 완료,
  `scan_parquet` 직전까지.
- `engine_execution_ms`: child의 `scan_parquet` 직전부터 SQL context/execute/collect 완료까지.
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
다른 thread가 잡은 lock, logging/runtime 상태, Polars native thread-pool 상태를 child가
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

## 12. OCI 단일 VM 프로덕션 배포 (ADR 0017)

풀스택(Studio 프론트엔드 + Builder 백엔드)을 OCI에 배포하는 참조 토폴로지는
[ADR 0017](./adrs/0017-fullstack-oci-deployment.md)에 정의되어 있다. 핵심은 our-tax의
split-topology(별도 CUBRID DB VM)와 달리 **DB 서버 없이 단일 app VM + `/data` 볼륨**만
쓴다는 점이다 — Builder는 매니페스트(source of truth) + 파생 SQLite 인덱스를 파일로
영속화하기 때문이다(§6, ADR 0003/0010).

| 구성 요소 | 호스팅 | 산출물 |
| :--- | :--- | :--- |
| Studio(프론트엔드) | Cloudflare Pages 정적 배포 | studio 저장소 (`VITE_BUILDER_API_URL`=app-01) |
| Builder(백엔드) | OCI `app-01` Docker + Caddy | `docker-compose.prod.app.yml`, `ops/caddy/Caddyfile` |
| 상태 | `app-01` 로컬 블록 볼륨 `/data` | `builder-data` 볼륨 (네트워크 FS 금지, §6) |
| CI/CD | GitHub Actions → GHCR → SSH | `.github/workflows/deploy.yml` |

배포 산출물:

- `docker-compose.prod.app.yml` — Builder + (opt-in) Caddy 스택. migration/ETL/DB 없음.
- `ops/caddy/Caddyfile` — Cloudflare → Caddy → `builder:8000` 리버스 프록시.
- `.env.app.example` — VM-local `.env` 템플릿(placeholder secret만). `.env`는 커밋 금지.
- `.github/workflows/deploy.yml` — 이미지 빌드/푸시 후 `app-01`에 SSH 배포 + `/healthz` 체크.

app-01 최초 준비:

```bash
cp .env.app.example .env   # 값 채우고 chmod 600
docker compose -f docker-compose.prod.app.yml up -d            # IP-only
docker compose -f docker-compose.prod.app.yml --profile caddy up -d  # 공개 TLS 진입
```

> fail-closed(§2, ADR 0006): `KPUBDATA_BUILDER_API_KEY` 없이는 컨테이너가 기동을 거부한다.
> `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`는 재기동 사이에 동일 값을 유지해야 한다(ADR 0012).

## 인증 실패 스로틀

인증 게이트는 클라이언트별(TCP peer 주소) 인증 실패를 슬라이딩 윈도로 세고, 한도를
넘으면 인증을 시도하기 전에 `429`(`code: "auth_throttled"`, `retry_after_seconds`)로
끊는다. 정적 API 키 추측과 무효 토큰 서명 검증 CPU 소모를 공짜로 반복하지 못하게 하는
것이 목적이다. 인증에 성공하면 그 클라이언트의 실패 기록은 즉시 비워지므로, 토큰 만료로
몇 번 401을 받는 정상 사용자는 누적되지 않는다.

- `KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT` (기본 `60`, `0` 이하면 비활성)
- `KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS` (기본 `60`)
- 401만 센다 — 403(유효 토큰의 인가 실패)과 503(JWKS 일시 장애)은 카운트하지 않는다.
- `/healthz`는 인증 게이트 밖이라 스로틀과 무관하게 항상 응답한다.

> **리버스 프록시 주의**: 식별자는 TCP peer 주소이며 `X-Forwarded-For`는 위조 가능하므로
> 읽지 않는다. 클라이언트 IP를 보존하지 않는 프록시/로드밸런서 뒤에 있으면 모든 요청이
> 한 버킷을 공유해 한 클라이언트의 실패가 다른 사용자에게 영향을 준다 — 그런 배포에서는
> 한도를 `0`으로 두어 비활성화하고 프록시 계층에서 스로틀을 거는 편이 낫다.
>
> 카운터는 프로세스 로컬이다. 인스턴스를 여러 개 띄우면 인스턴스별로 센다(정확한 전역
> 한도가 아니라 남용 완화가 목적).


## 관련

- [ADR 0006](./adrs/0006-service-auth-and-deployment.md) — 인증·배포(fail-closed, Docker)
- [ADR 0017](./adrs/0017-fullstack-oci-deployment.md) — 풀스택 배포 토폴로지(OCI 단일 VM + Cloudflare Pages, 제안됨)
- ADR 0009(PR #398) — 사용자 인증(Google OIDC, 제안됨)
- ADR 0010(PR #399) — 상태 백엔드 분리(제안됨)
- [ADR 0008](./adrs/0008-async-build-job-model.md) — 비동기 build job 모델(제안됨)
- [API_CONTRACT.md](./API_CONTRACT.md) — `/healthz`, 401/403/503 응답
# Keycloak 공개 사용자 설정

클라우드 배포에서 Studio는 public SPA로 Keycloak의 Authorization Code + PKCE(S256)를 사용한다.
Builder는 `OIDC_ISSUER`와 `OIDC_AUDIENCE`가 모두 설정된 정상 OIDC 토큰만 수락하며,
issuer·audience·JWKS 서명·만료 검증은 항상 fail-closed로 유지한다.

**OIDC 배포는 다중 사용자 배포다**(ADR 0012 2026-09-30 개정, #635). 그래서 두 가지가 강제된다.

- 허용 목록(`OIDC_ALLOWED_HD`, `OIDC_ALLOWED_SUBJECTS`, `OIDC_ALLOWED_EMAILS`) 중 하나 이상이
  **필수**다. 없으면 `serve`가 기동을 거부하고, 다른 방식으로 뜬 프로세스도 모든 토큰을 403으로
  거부한다 — 공개 가입은 지원하지 않는다. 설정된 목록 중 하나라도 일치해야 통과한다.
  (예전의 `OIDC_LEGACY_REQUIRE_ALLOWLIST` 스위치와 공개 가입 경고는 이것으로 대체됐다.)
- `ENFORCE_OWNERSHIP`는 환경변수 값과 무관하게 **켜진다**. 남의 run은 없는 run과 같은 404다(#796).

OIDC 없이 `ENFORCE_OWNERSHIP`도 설정하지 않은 단일 사용자 배포는 바뀌지 않는다.

`KPUBDATA_BUILDER_DEV_MODE`는 **인증을 통째로 우회**하므로 로컬 개발 전용이다. 켜진 채로
기동하면 경고 로그를 남기고, `OIDC_ISSUER`가 함께 설정돼 있으면 (사용자 인증을 구성해두고
인증을 우회하는 모순된 조합이므로) `serve`가 기동을 거부한다.

Keycloak Admin Console에서 realm의 User registration과 Verify email을 켜고 적절한
password policy를 설정한다. Google Identity Broker를 사용하려면 broker의 Store Tokens는
꺼 둔다. Studio가 Google token을 Builder에 직접 전달하지 않으며, signup/password UI는
Keycloak hosted UI가 담당한다. 스케줄러 등 service principal은 기존 `X-API-Key`를 계속 사용한다.

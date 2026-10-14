# 배포 가이드

Builder HTTP 서비스를 로컬 개발 이상으로 운영하기 위한 배포·인증 스토리. 본 문서는 [ADR 0006](./adrs/0006-service-auth-and-deployment.md)(인증·배포), ADR 0009(사용자 인증, PR #398 — [ADR 0015](./adrs/0015-email-password-oidc-idp-keycloak.md)로 대체됨)의 운영 가이드를 통합한다.

> **상태**: ADR 0009는 제안됨(Proposed). 인증(Bearer) 구현은 B3(#385)/B4(#386) 진행 중이며, 본 문서의 Bearer 관련 절은 구현 완료 후 적용된다. 컨테이너 배포(fail-closed, HEALTHCHECK)는 이미 구현되었다.

## 1. 기본 배포 형태 — 사용자의 브라우저가 부르는 엔드포인트

**Studio 는 정적 SPA 다. Builder 를 부르는 것은 Studio 의 서버가 아니라 사용자의 브라우저다.** 그래서 Studio 를 쓰는 배포에서 Builder 는 사용자의 브라우저가 닿을 수 있는 HTTPS 주소를 가져야 한다. "Studio 가 같은 네트워크/VPC 에서 Builder 를 호출한다"는 구조는 Studio 에 서버 쪽 구성 요소가 있을 때만 성립하고, 지금의 Studio 에는 없다(kpubdata-studio#841).

- 참조 배포(§12)는 Cloudflare → Caddy → Builder 로 그 주소를 내고, Studio 는 그 주소로 빌드·설정된다(`VITE_BUILDER_API_URL`, 컨테이너 이미지에서는 `BUILDER_API_URL`). Builder 컨테이너 자체는 포트를 밖으로 열지 않고 Caddy 만 연다.
- 그 주소로 들어오는 것을 좁히는 것은 다음 둘이다. 네트워크 위치가 아니다.
  - **인증** — `/healthz` 같은 무인증 프로브를 빼면 모든 요청은 `X-API-Key` 또는 OIDC Bearer 를 가져야 한다(§2). OIDC 배포에서는 그 위에 allowlist 와 가입 원장이 있다.
  - **CORS** — `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 에 적은 Studio 오리진만 허용하고 기본은 전부 거부한다(§4, ADR 0006). CORS 는 다른 사이트의 페이지가 사용자의 브라우저로 Builder 를 부르는 것을 막을 뿐, 브라우저가 아닌 호출자를 막지 않는다. 그쪽을 막는 것은 인증이다.
- 브라우저가 부르지 않는 배포 — 스케줄 워크플로만 Builder 를 부르는 경우 — 는 Builder 를 내부 서비스로만 둘 수 있고, 그럴 수 있으면 그렇게 두는 편이 공격면이 작다.

> **결정.** 이 구조가 기본형이다([ADR 0023](adrs/0023-public-endpoint-with-auth-and-cors.md), 2026-10-11). ADR 0009 결정 3 은 "Builder 는 공개 인그레스를 갖지 않는다(internal ingress)"를 기본형으로 정했는데, 그 전제는 Studio 가 같은 네트워크에서 Builder 를 부른다는 것이었다. Studio 가 정적 SPA 가 되면서 그 전제가 사라졌고, ADR 0023 이 그 결정을 대체했다. 경계는 네트워크 위치가 아니라 위의 인증과 CORS allowlist 다.

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

- 인스턴스는 하나다. 오케스트레이터에 올린다면 replica 를 1 로 고정한다.
- **롤링 배포를 하지 않는다.** 새 프로세스와 옛 프로세스가 같은 `/data` 를 함께 여는 시간이
  생기면 안 된다. 옛 컨테이너를 내린 뒤 새 컨테이너를 올린다(compose 의 `up -d` 가 그렇게 한다).
- `/data` 는 **로컬 블록 볼륨**으로 마운트한다 — 산출물·인덱스·SQLite 저장소가 여기에 있다.
  compose 는 named volume `builder-data` 를 쓴다.
- **네트워크 파일시스템(NFS, Azure Files) 위에 SQLite를 올리지 말 것** — 파일 잠금이
  불안정하다 (ADR 0010 §5). 저장소에 있던 Azure Container Apps 템플릿은 `/data` 를 Azure
  Files 에 두었기 때문에 없앴다(#1097, `infra/README.md`).
- 재기동해도 `/data` 가 남는지는 `scripts/data_volume_smoke.py` 가 `Docker` 워크플로에서
  확인한다: 실제 compose 로 올리고, `/data` 에 쓰고, 컨테이너를 내렸다가 다시 올려 읽는다.

replica 를 늘릴 수 없는 이유는 저장소만이 아니다. ADR 0010 의 백엔드 분리(`ArtifactStore`,
`make_build_index()`)는 구현되어 있지만(구현은 SQLite 하나다, ADR 0022), 아래는 여전히
**프로세스 메모리**에 있다(2026-10-07):

| 프로세스 안에만 있는 것 | 둘 이상이면 |
|---|---|
| 비동기 job 레지스트리와 큐 (`service/jobs.py`) | 다른 replica 에 제출된 job 의 상태를 답하지 못한다 |
| job 에 묶인 provider 키 (`JobCredentials`, #683) | 키를 받은 replica 만 그 job 을 돌릴 수 있다 |
| 동시에 도는 build 의 자리 수 (`BuildSlots`) 와 preview 상한 | 상한이 replica 수만큼 곱해진다 |
| 인증 실패 스로틀, probe 간격 | 클라이언트가 replica 를 바꿔 가며 한도를 피한다 |

그래서 재시작하면 대기·실행 중이던 job 은 이어지지 않는다. 종료 신호를 받았을 때의 처리는
§7, 중단된 run 의 표시는 #683·#1120 을 본다.

### 6.0 이전 릴리스로 되돌렸을 때 (#1096)

상태 저장소는 자기 스키마의 버전을 적어 둔다. 실행 중인 릴리스가 아는 것보다 **새로운 버전**의
저장소를 만나면 그 저장소를 열지 않고 건드리지도 않는다. 열 개 모두(§6.0.1) 기동할 때 확인하므로,
서버가 `error:` 한 줄을 남기고 뜨지 않는다(종료 코드 1). 아직 없는 저장소는 지금처럼 처음 쓰일 때
만들고, 확인하느라 파일을 만들지도 않는다.

| 저장소 | 새 버전을 만나면 | 할 수 있는 일 |
|---|---|---|
| 빌드 인덱스 (`_builds.sqlite`) | 기동 거부. 예전에는 표를 지우고 다시 만들어서, 새 릴리스로 돌아가면 인덱스가 비어 있었다 | 그 저장소를 쓴 릴리스를 띄우거나, `kpubdata-builder rebuild-index` 로 이 릴리스의 인덱스를 manifest 에서 다시 만든다 — 인덱스는 파생물이다 |
| run 이벤트 (`_build_events.sqlite`) | 기동 거부. 예전에는 버전을 보지 않고 열었다 | 그 저장소를 쓴 릴리스를 띄우거나, 업그레이드 전 백업에서 출력 디렉터리를 복구한다 — 이벤트는 다른 것에서 다시 만들 수 없다 |
| 테이블 카탈로그 (웨어하우스의 `_warehouse.sqlite`) | 기동 거부. 예전에는 처음 쓰일 때 열려서, 기동이 아니라 그 요청이 실패했다 | 위와 같다 — 카탈로그도 다시 만들 수 없다 |
| 게시 영수증, provider 자격 증명, 업로드, 문서 revision, 가입 원장, 저장한 분석 | 기동 거부. 예전에는 버전을 적지 않아서, 새 릴리스가 쓴 파일을 이 릴리스의 것으로 읽었다 | 그 저장소를 쓴 릴리스를 띄우거나, 업그레이드 전 백업에서 출력 디렉터리를 복구한다 — 여섯 모두 다른 것에서 다시 만들 수 없다 |
| provider 연결 테스트의 마지막 결과 (`.service/provider_tests.sqlite3`) | 기동 거부. 예전에는 버전을 적지 않았다 | 그 저장소를 쓴 릴리스를 띄우거나, 파일을 다른 곳으로 옮긴다 — 연결 테스트를 다시 하면 채워진다 |

provider 자격 증명 저장소는 실제로 쓸 때만 확인한다 — 마스터 키(`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`)가
설정되어 있고 상태 백엔드가 SQLite 일 때다.

**버전이 없는 파일**: 위 일곱 저장소는 이 릴리스부터 버전을 적는다. 이전 릴리스가 만든 파일에는 버전이
없는데, 처음 열 때 첫 버전(1)으로 받아들인다 — 표와 행은 그대로 두고, 그 파일이 만들어진 뒤에 생긴 열이
빠져 있으면 더한 다음(게시 영수증의 감사 기록, 업로드, 저장한 분석), 버전 표에 1 을 적는다. 이 전부가 한
트랜잭션이라 도중에 실패하면 파일은 버전이 없던 그대로 남고 다음 기동이 다시 시도한다. 운영자가 할 일은
없다. 버전 표(`schema_version`)만 더해질 뿐 기존 표의 모양은 이전 릴리스가 쓰던 것과 같으므로, 이 릴리스가
받아들인 파일을 **바로 앞 릴리스로 되돌려 열 수 있다**(이전 릴리스는 버전 표를 보지 않는다). 가입 원장은
쓸 수 없는 디스크에서도 읽혀야 하므로(#1121), 버전을 적지 못하면 경고를 남기고 그대로 읽으며 다음 기동에
적는다. 이미 버전이 적힌 저장소는 열 때 아무것도 쓰지 않는다.

**오래된 버전**: 카탈로그는 버전 사슬을 따라 올린다. 올리기 직전에 카탈로그를 SQLite 로 복사해
옆에 둔다(`_warehouse.sqlite.v<이전 버전>.before-migration`). 사본은 마이그레이션과 같은 쓰기 잠금
안에서 뜨므로, 사본과 마이그레이션 사이에 다른 프로세스의 쓰기가 끼지 않는다. 마이그레이션은 한
트랜잭션이라 도중에 실패하면 이전 버전 그대로 남고, 다음 기동이 다시 시도한다. **사본은
마이그레이션할 때마다 그 시점의 카탈로그로 바뀐다**(#1163) — 업그레이드 → 롤백(사본을 되돌려
이전 릴리스로 운영) → 재업그레이드를 거치면, 사본은 롤백 뒤 이전 릴리스가 쓴 내용까지 담는다.
예전에는 처음 뜬 사본을 그대로 두어서, 두 번째로 되돌릴 때 그 사이의 쓰기를 잃었다. 되돌릴 때는
사본을 옮기지 말고 복사해 두는 편이 낫다. 버전 2 에서 올린 카탈로그 중 예전 마이그레이션이 `snapshot_leases`·`snapshot_holds` 의 외래 키를 지워진 `table_snapshots_v2` 로 바꿔 놓은 것은 열 때 고친다(#1319) — 두 표를 올바른 참조로 다시 만들고 모든 행을 옮긴 뒤, `PRAGMA foreign_key_check` 와 `integrity_check` 가 통과해야 확정한다. 고치기 전에 `_warehouse.sqlite.v5.before-foreign-key-repair` 로 복사해 둔다. 스냅샷이 없는 리스·홀드처럼 고친 결과를 증명할 수 없으면 아무것도 바꾸지 않고, 어느 행이 문제인지 적어 열기를 거부한다. 검사를 통과시키려고 행을 지우지는 않는다. **사본은 카탈로그뿐이다** — 스냅샷 파일은 들어 있지 않으므로,
그 뒤에 쓰인 것이 있는 웨어하우스에 사본만 되돌려 놓으면 그만큼을 잃는다. 전체 복구는 `backup`
으로 받은 백업으로 한다(§6.2). 인덱스는 `serve` 가 요청을 받기 전에
manifest 에서 다시 만든다 — 인덱스 파일이 아예 없을 때도 같다. 예전에는 비운 채로 떠서
`rebuild-index` 를 따로 돌리기 전까지 이전 run 이 목록에 보이지 않았다. run 이 많으면 그만큼
기동이 늦어진다(run 마다 manifest 하나를 읽는다). 그 밖의 일곱 저장소는 아직 첫 버전뿐이라 올릴
것이 없다. 스키마가 바뀌면 저장소마다 한 단계씩 더해지고(`store/schema_version.py` 의 `StoreSchema`),
오래된 파일은 열 때 한 트랜잭션 안에서 그 단계들을 차례로 밟는다.

### 6.0.1 SQLite 상태 저장소 목록과 연결 방식 (#1096)

Builder 가 두는 SQLite 파일은 열 개다. 목록의 정본은 `src/kpubdata_builder/store/inventory.py` 이고,
`tests/unit/test_state_store_inventory.py` 가 그 목록을 코드(SQLite 를 여는 모듈, 각 모듈의 timeout·저널
모드·버전 관리)와 아래 표에 대조한다. 저장소를 더하거나 연결 방식을 바꾸면 그 파일과 이 표를 함께
고쳐야 테스트가 통과한다.

| 저장소 | 위치 | 잠금 대기 | 저널 모드 | 스키마 버전 | 잃으면 |
|---|---|---|---|---|---|
| 빌드 인덱스 | 출력 디렉터리의 `_builds.sqlite` | 30초 | WAL | 버전 표 | 잃어도 된다 — manifest 와, manifest 없이 끝난 run 은 run 이벤트 저장소의 종료 기록에서 다시 만든다(`serve` 가 기동할 때, 또는 `rebuild-index`) |
| run 이벤트와 제출 기록 | 출력 디렉터리의 `_build_events.sqlite` | 30초 | WAL | 버전 표 | run 의 타임라인과 제출자 기록, manifest 없이 끝난 run 의 종료 기록(#1120)을 잃는다. 다시 만들 수 없다 |
| 게시 영수증 | 출력 디렉터리의 `_publish_receipts.sqlite` | 30초 | WAL | 버전 표 | 어떤 run 을 어디에 게시했는지와, 같은 게시가 두 번 나가는 것을 막는 근거를 잃는다. 다시 만들 수 없다 |
| provider 자격 증명 (암호화) | 출력 디렉터리의 `.service/provider-credentials.sqlite3` | 30초 | 기본(rollback journal) | 버전 표 | 사용자가 저장한 provider 키를 잃는다. 각자 다시 입력해야 한다 |
| 업로드 | 출력 디렉터리의 `.service/uploads.sqlite3` | 30초 | 기본(rollback journal) | 버전 표 | 올린 파일과 그 목록을 잃는다(큰 파일의 내용은 옆의 `uploads.sqlite3.blobs/` 에 있다) |
| provider 연결 테스트의 마지막 결과 | 출력 디렉터리의 `.service/provider_tests.sqlite3` | 30초 | 기본(rollback journal) | 버전 표 | 잃어도 된다 — 연결 테스트를 다시 하면 채워진다 |
| 문서 revision 과 감사 기록 | 출력 디렉터리의 `.service/revisions.sqlite3` | 30초 | 기본(rollback journal) | 버전 표 | BuildSpec 과 표시 주석의 저장 이력을 잃는다. 다시 만들 수 없다 |
| 가입 원장 | 출력 디렉터리의 `.service/users.sqlite3` | 30초 | 기본(rollback journal) | 버전 표 | 가입 승인·거절 기록을 잃는다. allowlist 에 없는 사용자는 다시 승인을 기다린다 |
| 저장한 분석 | 출력 디렉터리의 `.service/analyses.sqlite3` | 30초 | 기본(rollback journal) | 버전 표 | 저장한 SQL 과 그것이 읽은 스냅샷의 기록을 잃는다. 다시 만들 수 없다 |
| 테이블 카탈로그 | 웨어하우스의 `_warehouse.sqlite` | 30초 | WAL | 버전 표 | 어떤 스냅샷이 어느 테이블의 현재 것인지를 잃는다. 다시 만들 수 없다 |

상태는 위 SQLite 파일과 로컬 파일에만 있다. CUBRID 백엔드는 제거되었다(§6.1, ADR 0022).

**지금의 방식.** 이 표는 정해 둔 규칙이 아니라 지금 코드가 하는 일이다. 저장소마다 다른 점은 다음과 같다.

- **잠금 대기는 모두 30초다.** 저장소를 여는 모든 연결이 한 값(`sqlite_settings.BUSY_TIMEOUT_SECONDS`)을
  쓰고, `PRAGMA busy_timeout` 으로 따로 정하는 곳도 같은 값에서 나온다. provider 자격 증명과 업로드만
  5초였는데 이유가 적혀 있지 않았고, 같은 잠금 뒤에서 어떤 요청은 5초에 실패하고 다른 요청은 계속 기다렸다.
  30초는 열 중 여덟이 이미 쓰던 값이고, 다른 무엇에서 끌어낸 숫자는 아니다(요청 전체에는 시간 상한이 없다 —
  `service/http.py` 의 30초는 소켓이 읽기를 기다리는 시간이다). 카탈로그의 사본을 뜨는 연결도 SQLite
  기본값(5초) 대신 이 값을 쓴다.
- **자격 증명과 업로드 저장소는 줄을 서서 기다린다.** 이 둘은 프로세스 안에서 한 번에 한 요청만 저장소를
  쓰도록 잠금(`RLock`)을 쥔 채 SQLite 를 기다린다. 다른 프로세스가 파일을 잠그고 있으면 뒤 요청들이 각각
  30초까지(전에는 5초까지) 차례로 밀린다. 파일을 오래 잠그는 다른 프로세스(백업 도구, 손으로 연 `sqlite3`)가
  없다면 드러나지 않는다.
- **WAL 은 넷이다**(빌드 인덱스, run 이벤트, 게시 영수증, 테이블 카탈로그). 나머지는 SQLite 기본 모드다.
  새 파일을 두 연결이 동시에 처음 열면 둘 다 WAL 로 바꾸려 하고 SQLite 는 한쪽을 기다리게 하지 않고 곧바로
  거절하므로, 넷 모두 그 전환을 잠금 대기 시간 안에서 다시 시도한다(`sqlite_settings.enable_wal`, #1210).
- **가입 원장은 일부러 WAL 이 아니다.** WAL 데이터베이스는 쓸 수 없는 디렉터리에서 읽지 못한다(연결마다
  `-shm` 파일을 만들어야 한다). 원장은 디스크가 읽기 전용이 되어도 이미 가입한 사용자를 들여보내야 해서
  기본 모드로 둔다(#1121). 그래서 "전부 WAL" 은 답이 아니다.
- **읽기 전용 연결과 백업 사본은 따로다.** 저장소의 버전만 읽는 연결(`store/schema_version.py`)은 저널
  모드를 정하지 않고, 카탈로그의 사본(`warehouse/backup.py`, 마이그레이션 전 사본)은 기본 모드로 돌려 둔다.
- **버전 표는 열 개 모두에 있다.** 모두 `schema_version` 표에 버전을 적고, 더 새로운 릴리스가 쓴 저장소를
  열지 않는다(§6.0). 예전에는 셋만 그랬고, 나머지 일곱은 버전을 적지 않았다 — 그중 셋은 빠진 열을 열 때
  더하는 방식으로만 바뀌어 왔다. 버전을 적지 않는 저장소를 새로 두려면 목록의 그 항목에 이유를 적어야 하고
  (`unversioned_because`), 이유가 없으면 테스트가 실패한다. 그런 저장소는 지금 없다.

### 6.1 CUBRID 상태 백엔드는 제거되었다 (ADR 0022, #1094)

CUBRID 상태 백엔드(ADR 0016)는 제거되었다. 상태는 SQLite 와 로컬 파일에만 있다(§6). `cubrid` extra,
`:cubrid` 이미지, `infra/cubrid/` 구성도 함께 없앴다. CUBRID 로 운영 중인 배포는 없다고 소유자가
확인했다(ADR 0022 승인 기록).

남아 있는 CUBRID 설정은 **모든 명령**(`serve`, `build`, `rebuild-index`, `prune-cancelled`, `backup` 등)을
시작하지 못하게 한다. 알림 없이 SQLite 로 넘어가거나 경고만 남기고 뜨지 않는다.

| 설정 | 거부하는 값 |
| :--- | :--- |
| `KPUBDATA_BUILDER_STORAGE_BACKEND` | 비어 있거나 `sqlite` 가 아닌 모든 값(`cubrid` 포함). 비어 있거나 `sqlite` 이면 아무 일도 하지 않는다 |
| `KPUBDATA_BUILDER_CUBRID_URL` | 비어 있지 않은 모든 값. 오류는 URL 을 다시 적지 않는다(비밀번호가 들어 있다) |

오류는 `error:` 로 시작하는 한 줄이고, 종료 코드는 1 이다. 할 일은 둘 중 하나다.

- CUBRID 데이터를 아직 대조하지 않았다면, CUBRID 백엔드가 있는 마지막 릴리스(v0.4.0)로 되돌려
  [ADR 0022](./adrs/0022-sqlite-only-state-and-cubrid-retirement.md) §3 의 절차를 먼저 밟는다. 대조 도구는
  그 릴리스에 없으므로 ADR 0022 승인 기록의 2026-10-12 보충대로 #1269 커밋의 소스에서 실행한다.
- 대조를 마쳤다면(또는 CUBRID 를 쓴 적이 없다면) 두 설정을 `.env` 와 환경에서 지운다.

### 6.2 전체 상태의 백업과 복구 (#1095)

`warehouse-backup` 은 카탈로그와 스냅샷만 담는다. 아래 세 명령은 출력 디렉터리(`/data`)의
**모든 상태**를 하나의 파일로 받고, 그 파일을 검증한 뒤에만 통째로 되돌린다. 제거된 CUBRID
설정이 남아 있으면 다른 모든 명령처럼 셋 모두 거부한다(§6.1).

```bash
# 1) 서버를 내린다. SIGTERM 이 실행 중인 빌드를 끝내거나 멈춘다(§7) — 이것이 쓰기를 멈추는 방법이다.
docker compose -f docker-compose.prod.app.yml stop builder

# 2) 백업. ARCHIVE 는 /data 밖이어야 한다(안이면 거부한다 — 자기 자신을 복사하게 된다).
docker compose -f docker-compose.prod.app.yml run --rm --no-deps \
  -v /srv/backups:/backups --entrypoint kpubdata-builder builder \
  backup /backups/state-$(date -u +%Y%m%dT%H%M%SZ).tar --output-dir /data

# 3) 서버를 다시 올린다.
docker compose -f docker-compose.prod.app.yml start builder

# 백업 파일 검증 — 복구가 하는 검사를 그대로 하고 아무것도 바꾸지 않는다
kpubdata-builder verify-backup ARCHIVE [--work-dir DIR]

# 복구 (서버를 내린 상태에서). 지금의 상태는 지우지 않고 옆으로 옮겨 둔다
kpubdata-builder restore ARCHIVE --output-dir /data
# 복구가 도중에 죽었을 때 — 그 전 상태를 되돌려 놓는다
kpubdata-builder restore --recover --output-dir /data
```

웨어하우스는 `--warehouse`(없으면 `KPUBDATA_BUILDER_WAREHOUSE`)로 주며, compose 처럼 출력
디렉터리 바로 아래(`/data/warehouse`)여야 한다. 출력 디렉터리 밖의 웨어하우스는 한 번의 교체로
함께 되돌릴 수 없어서 거부하고 `warehouse-backup` 을 안내한다. 카탈로그가 든 디렉터리를
`--warehouse` 로 알려 주지 않으면 그것도 거부한다(카탈로그의 규칙대로 복사하기 위해).

**같은 시점이라는 것의 범위.** 저장소마다 SQLite 온라인 백업을 하는 것만으로는 전체가 같은
시점이 되지 않는다 — 두 저장소를 복사하는 사이에 끝난 빌드는 한쪽에서만 끝나 있다. 그래서
백업은 **아무것도 쓰지 않을 때만** 돈다. 상태를 쓰는 명령(`serve` 는 떠 있는 내내, `build`,
`rebuild-index`, `prune-cancelled`, `prune-runs`, `delete-user`, `warehouse-gc`, `warehouse-hold`)은 출력 디렉터리와 웨어하우스의
`.state.lock` 을 공유 잠금으로 쥐고, `backup`·`restore` 는 배타 잠금을 기다리지 않고 잡는다. 하나라도
쥐고 있으면 백업은 거부하고("in use"), 백업·복구 중에는 쓰는 명령이 시작하지 않는다. 잠금은
`flock` 이라 프로세스가 어떻게 끝나든 커널이 푼다. 그러므로 백업 파일은 **출력 디렉터리의 한
시점**이다. 인증 서버(Keycloak)의 데이터베이스, 환경 변수와 compose 설정, 출력 디렉터리 밖의
것은 들어 있지 않다.

**비밀은 저장된 그대로.** provider 키 저장소는 SQLite 가 가진 그대로(키마다
`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` 로 암호화된 채) 들어간다. master key 는 백업에 넣지 않고,
읽지도 출력하지도 않는다. 백업 파일의 `backup.json` 이 그 변수 이름만 적어 둔다 — 복구한 곳에서
같은 키를 설정해야 키를 읽을 수 있다. master key 는 백업 파일과 다른 곳에 보관한다.

**백업 파일.** 압축하지 않은 tar 하나다(산출물이 Parquet 이라 더 줄지 않는다).

| 경로 | 내용 |
|---|---|
| `output/<경로>` | 저장소마다 SQLite 온라인 백업으로 뜬 사본(저널 모드 DELETE), 그리고 아래 표의 파일 |
| `warehouse/` | 웨어하우스 백업(`warehouse-backup` 과 같은 형식) — 카탈로그와 읽을 수 있는 스냅샷 |
| `backup.json` | 형식, 릴리스, 시각, 멤버마다 SHA-256·크기, 저장소마다 스키마 버전, 빠진 것 |

파일은 대상 옆에 쓰다가 마지막에 이름을 바꾸고, 작업 사본도 대상 옆(또는 `--work-dir`)에 둔다.
실패하면 끝난 것처럼 보이는 파일이 남지 않는다.

**무엇이 들어가는지 — 목록.** 저장소는 §6.0.1 의 열 개, 그 밖의 경로는 아래 표다. 목록의 정본은
`src/kpubdata_builder/store/inventory.py` 의 `OUTPUT_PATHS` 이고, 출력 디렉터리에 **두 목록 어디에도
없는 것이 있으면 백업은 그 경로를 적고 거부한다** — 아무도 상태인지 아닌지 정하지 않은 것을 빼고
받은 백업은 불완전한 백업이기 때문이다. `tests/unit/test_state_backup.py` 가 코드가 실제로 만드는
경로와 `.service/` 아래에 붙이는 이름을 이 목록에 대조하고, 이 표에 모든 항목이 있는지 확인한다.

| 경로 | 백업 | 무엇인가 |
|---|---|---|
| `[!._]*` (디렉터리) | 담는다 | run 작업 공간 — run 마다 하나. manifest(정본), BuildSpec 사본, Bronze·Silver·Gold 산출물 |
| `.service/uploads.sqlite3.blobs` | 담는다 | 큰 업로드의 내용. 업로드 저장소의 행이 `blob_path` 와 SHA-256 으로 가리킨다 |
| `.service/exports` | 담는다 | 내보내기 기록과 묶음 |
| `.service/deleted-users` (디렉터리) | 담는다 | 사용자 삭제 기록(tombstone)과 그 journal(§6.4). 복구는 지금의 것을 복원하는 상태로 옮긴다 |
| `.state.lock` | 뺀다 | 백업·복구 중 쓰기를 막는 잠금 파일 |
| `.readyz-*` | 뺀다 | `/readyz` 가 잠깐 만드는 파일 |
| `_builds.sqlite.bak*` | 뺀다 | `rebuild-index` 가 바꾸기 전의 인덱스 |
| `_builds.sqlite.tmp*` | 뺀다 | `rebuild-index` 가 쓰는 도중의 인덱스 |
| `.before-restore-*` (디렉터리) | 뺀다 | `restore` 가 옮겨 둔 그 전 상태 |
| `.run-gc` (디렉터리) | 뺀다 | `prune-runs --apply` 가 지우려고 옮겨 둔 run(§6.3). 중단됐으면 다음 `prune-runs --apply` 가 마저 지운다 |

저장소 옆의 `-wal`·`-shm`·`-journal` 은 복사하지 않는다 — 온라인 백업이 그 내용을 담는다. 웨어하우스
안에서는 카탈로그가 정한다: 읽을 수 있는 스냅샷(committed, quarantined)만 담고 lease 와 staging 은
뺀다(`warehouse/backup.py`).

**복구 — 검증, staging, 교체.** 복구는 먼저 모든 멤버의 checksum, 저장소마다 SQLite
`integrity_check` 와 기록된 스키마 버전, 웨어하우스의 카탈로그와 스냅샷 대조, 업로드 행과 파일의
대조를 한다. 하나라도 실패하면 아무것도 옮기지 않고 모든 이유를 적어 거부한다. **이 릴리스보다 새로운
버전의 저장소가 있으면 거부한다**(§6.0 — 그 저장소를 쓴 릴리스로 복구한다). 오래된 버전은 받아들이고,
저장소가 열릴 때 앞으로 마이그레이션된다. 검증을 통과하면 출력 디렉터리 안의 `.restoring/` 에 풀고
(`/data` 는 마운트 지점이라 통째로 이름을 바꿀 수 없어서 같은 파일시스템 안에 둔다), 이름 바꾸기만으로
교체한다: 지금의 내용을 `.restoring/previous/` 로, 새 내용을 제자리로. 각 단계 전에 journal 을 쓴다.
끝나면 그 전 내용은 `.before-restore-<시각>/` 에 남는다 — **덮어쓰거나 지우는 것은 없다.** 확인한 뒤
운영자가 지운다(그동안 디스크를 두 배 쓴다). 마지막으로 빌드 인덱스를 manifest 에서 다시 만든다
(`rebuild-index` 와 같다).

**복구가 도중에 죽으면.** 풀던 중이면 출력 디렉터리는 그대로이고 `.restoring/` 만 남는다(다음
`backup` 이 그것을 보고 거부한다). 교체하던 중이면 journal 이 남고, `serve` 와 다른 쓰는 명령은
"interrupted" 를 말하며 시작하지 않는다 — 두 상태가 섞인 디렉터리로 뜨지 않게. `restore --recover`
가 journal 을 읽어 그 전 상태를 그대로 되돌리고, 다시 `restore` 를 해도 먼저 그렇게 한다.
테스트는 복구를 별도 프로세스에서 돌리다 교체 도중(옮겨 내던 중, 옮겨 넣던 중)과 풀던 중에 죽이고,
`--recover` 뒤의 출력 디렉터리가 바이트 단위로 그 전과 같은지 확인한다.

**정하지 않은 것.** 백업 주기, 보존 기간, 외부의 암호화된 보관소(예: restic)로 올리기와 그 뒤의
정리(prune), RPO·RTO 는 메인테이너가 정한다. 이 명령은 요청받을 때 받고, 받으라는 곳에 둘 뿐이다.
깨끗한 환경에서의 복구 리허설 기록, 인증 서버 데이터베이스의 백업과 복구도 아직 없다(#1095).

**복구는 삭제된 사용자를 되살리지 않는다(#1102).** 백업을 받은 뒤에 `delete-user` 로 지운 사용자는
그 백업 안에 아직 있다. 그래서 복구는 교체하기 전에 **지금의** 삭제 기록(`.service/deleted-users/`)을
복원하는 상태에 덮어 옮기고, 교체한 뒤 같은 배타 잠금 안에서 그 기록과 복원된 저장소들을 대조한다 —
기록의 subject 는 사용자 id 의 해시이므로, 저장소에 남은 owner id 를 같은 방식으로 해시해 맞춘다.
맞는 사용자가 있으면 그 삭제를 다시 열고 그 자리에서 다시 실행한다. 서버가 뜨기 전에 끝나므로
복원된 상태로 그 사용자가 다시 보이는 순간이 없다. 무엇이 막아 끝내지 못한 삭제는 출력에 적히고,
서버는 여전히 그 사용자를 거부하며, `delete-user --resume` 이 끝낸다. 백업 파일 자체의 보존 기간 —
삭제 전에 받은 백업을 언제 지우는가 — 은 정해지지 않았다(아래 §6.4).

### 6.3 run 보관기한과 저장량 (#1099)

빌드는 run 마다 작업 공간(`/data/<run_id>/`)을 남기고, 지금까지는 아무것도 그것을 지우지 않았다 —
취소된 partial run 만 `prune-cancelled` 가 정리한다. 성공·실패한 run 은 디스크에 계속 쌓이고,
디스크가 차면 모든 사용자의 빌드와 조회가 함께 실패한다. 여기서는 세 가지를 둔다.

**정하지 않은 것 — 모든 수치는 기본이 끔이다.** 보관기한, 사용자별 저장량 상한, 예약 크기,
빌드에 필요한 여유 공간은 사용자 약관·삭제 정책·백업 보존 정책과 맞춰 메인테이너가 정한다
(kpubdata#812). 그 결정 전까지 이 릴리스는 아무 run 도 지우지 않고 아무 빌드도 거절하지 않는다.
이슈에 나온 30일은 예시이지 정책이 아니다. 설정은 `docs/deployment.md` 의 설정 표에 있다.

| 설정 | 하는 일 | 기본 |
|---|---|---|
| `KPUBDATA_BUILDER_RUN_RETENTION_DAYS` | `prune-runs` 가 지우는 run 의 나이(일) | 끔 |
| `KPUBDATA_BUILDER_RUN_STORAGE_MAX_BYTES` | 다중 사용자 배포에서 사용자 한 명의 끝난 run 합계 상한 | 끔 |
| `KPUBDATA_BUILDER_RUN_STORAGE_RESERVE_BYTES` | 대기·실행 중인 build 하나가 미리 차지하는 크기 | `0` |
| `KPUBDATA_BUILDER_BUILD_MIN_FREE_MB` | build 를 받는 데 필요한 여유 공간 | 끔 |

**1) 기한이 지난 run 정리 — `prune-runs`.** 기본은 dry run 이라 아무것도 지우지 않고, 지울 run
마다 상태·끝난 시각·크기를, 남길 run 마다 이유를 적고, 마지막 줄에 지우면 비는 크기를 말한다.
`--apply` 를 줄 때에만 지운다. 같은 명령을 다시 돌려도 안전하다 — 이미 지운 run 은 더 없다.

```bash
# 무엇이 지워질지 본다 (아무것도 바꾸지 않는다)
docker compose -f docker-compose.prod.app.yml exec builder \
  kpubdata-builder prune-runs --output-dir /data --warehouse /data/warehouse --retention-days 30
# 지운다
docker compose -f docker-compose.prod.app.yml exec builder \
  kpubdata-builder prune-runs --output-dir /data --warehouse /data/warehouse --apply
```

`serve` 가 떠 있어도 돌릴 수 있다. 정기 GC 는 이 명령을 호스트의 cron 이나 systemd timer 로
돌리는 것이다(주기도 메인테이너가 정한다). 다른 쓰는 명령처럼 상태 잠금(§6.2)을 공유로 쥐므로
백업·복구 중에는 시작하지 않고, 이 명령이 도는 동안 백업은 거부된다.

지우는 run 은 manifest 의 `finished_at` 이 기한보다 오래된 **성공·실패** run 이다. 파일의 수정
시각으로는 판단하지 않고, `finished_at` 이 없는 run 은 남긴다(`no_finish_time`). 취소된 run 은
`prune-cancelled` 의 정책을 따른다. 기한이 지났어도 다음 run 은 지우지 않고 이유를 적는다.

| 이유 | 무엇인가 |
|---|---|
| `running` | run 이벤트에 제출·시작이 있고 끝이 없다 |
| `retry_source` | 아직 끝나지 않은 run 이 이 run 을 재시도한다(checkpoint 를 이어받을 수 있다, #1103) |
| `published` | 게시 영수증이 이 run 을 가리킨다(상태와 무관하게) |
| `held` | 이 run 의 warehouse snapshot 에 hold 가 있다(`warehouse-hold`) |
| `leased` | 이 run 의 warehouse snapshot 을 지금 질의가 읽고 있다 |
| `snapshot` | 이 run 이 만든 committed warehouse snapshot 이 있다 — 내보내기가 그 run 의 BuildSpec 을 읽는다 |

warehouse snapshot 은 `warehouse-gc` 가 정리한다. 그 run 의 snapshot 이 회수된 뒤의 `prune-runs` 가
run 을 지운다. 그래서 `--warehouse`(또는 `KPUBDATA_BUILDER_WAREHOUSE`)를 반드시 준다 — 주지 않으면
snapshot 의 참조를 볼 수 없다. 참조는 run 마다 지우기 직전에 다시 읽으므로, 명령이 도는 사이에
들어온 게시나 재시도는 그 run 을 지킨다.

**삭제의 순서와 중단.** run 하나를 지우는 것은 세 단계다: 작업 공간을 `/data/.run-gc/` 로 이름을
바꾸고(한 번에 출력 디렉터리에서 빠진다), 빌드 인덱스의 행을 지우고, 파일을 지운다. 어느 단계에서
죽든 다음 `prune-runs --apply` 가 `.run-gc/` 에 남은 것의 인덱스 행과 파일을 먼저 마저 지운다(dry
run 은 그것을 "would finish deleting" 으로 알린다). 인덱스 행이 지워지지 않았으면 manifest 에서
인덱스를 다시 만든다(`rebuild-index` 와 같다). run 의 이벤트 타임라인(`_build_events.sqlite`)은
지우지 않는다. 사용자와 그 데이터의 삭제는 §6.4 의 `delete-user` 가 한다.

**2) 사용자별 저장량 — 예약하고 받는다.** 다중 사용자 배포에서 `KPUBDATA_BUILDER_RUN_STORAGE_MAX_BYTES`
를 정하면 `POST /build` 와 `POST /builds` 가 실행하거나 큐에 넣기 전에 그 사용자의 끝난 run 작업 공간 합계를 잰다. 빌드의
크기는 끝나야 알 수 있으므로 대기·실행 중인 그 사용자의 build 마다
`KPUBDATA_BUILDER_RUN_STORAGE_RESERVE_BYTES` 를 예약한 것으로 치고, 합계와 예약(이번 것 포함)이 상한을
넘거나 합계가 이미 상한에 닿았으면 409 `run_storage_quota_exceeded`(`limit_bytes`, `used_bytes`)로
거절한다. 동기 실행도 끝날 때까지 같은 잠금 아래 예약에 포함되므로 동시에 들어온 두 요청이 마지막 자리를 함께
가져가지 못한다. 업로드는 따로 한도가 있고(#1045) run 작업 공간 밖에 있어 두 번 세지 않는다.
warehouse snapshot 도 세지 않는다. 실행 중인 build 가 예약보다 크게 자랄 수는 있다 — 예약은 상한을
넘는 제출을 막는 장치이지 정확한 회계가 아니다. 거절된 run 은 아무것도 기록되지 않는다.

**3) 디스크가 부족하면 받지 않는다.** `KPUBDATA_BUILDER_BUILD_MIN_FREE_MB` 를 정하면, 출력 디렉터리의
파일시스템 여유 공간이 그보다 작을 때 `POST /build` 와 `POST /builds` 가 503 `disk_space_low` 로 답한다. 여유 공간은
`/readyz`(§7.1)와 같은 방식으로 잰다. 응답은 경로도 크기도 말하지 않는다. 두 거절 모두 `/metrics` 의
응답 수(상태·코드별)에 나타난다. 사용자별 동시 build 개수 상한(#1189)은 계속 비동기 접수에만 적용된다.

**하지 않는 것.** `VACUUM`, export·spill·staging 디렉터리의 정리, 기한이 지난 업로드의 일괄 정리,
디스크가 가득 찼을 때의 aggressive 모드는 이 명령에 없다 — 이슈의 `maintain` 명령 범위다(#1099).

### 6.4 사용자와 그 데이터의 삭제 (#1102)

삭제 요청을 받은 사용자 한 명과 그 데이터를 운영자가 지운다. 사용자가 직접 탈퇴하는 화면과 API 는
아직 없고, 이 명령이 실제 요청을 처리하는 절차다.

```bash
# 무엇이 지워질지 본다 — 아무것도 쓰지 않는다(삭제 기록도)
docker compose -f docker-compose.prod.app.yml exec builder \
  kpubdata-builder delete-user oidc:... --output-dir /data --warehouse /data/warehouse
# 지운다. 그 사용자의 빌드가 끝나기를 최대 60초 기다린다
docker compose -f docker-compose.prod.app.yml exec builder \
  kpubdata-builder delete-user oidc:... --output-dir /data --warehouse /data/warehouse --apply --wait 60
# 중단되었거나 막혔던 삭제를 모두 이어서 끝낸다(사용자 id 없이)
docker compose -f docker-compose.prod.app.yml exec builder \
  kpubdata-builder delete-user --resume --output-dir /data --warehouse /data/warehouse
```

`USER_ID` 는 `GET /admin/users` 의 `user_id` 다. `oidc:` 로 시작하지 않는 id, 관리자
(`KPUBDATA_BUILDER_ADMIN_SUBJECTS`), 이 출력 디렉터리에 아무것도 없는 id 는 거부한다 — 오타가 영구한
차단이 되지 않게. 다른 쓰는 명령처럼 상태 잠금(§6.2)을 공유로 쥐므로 `serve` 가 떠 있어도 돌릴 수
있고, 백업·복구 중에는 시작하지 않는다. 같은 출력 디렉터리에서 삭제는 한 번에 하나만 돈다.

**먼저 막는다.** `--apply` 가 처음 쓰는 것은 삭제 기록(tombstone)이다:
`.service/deleted-users/<subject>.json`. 그 순간부터 서버는 그 사용자의 모든 요청을 403
`signup_rejected` 로 거부한다 — 가입 원장을 읽기 전에 거부하므로, 다시 로그인해도 원장에 새로 기록되지
않는다(trust-issuer 재등록으로 되살아나지 않는다). 그 사용자의 빌드도 막는다:

| 빌드의 상태 | 서버가 하는 일 |
|---|---|
| 새로 제출 | 아무것도 기록하지 않고 거부한다 |
| 삭제가 시작되는 순간에 제출되던 중 | 제출을 기록한 뒤 다시 확인해 바로 취소한다 — 삭제가 그 run 을 보거나, 그 확인이 삭제를 본다 |
| 대기 중 | worker 가 꺼낼 때 시작하지 않고 취소한다 |
| 실행 중 | 다음 단계 경계에서 취소한다. 삭제된 사용자의 빌드는 정상 종료로 확정(commit)할 수 없다 |

삭제는 그 사용자의 끝나지 않은 run 이, 그리고 끝나야 주인을 알 수 있는 run(동기 `POST /build`)이 모두
끝나기를 `--wait` 초까지 기다리고, 그래도 남으면 run id 를 적고 거부한다. 서버를 내리는 것도 방법이다 —
종료 drain 이 대기 중인 job 을 끝내고 실행 중인 것을 취소한다(§7).

**그다음 단계별로 지운다.** 순서대로, 각 단계는 여러 번 실행해도 결과가 같다.

| 단계 | 지우는 것 |
|---|---|
| `analyses` | 저장한 분석. 그 분석의 snapshot hold 를 먼저 푼다 |
| `exports` | warehouse 내보내기 기록과 묶음 |
| `warehouse` | 그 사용자 workspace 의 테이블과 모든 snapshot(파일, 카탈로그 행), 그리고 다른 workspace 에 그 사용자가 만든 snapshot |
| `runs` | run 작업 공간(`.run-gc/` 로 옮긴 뒤 지운다, §6.3), 빌드 인덱스 행, run 이벤트·제출 기록·종료 기록 |
| `uploads` | 업로드한 파일과 그 행 |
| `provider_keys` | 저장한 provider 키(복호화하지 않으므로 master key 가 필요 없다) |
| `provider_tests` | provider 연결 테스트의 마지막 결과 |
| `ledger` | 가입 원장의 행(검증된 이메일) |
| `verify` | 모든 저장소에서 그 사용자를 다시 찾는다. 없어야 삭제가 완료된다 |

다음은 지우지 않고 멈추며, 무엇이 막는지 적는다: hold(`audit`, `retention` — `warehouse-hold release`
로 풀거나 기한이 지나야 한다)나 lease(지금 읽는 질의)가 있는 snapshot, 다른 사용자와 함께 쓰는
workspace 에서 현재 snapshot 인 것. 한 단계가 막히면 그 뒤 단계는 하지 않는다. 지워진 데이터는 그대로
지워져 있고, 삭제 기록은 미완료로 남아 서버는 계속 그 사용자를 거부한다. 원인을 해결하고 같은 명령을
다시 돌리면 모든 단계를 처음부터 다시 실행해 남은 것만 지운다. 마지막 확인 뒤에 들어온 것을 잡으려고
데이터 단계는 한 번 더 돈다.

**중단되면.** 각 단계는 끝날 때마다 journal 에 기록된다. 어느 단계에서 죽든 `delete-user --resume` 이
미완료 삭제를 모두 다시 실행한다. journal 에는 사용자 id 가 없으므로, 저장소에 남은 owner id 를 해시해
subject 와 맞춰 찾는다. 남은 것이 하나도 없으면 그대로 완료로 기록한다.

**감사 기록에는 사람이 없다.** journal 과, 단계마다 관리자 감사 로거(`kpubdata_builder.admin_audit`)에
남는 한 줄(`user deletion: subject=… step=… outcome=… removed=…`)은 사용자 id 가 아니라 **subject**
— 사용자 id 를 고정 라벨과 함께 SHA-256 한 값 — 만 담는다. subject 로 id 나 이메일을 되찾을 수 없고,
id 를 이미 가진 사람만 맞춰 볼 수 있다(복구가 그렇게 대조한다). 단계가 지운 개수와 막은 이유(run id,
snapshot id, 테이블 id)는 담지만 키, 파일 내용, 이메일은 담지 않는다. 이 기록을 얼마나 보관할지는
삭제 정책과 함께 메인테이너가 정한다.

**지우지 않는 것, 정해지지 않은 것.**

- **인증 서버(Keycloak)의 계정.** 이 명령은 Builder 의 데이터만 지운다. IdP 계정을 지우는 절차는
  어떤 IdP 를 쓸지(#1124)와 함께 정한다. 그 전까지 운영자가 Keycloak 관리 화면에서 따로 지운다. 지우지
  않아도 삭제 기록이 그 계정의 로그인을 거부한다.
- **삭제 전에 받은 백업.** 복구는 삭제를 다시 적용하지만(§6.2), 백업 파일 안에는 그 사용자가 남아 있다.
  백업 보존 기간은 메인테이너가 정한다(#1095).
- **외부에 게시한 것.** Hugging Face·Kaggle 등에 게시한 데이터셋은 지우지 않는다. 게시 영수증
  (`_publish_receipts.sqlite`)은 무엇이 어디로 나갔는지의 기록이라 남긴다.
- **문서 revision 의 작성자 표시와 브라우저에 저장된 데이터.** revision 이력(`author`)은 지우지 않는다.
  Studio 가 브라우저에 둔 것은 사용자가 브라우저에서 지운다.

## 7. 헬스체크·종료

- `GET /healthz` — 무인증 liveness probe (#372). 프로브가 API 키를 못 실을 때 사용.
- `Dockerfile` `HEALTHCHECK` — urllib로 `/healthz` 폴링 (#372).
- `SIGTERM` — 우아한 종료(진행 중 요청 drain, #374). 컨테이너를 내릴 때(`docker stop`,
  compose 의 재배포) 받는 신호다.
  비동기 빌드는 이렇게 끝난다(#1118). 대기 중인 작업은 시작하지 않고 `failed`
  (`interrupted: …`)로 끝나며 키를 버린다 — 가져오거나 쓴 것이 없으므로 새 `run_id` 로 다시
  제출하면 된다. 그 뒤의 제출은 503 `shutting_down` 이다. 실행 중인 빌드는
  `KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS`(기본 90초) 동안 끝나기를 기다리고, 그때까지 남은
  빌드에는 사용자의 취소와 같은 방식으로 멈추라고 한 뒤 10초를 더 준다(다음 단계 경계에서
  `cancelled` 로 끝나고 이벤트가 이유를 적는다). **컨테이너의 종료 대기 시간은 그 합보다 길게**
  둔다 — compose 의 `stop_grace_period: 120s`. Docker 의 기본값 10초로는 실행 중인 빌드가
  SIGKILL 로 끊긴다. 그렇게 끊긴 run 은 manifest 없이 남고, 다음 기동 때 `failed` 로
  표시된다 — 다중 사용자 배포는 `credentials_required` 로(#683), 단일 사용자 배포는 키가
  남아 있으므로 `interrupted: …` 오류만으로(#1120). 둘 다 종료 기록의 code 는 `interrupted` 다.
  이 동작은 `scripts/shutdown_drain_smoke.py` 가 `Docker` 워크플로에서 실제 compose 와
  이미지로 확인한다: 빌드 하나가 실행 중이고 둘이 대기 중일 때 `docker compose stop` 으로
  내리고(유예 시간이 기본값일 때와 1초일 때), SIGKILL 로도 끊은 뒤, 같은 `/data` 로 다시 올려
  각 run 의 상태를 읽는다. 확인하지 않는 것: provider 에서 가져오는 빌드, 빌드 둘이 동시에
  실행 중인 경우, 작업의 키가 메모리에서 지워졌는지.

### 7.1 liveness 와 readiness 의 분리, 메트릭 (#1101)

세 가지를 따로 본다.

| 질문 | 어디서 | 인증 | 실패의 뜻 |
| :--- | :--- | :--- | :--- |
| 프로세스가 살아 있나 (liveness) | `GET /healthz` | 없음 | 응답이 없다. 파일을 건드리지 않는다 |
| 지금 일을 할 수 있나 (readiness) | `GET /readyz` | 없음 | 503 `not_ready`, 본문의 `checks` 가 어느 검사인지 말한다 |
| build 를 더 받을 수 있나 | `GET /readyz` 의 `accepting_builds` | 없음 | `false` 여도 `status` 는 `ready` 로 남는다 |

`/readyz` 가 보는 것: `state_store`(build index 를 읽을 수 있나, 쓰지는 않는다), `output_dir`(출력
디렉터리에 파일을 만들고 지울 수 있나 — SQLite 저장소와 그 저널이 쓰이는 곳),
`disk`(그 파일시스템의 여유 공간이 `KPUBDATA_BUILDER_READY_MIN_FREE_PERCENT`(기본 15%)와
`KPUBDATA_BUILDER_READY_MIN_FREE_MB`(기본 1024MB) 이상인가, 하나라도 못 미치면 `low`),
`workers`(build worker 가 멈추지 않았나, 종료 중이면 `draining`). 값은 `ok` 이외에
`failed`·`timeout`·`low`·`draining`·`stopped` 다. 두 임계값은 이슈가 제안한 출발값이며 측정해서
정한 값이 아니다.

정책:

- **큐가 가득 찬 것은 장애가 아니다.** 로그인·취소·조회는 그대로 되므로 `status` 는 `ready` 이고
  `accepting_builds` 만 `false` 다.
- **JWKS(토큰 서명 검증용 공개키)는 readiness 에서 보지 않는다.** 검사마다 IdP 를 부르게 되기
  때문이다. 키를 얻지 못해 판정할 수 없는 요청은 그 요청이 503 `auth_unavailable` 로 답한다.
  캐시가 유효한 동안의 IdP 장애, 처음 기동, 모르는 `kid`, 캐시 만료를 구분해 알리는 일은 아직
  남아 있다.
- **schema migration** 은 기동 시점의 일이다. 이 릴리스가 읽지 못하는 저장소는 기동을 막는다(§6.0).
  떠 있는 프로세스는 그것을 이미 통과했다.
- **부하 제한.** 검사 결과는 5초 동안 재사용되고, 그 사이에 온 요청은 하나의 계산을 함께 기다린다.
  파일시스템 검사는 2초 안에 끝나지 않으면 `timeout` 이고, 그 검사가 아직 멈춰 있으면 새로 시작하지
  않는다. 응답에는 경로·크기·오류 문구를 넣지 않는다.

compose 의 healthcheck 와 Caddy 의 `health_uri` 는 **계속 `/healthz`** 를 본다. 디스크가 찼다고
Caddy 가 upstream 을 빼면 취소와 조회까지 모든 요청이 실패한다. 또 Docker 는 컨테이너가 `unhealthy` 가
되어도 `restart: unless-stopped` 만으로는 재시작하지 않고, `depends_on: service_healthy` 는 시작
순서만 정할 뿐 실행 중 장애를 복구하지 않는다. `/readyz` 는 외부 감시가 부르는 주소다:
`https://<APP_DOMAIN>/readyz`.

`GET /metrics` 는 Prometheus 텍스트 형식(0.0.4)이고 **관리자만** 읽는다 — 서비스 API 키 또는
관리자의 Bearer 토큰. 다른 사용자는 403 이다. 내용: `kpubdata_builder_builds_queued`,
`_builds_running`, `_build_workers`, `_build_queue_limit`, `_accepting_builds`, `_ready`,
`_readiness_check{check}`, `_disk_free_bytes`, `_disk_total_bytes`(게이지),
`_http_responses_total{status,code}`(429 와 5xx 포함, `code` 는 Builder 의 고정 어휘 또는 `other`),
`_build_duration_seconds{mode,status}`, `_build_queue_wait_seconds`(히스토그램). 라벨에 경로·run id·
소유자·오류 문구는 들어가지 않는다. 카운터는 프로세스가 다시 뜨면 0 부터 센다.

```bash
curl -s https://<APP_DOMAIN>/readyz
curl -s -H "X-API-Key: $KPUBDATA_BUILDER_API_KEY" https://<APP_DOMAIN>/metrics
```

**아직 하지 않은 것(#1101):** 외부 감시 서비스의 계정, 알림 수신자와 당직 절차, 알림 임계값,
장애 주입 리허설, Studio·Keycloak 의 합성 검사와 로그인 여정 검사, nightly·restore-test 의
deadman 은 메인테이너의 결정과 운영 계정이 필요해 이 저장소의 변경만으로는 끝나지 않는다.

## 8. 동시성·풀·백프레셔

단일 Builder process 안에는 역할이 다른 실행 제한이 세 개 있다. 숫자가 같더라도 하나의
공유 pool이 아니며 서로 대신하지 않는다.

| 계층 | 구현 | 기본값 | 포화 시 동작 |
| :--- | :--- | :--- | :--- |
| HTTP 요청 | 별도 `ThreadPoolExecutor` (`kpubdata-http`) | service 기본 10, ACA template 4 | executor의 무제한 내부 queue에서 대기. 명시적 `429`가 아니므로 앞단 ingress timeout/connection limit가 필요 |
| 비동기 build | 별도 in-process `ThreadPoolExecutor` (`kpubdata-build`) | `KPUBDATA_BUILDER_MAX_BUILDS`, 기본 2(HTTP worker 수가 더 작으면 그 수, #1245), queued job 10 | queued 상태가 10건이면 제출을 `429`로 거절. 실행 중 job은 queue 수에 포함하지 않음 |
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

### 8.1 사용자별 질의 빈도와 provider 별 호출 한도 (#1245)

위 상한은 모두 **서비스 전체**의 것이다. 다중 사용자 배포에서는 한 사용자가 질의를 연달아 보내면 자리가
날 때마다 그 사용자가 가져가 다른 사용자가 `429 query_busy` 를 받고, 한 사용자의 build 가 provider 를
많이 부르면 provider 가 서버 주소를 막아 모두가 막힌다. 두 한도가 이를 나눈다. 둘 다 **기본은 끔**이고,
같은 방식으로 센다: 60초 안의 어느 구간에도 한도보다 많은 일이 시작되지 않는다(sliding window). 상태는
프로세스 메모리에만 있어 재시작하면 잊는다.

| 한도 | 세는 단위 | 넘으면 |
| :--- | :--- | :--- |
| `KPUBDATA_BUILDER_QUERY_RATE_PER_MINUTE` | 사용자(owner) 한 명. `POST /query`, `/warehouse/query`, `/warehouse/rows`, `/warehouse/aggregate`, `/warehouse/exports` 를 합쳐서. 단일 사용자 배포에는 적용하지 않는다 | 질의를 해석·실행하기 전에 `429 query_rate_limited` 와 `retry_after_seconds`, 같은 수의 `Retry-After` 헤더 |
| `KPUBDATA_BUILDER_PROVIDER_CALLS_PER_MINUTE` | provider 하나. 모든 사용자·build·preview·probe 를 합쳐서. kpubdata 에서 받는 page 하나 또는 probe 의 dataset 하나가 한 번 | build 는 차례를 기다린다. preview 는 `429 provider_egress_limited` + `Retry-After`, probe 는 닿지 못한 dataset 을 `not_probed` 로 |

provider 호출은 kpubdata 가 하므로 Builder 는 HTTP 요청 하나하나를 보지 못한다. 첫 page 를 넘기기 전에
모든 page 를 요청하는 kpubdata dataset(#481)은 한 호출 안에서 한꺼번에 나가고, 그 몫은 뒤의 호출들이
기다린다 — 분 단위로는 한도가 지켜지지만 그런 dataset 의 한 호출 안에서는 지켜지지 않는다. provider 연결
테스트(`POST /providers/{provider}/test`)는 요청 하나에 한 번이고 세지 않는다.

429 를 돌려주는 다른 한도(`auth_throttled`, `probe_rate_limited`)도 이제 `Retry-After` 헤더를 함께
보낸다. 거절은 다른 응답처럼 요청 로그의 `code` 와 `/metrics` 의 `http_responses_total{status,code}`
에 나타난다(#1272).

**측정.** `scripts/measure_query_load.py` 가 질의 경로에 부하를 걸고, 본 것을 그 기계의 정보와 함께 JSON
으로 출력한다. 아래는 **로컬 한 번의 실행이고 배포를 대표하지 않는다** — 다른 작업이 함께 돌던 개발용
데스크톱(load average 약 1.6)에서, HTTP·프록시 없이 프로세스 안에서 쟀다. 배포에 맞는 수는 배포할
하드웨어와 설정에서 같은 스크립트를 돌려 정한다.

```text
$ uv run python scripts/measure_query_load.py --seconds 10
machine: Intel Core i5-9400F (6 cores), 31.3 GiB, Linux 5.15, Python 3.12.13, DuckDB 1.5.6
table: 200,000 rows, query: GROUP BY over 200 groups, KPUBDATA_QUERY_MAX_CONCURRENCY=2
clients  completed  query_busy  queries/min  p50 ms  p95 ms
1        33         0           193.1        310.7   313.9
2        62         0           369.3        324.9   327.3
4        62         400         369.5        324.6   327.1
8        62         1200        368.0        326.6   328.5
rate limiter: 3.76 µs per admission
```

질의 하나는 이 기계에서 약 0.31초이고 대부분이 child process 를 띄우는 시간이다. 처리량은 query 자리 수에서
멈추고(자리 2 개에 분당 약 370), 그보다 많은 클라이언트는 `query_busy` 를 받는다. 사용자별 한도를 정할 때는
배포에서 잰 분당 처리량을 동시에 질의하는 사용자 수로 나눈 값이 출발점이 된다.

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
thread(`KPUBDATA_DUCKDB_THREADS`)를 여럿 쓸 수 있으므로 `query_concurrency`를 vCPU 수처럼 간주하면 안 된다. 작은
인스턴스(1 vCPU/2 GiB)는 HTTP worker 4, async build worker 4,
query concurrency 1에서 시작한다. HTTP와 build는 같은 설정값을 받지만 서로 다른 pool이라
동시에 각각 4개까지 실행될 수 있다. 따라서 CPU/memory 중심 build를 많이 제출하는 환경에서는
이 크기만으로 안전하다고 가정하지 말고 working set과 throttling을 관찰해야 한다.

한 호스트에 동시에 존재할 수 있는 실행 단위 전체 — 각각 따로 설정되므로 **합**을 호스트와
대조해야 한다(#701):

| 실행 단위 | 상한 | 설정 |
| :--- | :--- | :--- |
| HTTP worker thread | 기본 10 | `KPUBDATA_BUILDER_MAX_WORKERS` / `serve --max-workers` |
| 동시 build (동기 `POST /build` + 비동기 job **합계**) | 기본 2(HTTP worker 수가 더 작으면 그 수), queued 10 | `KPUBDATA_BUILDER_MAX_BUILDS` / `serve --max-builds` (#1028, #1245) |
| 동시 preview | 기본 무제한(요청 스레드가 상한) | `KPUBDATA_BUILDER_MAX_PREVIEWS` / `serve --max-previews` (#1028) |
| build 하나 안의 source fetch thread | **build 당** 최대 4 (`_MAX_PARALLEL_SOURCES`) | 코드 상수 |
| query child process | 기본 2 | `KPUBDATA_QUERY_MAX_CONCURRENCY` |
| 사용자 한 명이 60초에 시작하는 질의 | **기본 끔**. 다중 사용자 배포에서만 적용. 넘으면 `429 query_rate_limited` + `Retry-After` | `KPUBDATA_BUILDER_QUERY_RATE_PER_MINUTE` (#1245) |
| provider 하나에 60초 동안 하는 호출(모든 사용자 합계) | **기본 끔**. build 는 기다리고, preview 는 `429 provider_egress_limited` + `Retry-After`, probe 는 `not_probed` | `KPUBDATA_BUILDER_PROVIDER_CALLS_PER_MINUTE` (#1245) |
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
`build_queue_full` 로 답한다(#1040). 자리가 나면 동기 build 도 job 으로 등록되어 요청 스레드에서 그 슬롯을
쥔 채로 실행된다(#1242) — 그래서 취소(`POST /builds/{run_id}/cancel`), 실행 시간 상한, 사용자별
동시 build 상한, 종료 시의 drain 이 비동기 job 과 똑같이 적용된다. 비동기 worker 수는 기존 `MAX_WORKERS` 를
따른다. 빌드가 거절할 spec·`url` source·provider 키 문제는 job 을 만들기 전에 답하므로 run 이 남지 않는다. 비동기 job 은 자리를 얻을 때까지 `queued` 로 남는다 — 기다리는 job 은 서비스 전체에서 `KPUBDATA_BUILDER_MAX_QUEUED_BUILDS`(기본 10)까지이고, 넘으면 `POST /builds` 가 429 `build_queue_full` 로 답한다(#1108). 동기 `POST /build` 도 job 으로 등록되는 동안 `queued` 로 세어지므로, 이미 슬롯을 잡았더라도 기다리는 job 이 그 한도만큼 있으면 같은 `build_queue_full` 로 거절된다(#1310). `KPUBDATA_BUILDER_MAX_PREVIEWS` 를 주면 preview 도 그 수까지만
함께 돌고 나머지는 기다린다. `MAX_BUILDS` 를 주지 않으면 2 이고, `MAX_WORKERS` 가 그보다 작으면 그
값이다(#1245). 그 전에는 `MAX_WORKERS` 값(기본 10)을 따랐다 — 아래 메모리 예산은 build 2 개로 계산돼
있는데 기본 설정으로는 10 개가 돌 수 있었고, 동기 build 10 개가 요청 스레드를 모두 잡을 수 있었다.
production compose 는 전부터 `MAX_BUILDS=2` 를 넘기므로 바뀌지 않는다. `MAX_WORKERS` 만 크게 설정해 두고
build 도 그만큼 돌리던 배포는 `KPUBDATA_BUILDER_MAX_BUILDS` 를 함께 적어야 한다.

**1 vCPU / 2 GiB 예시.** 동시 build 를 둘로 묶고 나머지를 맞춘다.

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
중단되면 child 도 멈춘다), 클라이언트가 떠남(#1245). 요청을 처리하는 동안 HTTP 계층이 그 연결을 지켜보고,
query engine 은 child 를 기다리는 사이 0.1초마다 클라이언트가 연결을 닫았는지 본다. 닫았으면 child 를 멈추고
query 자리와 메모리 예약을 돌려주며, 응답은 보내지 않는다(요청 로그와 `/metrics` 에는 상태 499). 연결을 닫은
것은 소켓이 끝(EOF)에 닿은 것으로 판단하므로, 요청을 보낸 뒤 쓰기 쪽만 닫는(half-close) 클라이언트는 떠난 것으로
보인다 — 브라우저·Studio·Caddy 는 그렇게 하지 않는다. Caddy 를 거치면 브라우저가 떠날 때 Caddy 가 upstream
연결을 닫는 것으로 전달된다. preview(`/query`)는 SQL 결과를 `limit + 1` 행에서 자르므로 전체 결과를 만들지
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

### 배포 중인 이미지의 정기 재스캔 (#1107)

위 게이트는 이미지를 **빌드할 때** 한 번 본다. 이미지는 아무도 다시 빌드하지 않아도 새
권고가 붙고, compose 가 다른 곳에서 받아 오는 이미지(Caddy)는 그 게이트가 본 적이
없다. `image-rescan.yml` 워크플로가 매일(04:41 UTC) `scripts/image_rescan.py` 를 돌린다.

| 항목 | 값 |
| :--- | :--- |
| 대상 | `docker-compose.prod.app.yml` 의 `image:` 전부. `${이름:-기본값}` 은 기본값으로 읽는다. 지금은 `ghcr.io/kpubdata-lab/kpubdata-builder:latest`, `caddy:2.11.6-alpine` |
| severity | `CRITICAL,HIGH`. **수정본이 없는 것도 포함한다** — 빌드 게이트(`ignore-unfixed: true`)와 다르다 |
| 결과 | 이미지마다 열린 이슈 하나(`fix(security): <이미지> has HIGH or CRITICAL vulnerabilities`, 라벨 `epic:distribution`·`security`). 스캔한 digest 와, 취약점마다 수정본이 나온 버전 또는 `no fix published` 를 적는다 |
| 중복 | 목록이 지난번과 같으면 아무것도 쓰지 않는다. 달라지면 같은 이슈의 목록을 고쳐 쓰고 무엇이 달라졌는지 댓글을 단다. 새 이슈를 또 열지 않는다 |
| 실패 | 이미지 하나라도 스캔하지 못했거나 이슈를 쓰지 못하면 워크플로가 실패한다. 스캔 실패를 "취약점 없음" 으로 적지 않는다 |

- **사람이 적는 부분.** 수정본이 있는지는 스캔이 안다. 그 코드에 실제로 닿을 수 있는지,
  영향이 무엇인지, 긴급 조치가 필요한지는 스캔이 알 수 없으므로 이슈의 `Triage` 에
  `not assessed` 로 두고 사람이 채운다. 재스캔은 목록 부분만 고쳐 쓰고 `Triage` 는 그대로
  둔다.
- **이슈를 닫지 않는다.** 취약점이 더 보고되지 않으면 이슈에 그렇게 적을 뿐이다. 배포가
  스캔한 digest 를 실제로 쓰고 있는지는 사람이 확인하고 닫는다.
- **실제로 돌고 있는 digest 를 스캔하려면** 워크플로를 수동 실행하면서 `images` 에
  `ghcr.io/kpubdata-lab/kpubdata-builder@sha256:...` 를 넣는다. 정기 실행은 compose 의
  기본 tag 가 지금 가리키는 것을 본다 — VM 이 예전 digest 를 계속 쓰고 있어도 워크플로는
  알 수 없다.
- **base 이미지 갱신**은 Dependabot 이 연다(`docker`: Dockerfile 의 `FROM`,
  `docker-compose`: compose 의 tag). 다른 변경과 같은 pull request 이고, `Docker`
  워크플로의 빌드·스모크·스캔을 거쳐 사람이 병합한다. 자동 병합은 켜지 않는다.
- base 를 고쳐 다시 빌드한 이미지는 **새 버전으로** 낸다. `docker.yml` 은 게시한 digest 를
  출력하고 SBOM 과 provenance 를 붙인다. 그 워크플로를 이미 나간 release tag 에서 수동
  실행하면 같은 tag 가 새 digest 로 다시 게시되므로 그렇게 하지 않는다 — 이것을 막는
  장치는 아직 없고, 승인 절차도 이 문서에 정해져 있지 않다.

## 12. 단일 VM 프로덕션 배포 (ADR 0017)

풀스택(Studio 프론트엔드 + Builder 백엔드)을 VM 한 대에 배포하는 참조 토폴로지는
[ADR 0017](./adrs/0017-fullstack-oci-deployment.md)에 정의되어 있다. 특정 클라우드를 전제하지 않는다
(kpubdata#812) — Docker Compose 가 도는 Linux VM 이면 된다. 핵심은 our-tax의
split-topology(별도 CUBRID DB VM)와 달리 **DB 서버 없이 단일 app VM + `/data` 볼륨**만
쓴다는 점이다 — Builder는 매니페스트(source of truth) + 파생 SQLite 인덱스를 파일로
영속화하기 때문이다(§6, ADR 0003/0010).

| 구성 요소 | 호스팅 | 산출물 |
| :--- | :--- | :--- |
| Studio(프론트엔드) | Cloudflare Pages 정적 배포, 또는 같은 VM 의 `--profile studio` | studio 저장소 (`VITE_BUILDER_API_URL`, 컨테이너에서는 `STUDIO_BUILDER_API_URL`=Builder 의 공개 주소) |
| Builder(백엔드) | VM 의 Docker + Caddy | `docker-compose.prod.app.yml`, `ops/caddy/Caddyfile` |
| 상태 | VM 에 붙인 디스크 `/data` | `builder-data` 볼륨 (네트워크 FS 금지, §6) |
| CI/CD | GitHub Actions → GHCR (이미지까지) | `.github/workflows/docker.yml` — VM 으로의 자동 배포는 없다(아래) |

배포 산출물:

- `docker-compose.prod.app.yml` — Builder + (opt-in) Caddy, Studio 스택. migration/ETL/DB 없음. 프로필은 아래
  "프로필과 기동 smoke" 절.
- `ops/caddy/Caddyfile` — Cloudflare → Caddy → `builder:8000` 리버스 프록시.
- `.env.app.example` — VM-local `.env` 템플릿(placeholder secret만). `.env`는 커밋 금지.
- 자동 배포 워크플로(`deploy.yml`)와 rollout 스크립트는 **제거됐다**(2026-10-05). 그 워크플로가
  대상으로 하던 VM 에 배포하지 않기로 했고, 그 워크플로는 `main` 에 머지될 때마다 실행돼 SSH 단계에서 실패하고 있었다.
  이미지는 `docker.yml` 이 GHCR 에 올린다. VM 에 올리려면 이 절의 compose 를 손으로 실행한다.
- 어떤 경로가 어떤 태그를 올리는지는 `docker.yml` 머리 주석에 있다. 요약하면 다음과 같다.
  - 풀 리퀘스트: 올리지 않는다.
  - `main` push: `main`, `latest`, `sha-…`.
  - 릴리스: `release.yml` 이 게이트를 통과한 커밋으로 `publish: true` 를 주어 부른다. 릴리스 풀 리퀘스트를 머지한 경우도 같다.
    이미지 빌드, 런타임 smoke, Trivy 스캔(수정판이 있는 HIGH/CRITICAL 이면 실패)이 **git 태그보다 먼저** 돈다(#1324).
    하나라도 실패하면 태그도 GitHub Release 도 만들어지지 않는다.
  - 그다음 `release.yml` 의 `tag` 작업이 레지스트리가 릴리스 태그로 내주는 digest 가 방금 올린 digest 와 같은지 확인하고
    그 digest 를 다시 스캔한 뒤에야 태그와 릴리스를 만든다. 릴리스 노트에 그 digest 가 적힌다. digest 가 없으면 이 작업이 실패한다(#1142).

VM 최초 준비. compose 파일, `ops/caddy/`, `.env.app.example` 이 필요하므로 저장소를 받아 그 안에서
실행한다. Builder 이미지 패키지는 비공개라서 먼저 GHCR 에 로그인한다 — 패키지 읽기(`read:packages`)
권한만 가진 토큰이면 된다.

```bash
git clone https://github.com/kpubdata-lab/kpubdata-builder.git && cd kpubdata-builder
echo "$GHCR_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
cp .env.app.example .env   # 값 채우고 chmod 600
docker compose -f docker-compose.prod.app.yml up -d            # IP-only
docker compose -f docker-compose.prod.app.yml --profile caddy up -d  # 공개 TLS 진입
docker compose -f docker-compose.prod.app.yml --profile caddy --profile studio up -d  # Studio 도 같은 VM 에
```

- **로그인하지 않으면 게시된 이미지가 아니다.** pull 이 `unauthorized` 로 실패하면 compose 는 멈추지 않고
  받아 둔 저장소에서 Builder 를 빌드해 `BUILDER_IMAGE` 의 이름(`ghcr.io/...:latest`)을 붙인다. 스택은
  뜨지만 그 이미지는 게시된 것이 아니다. `up` 의 출력에 `Pulling` 다음 `unauthorized` 와 `Building` 이
  보이면 로그인부터 다시 한다(kpubdata-studio#900 의 리허설에서 그렇게 됐다).
- `caddy` 프로필은 호스트의 80·443 을 쓴다. 다른 프로세스가 잡고 있으면 Caddy 가 뜨지 않는다.
- Studio 는 Bearer 토큰만 보낸다. `STUDIO_BUILDER_API_URL` 을 채우면서 `OIDC_ISSUER` 와
  `STUDIO_OIDC_CLIENT_ID` 를 비우면 Studio 는 로그인 화면에서 "인증 초기화에 실패했습니다" 로 멈춘다.
  인증 서버(#1124)가 아직 없으면 `STUDIO_BUILDER_API_URL` 도 비워 데모로 띄운다.

### 프로필과 기동 smoke (#692)

배포는 `docker-compose.prod.app.yml` 하나에서 시작한다. 프로필은 셋이고, 함께 켤 수 있다.

| 프로필 | 올라오는 것 | 이미지 |
| :--- | :--- | :--- |
| (없음) | Builder | `BUILDER_IMAGE`, 기본 `ghcr.io/kpubdata-lab/kpubdata-builder:latest` |
| `caddy` | + Caddy (Cloudflare → Caddy → Builder, 80/443) | `caddy:2.11.6-alpine` |
| `studio` | + Studio (nginx, `STUDIO_BIND` 기본 `127.0.0.1:8080`) | Studio 저장소의 고정된 커밋(`STUDIO_REF`)에서 빌드 |

- **Studio 는 pull 하지 않고 빌드한다.** Studio 는 아직 이미지를 게시하지 않고, 태그 `v0.4.0` 은 Studio 의
  Dockerfile 보다 오래됐다. 그래서 compose 는 `image:` 없이 Studio 저장소의 커밋 하나를 빌드한다. 첫 `up` 은
  Studio 를 빌드하느라 몇 분 걸린다. Studio 가 이미지를 게시하면 고정된 이미지로 바꾼다.
- **Studio 는 Builder 를 부르지 않는다. 사용자의 브라우저가 부른다**(§1). `STUDIO_BUILDER_API_URL` 은
  브라우저가 닿는 Builder 주소이고, Studio 페이지의 오리진은 `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 에 있어야 한다.
  둘 다 비우면 Studio 는 내장 데모 데이터로 뜬다.
- **Ask KPubData 는 기본이 꺼짐이다**(kpubdata-studio#933). `STUDIO_ASSISTANT_ENABLED` 가 `true` 가 아니면
  Studio 는 외부 AI 로 질문도 근거도 보내지 않고, 페이지의 Content-Security-Policy `connect-src` 에도 `https:`
  가 없다. 켜려면 `STUDIO_ASSISTANT_ENABLED=true` 와 함께 `STUDIO_ASSISTANT_ALLOWED_ORIGINS` 에 부를 수 있는
  https origin 을 공백으로 구분해 적는다(예: `https://api.openai.com`). 켜면서 비우거나 https 가 아닌 값을
  적으면 컨테이너가 이유를 밝히고 멈춘다. 사용자가 적은 주소는 이 목록 안에 있어야 한다.
- **`STUDIO_ACCOUNT_URL`** 은 계정 화면(비밀번호 변경·탈퇴)이 여는 로그인 서비스의 주소다. 비우면 Studio 가
  `<OIDC_ISSUER>/account` 를 쓴다.
- **`STUDIO_REF` 는 위 기능이 들어간 Studio 커밋 이상이어야 한다.** 그보다 앞선 커밋(예: 이전 기본값
  `023f0c3`)으로 빌드한 Studio 는 `ASSISTANT_*` 를 읽지 못해 사용자가 적은 아무 https 주소로 보낸다.
  `tests/unit/test_compose_profiles_smoke.py` 가 기본값이 그 아래로 내려가면 실패시킨다.
- **Keycloak 은 이 묶음에 없다.** 인증 서버를 직접 운영할지 관리형 서비스를 쓸지는 아직 정하지 않았다
  (#1124). 직접 운영하기로 하면 realm 설정, 영속 데이터베이스, SMTP, TLS 를 갖춘 프로필로 더한다.

`Docker` 워크플로가 둘을 확인한다(`scripts/compose_profiles_smoke.py`).

- **프로필별 기동** — 그 실행에서 빌드한 Builder 이미지로 각 프로필을 따로 올리고, 모든 서비스의 헬스체크가
  통과하기를 기다린 뒤 네트워크 안에서 묻는다. Builder 는 `/healthz` 200, `/version` 이 키와 함께 200, 키 없이
  401. `caddy` 는 Caddy 의 `/healthz` 와 Caddy 를 거친 `/version`. `studio` 는 페이지와 `/config.js`,
  `/config.js` 에 적힌 Builder 주소, 그리고 그 주소로 페이지의 오리진을 실어 보낸 호출이 200 이고 CORS 가
  그 오리진을 허용하는지. 같은 체인의 위조 헤더·보안 헤더·로그는 `proxy_chain_smoke.py` 가, `/data` 의
  보존은 `data_volume_smoke.py` 가, 종료는 `shutdown_drain_smoke.py` 가 본다 — 여기서 되풀이하지 않는다.
- **이미지 게시** — compose 파일(`docker-compose.prod.app.yml`)의 기본
  이미지가 레지스트리에 있는지 묻는다. #675 처럼 아무 워크플로도 올리지 않는 태그를 기본값으로 두면 여기서
  실패한다. Builder 의 패키지는 비공개라서 이 작업은 패키지 읽기 권한만 가진 토큰으로 로그인한다 — 로그인
  없는 VM 에서 `ghcr.io/kpubdata-lab/kpubdata-builder` 를 받으려면 그 VM 도 로그인해야 한다.

로컬에서 같은 것을 돌릴 수 있다. 호스트의 다른 Docker 네트워크가 `172.28.250.0/24` 와 겹치면
`APP_NET_SUBNET`, `APP_NET_IP_RANGE`, `CADDY_IPV4`, `KPUBDATA_BUILDER_TRUSTED_PROXIES` 를 환경변수로 함께 바꾼다.

```bash
docker build -t kpubdata-builder:ci .
python3 scripts/compose_profiles_smoke.py --image kpubdata-builder:ci                    # 세 프로필
python3 scripts/compose_profiles_smoke.py --image kpubdata-builder:ci --profile studio   # 하나만
python3 scripts/compose_profiles_smoke.py --published                                    # 게시 확인
```

이 smoke 가 보지 않는 것: 실제 인증 서버로의 로그인(OIDC), 공개 인증서를 쓰는 TLS, 문서만 보고 깨끗한 VM 에
올리는 리허설(kpubdata-studio#900), Studio 에서 시작해 빌드 산출물까지 가는 재생 기반 end-to-end 시험.

> fail-closed(§2, ADR 0006): 인증 수단이 하나도 없으면 컨테이너가 기동을 거부한다.
> `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`는 재기동 사이에 동일 값을 유지해야 한다(ADR 0012).

### compose 가 컨테이너에 넘기지 않는 설정 (#1108)

`docker-compose.prod.app.yml` 은 Builder 컨테이너에 넘길 환경변수를 하나씩 적는다. **거기 없는 설정은
`.env` 에 적어도 아무 일도 하지 않는다** — 값이 프로세스에 닿지 않고, 그 사실을 알리는 것도 없다.
아래 설정이 그렇다. 쓰려면 compose 파일의 `environment` 에 `이름: ${이름:-}` 줄을 더한다.

- `KPUBDATA_BUILDER_DEV_MODE` — 일부러 넘기지 않는다. 인증을 통째로 끄는 값이 프로덕션 스택에서
  `.env` 한 줄로 켜져서는 안 된다.

나머지는 지금 넘기지 않는다. 기본값이 있는 설정은 그 값으로 돈다.

- **기본값이 없는 것**(`HF_TOKEN`, `KAGGLE_USERNAME`, `KAGGLE_KEY`, `KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT`,
  `OIDC_JWKS_URL`, `KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS`)은 설정되지 않은
  것으로 돈다. `KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS` 가 없으므로 이 스택에서 `prune-cancelled --apply` 는
  `--ttl-hours` 를 직접 주지 않는 한 아무것도 지우지 않는다.
- **켜고 끄는 넷**(`ENFORCE_OWNERSHIP`, `KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL`,
  `KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL`, `KPUBDATA_BUILDER_REQUIRE_PII_SCAN_FOR_PUBLISH`)은 기본값이
  있다. `OIDC_ISSUER` 가 없는 단일 사용자 스택에서는 꺼져 있고, `OIDC_ISSUER` 를 설정한 다중 사용자 스택에서는
  `.env` 에 무엇을 적든 켜져 있다 — 다중 사용자 모드가 넷을 모두 강제한다(ADR 0012, #1113). 그래서 넘기지
  않아서 달라지는 것은 단일 사용자 스택에서 이 넷을 켤 수 없다는 점이다.
- **이 스택은 SQLite 로만 돈다.** `KPUBDATA_BUILDER_STORAGE_BACKEND` 와 `KPUBDATA_BUILDER_CUBRID_URL` 은 설정이
  아니라 제거된 설정이어서 이 목록에 없다(§6.1, #1094). 남아 있으면 Builder 가 시작하지 않는다.

넘기지 않는 설정 전체는 다음과 같다(`KPUBDATA_BUILDER_DEV_MODE` 는 위에 적었다).

- `KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS`
- `OIDC_JWKS_URL`
- `OIDC_JWKS_TTL`
- `ENFORCE_OWNERSHIP`
- `KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT`
- `KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL`
- `KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS`
- `KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL`
- `KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS`
- `KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS`
- `KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS`
- `KPUBDATA_BUILDER_CHECKPOINT_MAX_AGE_SECONDS`
- `KPUBDATA_BUILDER_MAX_UPLOAD_BYTES`
- `KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES`
- `KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT`
- `KPUBDATA_BUILDER_REQUIRE_PII_SCAN_FOR_PUBLISH`
- `HF_TOKEN`
- `KAGGLE_USERNAME`
- `KAGGLE_KEY`

이 목록은 `tests/unit/test_prod_compose_settings.py` 가 설정 목록(`settings_catalog.py`)과 compose 파일에
대조한다. 설정을 새로 만들면 compose 에 넘기거나 이 목록에 올려야 테스트가 통과한다.

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

### 인증에 성공하면 실패 기록을 비우는 이유 (#1074)

유효한 계정 하나를 가진 쪽은 성공하는 요청을 끼워 넣어 자기 주소의 실패 기록을 계속 비울 수
있다. 이 점을 알고 그대로 둔다. 결정과 근거는 다음과 같다.

- **비우지 않으면 한 주소를 함께 쓰는 사용자들이 서로를 막는다.** 식별자는 클라이언트
  주소이고, 사무실이나 학교처럼 여러 사용자가 한 주소로 나오는 곳이 있다. 성공이 기록을
  비우지 않으면 그 주소에서 누적된 실패가 창(기본 60초) 안에 한도에 닿을 때마다 정상
  사용자까지 `429` 를 받는다. 이 스로틀이 고치려던 문제(#1031)가 그대로 돌아온다.
- **비워서 얻는 것이 스로틀이 막으려는 두 가지에 해당하지 않는다.**
  - 정적 API 키 추측: 키는 32자 이상이어야 기동하고(예시 값은 거부한다) 무작위 값이므로,
    한도 없이 시도해도 맞힐 수 없다. 스로틀은 이 추측을 느리게 할 뿐 막는 장치가 아니다.
  - 무효 토큰의 서명 검증으로 CPU 를 쓰게 하기: 유효한 계정을 가진 쪽은 정상 요청(빌드,
    질의)만으로 그보다 훨씬 많은 자원을 쓸 수 있고, 그쪽은 사용자별 한도(동시 build 수,
    질의 메모리 예산)가 막는다.
- **성공한 요청은 누구의 것인지 남는다.** 요청 로그의 `owner` 로 그 계정을 가려낼 수 있고,
  관리자가 가입을 거부하거나 허용 목록에서 뺄 수 있다. 익명의 실패에는 이 수단이 없어서
  스로틀이 필요하다.

다시 검토할 조건: 인증 실패를 계정별로도 세게 되거나(지금은 주소별로만 센다), 짧은 API 키를
허용하게 되면 이 결정을 다시 본다.

### 리버스 프록시 뒤에서

기본 식별자는 TCP peer 주소다. `X-Forwarded-For`는 누구나 써 보낼 수 있으므로 기본으로는
읽지 않는다. 그래서 프록시 뒤에서는 모든 사용자가 프록시의 주소로 도착해 **한 버킷을
공유**한다 — 몇 사람의 실패가 모두를 429 로 막는다(#1031).

`KPUBDATA_BUILDER_TRUSTED_PROXIES` 에 프록시의 주소나 CIDR 블록을 콤마로 적으면, TCP
peer 가 그중 하나일 때에만 `X-Forwarded-For` 를 읽는다. 오른쪽부터 읽어 신뢰하는 프록시가
아닌 첫 주소를 클라이언트로 본다 — 프록시가 덧붙인 부분만 쓰고, 클라이언트가 직접 써
보낸 왼쪽 부분에는 닿지 않는다. 주소로 읽히지 않는 항목은 경고 로그(값이 아니라 몇 번째 항목인지)와 함께 버린다(호스트
이름은 쓸 수 없다).

#### 저장소의 구성: Cloudflare → Caddy → Builder (#1098)

`docker-compose.prod.app.yml` 을 `--profile caddy` 로 올리면 아래 구성이 된다. 따로 적을
값은 없다.

```text
클라이언트 ──▶ Cloudflare 엣지 ──▶ Caddy (80/443) ──▶ Builder (8000)
                X-Forwarded-For 에        클라이언트 주소를 정해        Caddy 의 주소에서 온
                자기가 본 주소를 덧붙임    X-Forwarded-For 를 다시 씀     요청의 헤더만 읽음
```

| 겹 | 신뢰하는 것 | 어디에 적혀 있나 |
|---|---|---|
| Caddy | Cloudflare 가 공개한 엣지 대역 | `ops/caddy/trusted_proxies.caddy` (Caddyfile 의 `servers` 블록이 import) |
| Builder | Caddy 컨테이너의 고정 주소 하나 | compose 의 `KPUBDATA_BUILDER_TRUSTED_PROXIES` 기본값 `172.28.250.2` = `CADDY_IPV4` 의 기본값 |

- **Caddy 는 peer 가 Cloudflare 대역일 때에만 `X-Forwarded-For` 를 읽는다.**
  `trusted_proxies_strict` 로 오른쪽부터 읽어 Cloudflare 가 덧붙인 주소를 고른다 —
  클라이언트가 직접 써 보낸 왼쪽 값에는 닿지 않는다. 그 밖의 peer 가 보낸 헤더는 읽지
  않고 peer 주소를 클라이언트로 본다.
- **Caddy 는 정한 주소 하나로 `X-Forwarded-For` 를 다시 쓴다**
  (`header_up X-Forwarded-For {http.vars.client_ip}`). Builder 가 받는 값은 항상 Caddy 가
  쓴 주소 하나다. `CF-Connecting-IP` 와 `Forwarded` 는 Caddy 도 Builder 도 읽지 않는다.
- **Builder 는 대역이 아니라 Caddy 의 주소 하나를 신뢰한다.** compose 네트워크의 대역
  (`APP_NET_SUBNET`)과 Caddy 의 주소(`CADDY_IPV4`)가 고정돼 있고, 주소를 지정하지 않은
  컨테이너는 `APP_NET_IP_RANGE` 에서 받으므로 다른 컨테이너가 Caddy 의 주소를 받지
  않는다. 네트워크를 다시 만들어도 주소는 같다. 호스트의 다른 네트워크와 겹치면 세 값을
  `.env` 에서 함께 바꾸고, `KPUBDATA_BUILDER_TRUSTED_PROXIES` 에도 새 `CADDY_IPV4` 를
  적는다 — 그 기본값은 `CADDY_IPV4` 를 따라 바뀌지 않는다.
- 이 구성은 `scripts/proxy_chain_smoke.py` 가 실제 compose·Caddyfile·이미지로 올려
  확인한다(`Docker` 워크플로). 위조한 `X-Forwarded-For`·`CF-Connecting-IP`·`Forwarded`
  가 반영되지 않는지, 한 클라이언트의 실패가 다른 클라이언트를 막지 않는지, Builder
  포트로 직접 보낸 위조 헤더가 무시되는지를 본다. CI 러너는 Cloudflare 대역에서 요청을
  보낼 수 없으므로, 그 검사에서는 `trusted_proxies.caddy` 를 시험용 컨테이너의 주소
  하나로 바꿔 넣는다. **실제 Cloudflare 를 거친 요청으로는 확인하지 않았다** — 첫 배포
  때 아래 "배포 후 확인" 을 한다.

#### origin 에 직접 닿는 경로를 막는다

위 설정은 위조한 헤더를 믿지 않게 할 뿐, origin(VM 의 80/443)에 Cloudflare 를 거치지
않고 닿는 것 자체를 막지는 않는다. 그 경로로 온 요청은 실제 peer 주소로 세므로 남의
한도를 쓰지는 못하지만, Cloudflare 의 WAF·rate limit 을 우회한다. 그리고 Cloudflare
대역 안에서 나오는 다른 고객의 트래픽(예: Workers)은 Caddy 에게 엣지와 구분되지 않는다.

- VM 의 방화벽(보안 그룹)에서 80/443 을 **Cloudflare 대역에서만** 허용한다. 대역은
  `ops/caddy/trusted_proxies.caddy` 와 같은 목록이다.
- Builder 의 포트(8000)는 기본이 루프백 바인딩이다(`BUILDER_BIND`). 공개하지 않는다.
- Cloudflare 대역 안의 다른 고객까지 막으려면 Cloudflare 의 Authenticated Origin
  Pulls(origin 이 Cloudflare 의 클라이언트 인증서를 요구) 또는 Tunnel 을 쓴다. 저장소의
  Caddyfile 은 이것을 설정하지 않는다.

#### Cloudflare 대역 갱신과 롤백

Cloudflare 는 대역을 바꿀 수 있다. 목록에 없는 새 엣지에서 온 요청은 엣지의 주소로
세어져 그 엣지 뒤의 사용자들이 한 버킷을 공유하고, Cloudflare 가 내놓은 대역이 목록에
남아 있으면 그 대역의 새 주인이 자기 주소를 꾸밀 수 있다.

```bash
python3 scripts/check_cloudflare_ranges.py           # 공개 목록과 비교. 다르면 exit 1
python3 scripts/check_cloudflare_ranges.py --write   # 파일의 목록을 다시 쓴다
```

1. 위 명령으로 차이를 확인하고, `--write` 로 고친 `ops/caddy/trusted_proxies.caddy` 를
   풀 리퀘스트로 올린다. `tests/unit/test_prod_proxy_chain.py` 가 공인 대역이 아니거나
   Cloudflare 의 가장 넓은 블록(/13, /29)보다 넓은 항목을 거부한다.
2. VM 에서 파일을 받은 뒤 Caddy 에 다시 읽힌다. 연결은 끊기지 않는다.
   `docker compose -f docker-compose.prod.app.yml exec caddy caddy reload --config /etc/caddy/Caddyfile`
3. 방화벽의 허용 대역도 같은 목록으로 맞춘다.
4. **롤백:** 이전 커밋의 `ops/caddy/trusted_proxies.caddy` 로 되돌리고 2를 다시 한다.
   Caddy 가 새 설정을 읽지 못하면 `caddy reload` 가 실패하고 이전 설정으로 계속 동작한다.

이 비교는 `cloudflare-ranges.yml` 워크플로가 매일(05:17 UTC) `scripts/cloudflare_ranges_watch.py`
로 돌린다(#1236). 파일이 공개 목록과 다르면 `chore(deploy): Cloudflare's published ranges
differ from the Caddy trusted proxies` 이슈를 **하나** 연다. 차이가 달라지면 같은 이슈의
목록을 고쳐 쓰고 댓글을 달며, 같으면 아무것도 쓰지 않는다. 차이가 사라져도 이슈를 닫지
않는다 — 파일을 고친 풀 리퀘스트가 배포되고 방화벽 목록까지 맞춰졌는지는 사람이 확인한다.
공개 목록을 읽지 못하면(네트워크 오류, 빈 목록) 워크플로가 실패한다. pull request 에서는
비교해 출력만 하고 이슈를 쓰지 않는다. 권한은 `contents: read`, `issues: write` 이고
`GITHUB_TOKEN` 외의 secret 은 쓰지 않는다. 이 워크플로는 파일을 고치거나 풀 리퀘스트를
열지 않는다.

#### 배포 후 확인

Cloudflare 를 거친 실제 요청으로 한 번 확인한다. 서로 다른 두 네트워크(예: 사무실과
휴대폰 테더링)에서 한다.

1. 한쪽에서 틀린 키로 한도(기본 60회)를 넘겨 `429 auth_throttled` 를 받는다.
2. 다른 쪽에서 같은 시각에 요청해 `401`(또는 올바른 키로 `200`)을 받는다. 여기서도
   `429` 가 나오면 두 클라이언트가 한 버킷에 있는 것이다 — Caddy 가 Cloudflare 엣지를
   신뢰하지 못하고 있다(대역 목록, `caddy reload` 여부를 본다).

#### 서버 시계 (chrony)

로그인 토큰(Keycloak·Google ID 토큰)의 `exp`·`nbf`·`iat` 는 서버 시계로 검사한다.
Builder 는 60초의 여유(leeway)만 둔다(`service/auth.py`). 서버 시계가 그보다 어긋나면
올바른 토큰이 `401` 로 거절되고, 그 거절은 인증 실패로 세어져 `429 auth_throttled` 까지
이어질 수 있다. 증상은 "일부 또는 전체 사용자가 갑자기 로그인되지 않는다" 이며 키·대역
문제와 구별되지 않으므로 시계를 먼저 본다.

```bash
chronyc tracking      # System time 이 NTP 시각과 얼마나 다른가, Leap status 가 Normal 인가
chronyc sources -v    # 가리키는 서버 중 하나에 '*'(동기화 중)가 있는가
timedatectl           # 'System clock synchronized: yes', 'NTP service: active'
```

- **경보 기준(제안).** `chronyc tracking` 의 `System time` 이 **0.5초** 를 넘거나 `Leap status`
  가 `Normal` 이 아니거나 `chronyc sources` 에 `*` 가 없는 상태가 5분 이상 가면 알린다. 60초
  여유의 한참 앞에서 잡으려는 값이다. 이 숫자는 측정 없이 정한 것이다.
- **고치기.** `sudo systemctl restart chrony` 뒤 `sudo chronyc makestep` 으로 한 번에 맞춘다.
  VM 이 일시 정지됐다 깨어난 경우 시계가 크게 뛰므로 그때도 같다.
- 컨테이너는 호스트 시계를 쓴다. 시계는 컨테이너 안이 아니라 VM 에서 맞춘다.
- 경보를 어디에서 받을지(메트릭 수집기, 메일)는 배포 대상이 정해져야 고를 수 있다
  (kpubdata-lab/kpubdata#812). 위 기준을 실제 배포에서 적용하고 이슈 #1236 에 결과를 남긴다.

#### 응답 보안 헤더 (#1107)

Caddy 는 거쳐 가는 모든 응답에 아래 헤더를 붙인다(`ops/caddy/Caddyfile` 의
`security_headers`). Builder 자신은 이 헤더들을 보내지 않으므로, Caddy 를 거치지 않는
경로(§"origin 에 직접 닿는 경로를 막는다")의 응답에는 없다.

| 헤더 | 값 | 이유 |
| :--- | :--- | :--- |
| `Strict-Transport-Security` | `max-age=31536000` | TLS 로 나간 응답에만. 1년 동안 HTTPS 로만 오게 한다 |
| `X-Frame-Options` | `DENY` | 다른 페이지의 iframe 삽입 거부 (`frame-ancestors` 를 모르는 브라우저용) |
| `Content-Security-Policy` | `frame-ancestors 'none'` | 같은 거부. Builder 는 JSON API 라 어떤 화면에도 frame 으로 들어갈 이유가 없다 |
| `X-Content-Type-Options` | `nosniff` | 본문을 `Content-Type` 과 다른 형식으로 추측하지 않게 한다 |
| `Referrer-Policy` | `no-referrer` | API 주소의 경로(run id, 테이블 이름)를 다른 사이트에 넘기지 않는다 |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=(), payment=(), usb=()` | API 응답이 쓸 일이 없는 브라우저 기능 |

- **HSTS 에 `includeSubDomains` 와 `preload` 는 없다.** 이 이름 옆의 다른 이름들이 모두
  HTTPS 인지는 도메인 전체를 본 뒤에 정할 일이고, `preload` 는 브라우저에 실려 나간 뒤
  되돌리기 어렵다. 넣기로 하면 Caddyfile 의 한 줄을 고치고, 그것을 금지하는 테스트
  (`tests/unit/test_prod_proxy_chain.py`)를 함께 고친다.
- **HSTS 는 평문 HTTP 응답에는 붙지 않는다.** 브라우저는 평문 응답의 이 헤더를 무시하고
  RFC 6797 §7.2 도 보내지 말라고 한다.
- **Cloudflare 에서 HSTS 를 따로 켜면** 엣지가 이 헤더를 자기 값으로 바꿔 쓴다. 거기서
  `includeSubDomains`·`preload` 를 켜면 위 결정과 달라지므로 켜지 않는다.
- `scripts/proxy_chain_smoke.py` 가 실제 Caddy 를 거친 응답(Builder 가 200 으로 답한 것과
  401 로 답한 것)에서 각 헤더가 **한 번씩** 그 값으로 오는지 본다. HSTS 는 Caddy 가 자기
  로컬 인증기관의 인증서로 TLS 를 여는 이름(`caddy.localhost`)으로 요청해 있는지 보고,
  평문 요청에는 없는지 본다. 같은 스모크가 러너의 Chrome 으로 다른 origin 의 페이지를
  열어, Caddy 를 거친 응답을 iframe 에 띄우기를 **브라우저가 거부하는지** 본다 — 헤더가
  없는 응답(Builder 포트 직접)은 같은 페이지에서 거부되지 않는 것까지 함께 본다.
- **확인하지 않은 것:** 공인 인증서와 Cloudflare 를 거친 실제 배포의 응답. 첫 배포 때
  한 번 본다.

  ```bash
  curl -sI https://<APP_DOMAIN>/healthz | grep -i -E \
    'strict-transport|x-frame|content-security|x-content-type|referrer|permissions'
  ```

#### 다른 프록시를 쓸 때

`KPUBDATA_BUILDER_TRUSTED_PROXIES` 에 **Builder 가 보는 프록시의 주소**를 적는다. 대역보다
주소를 적는다.

- **프록시가 여러 겹이면 각 겹이 앞 겹을 신뢰해야 한다.** 앞 겹을 신뢰하지 않는
  프록시는 `X-Forwarded-For` 를 자기가 본 peer(앞 겹의 주소)로 바꿔 쓰고, Builder 는
  사용자 대신 앞 겹의 주소로 묶는다.
- Builder 포트가 프록시를 거치지 않고도 닿는 배포에서는 이 변수에 그 경로의 주소가
  들어가지 않게 한다. 신뢰하는 주소에서 온 요청은 헤더로 자기 식별자를 정할 수 있다.
- 프록시가 클라이언트 주소를 넘기지 않는 배포에서는 한도를 `0`으로 두고 프록시
  계층에서 스로틀을 거는 편이 낫다.
>
> 카운터는 프로세스 로컬이다. 인스턴스를 여러 개 띄우면 인스턴스별로 센다(정확한 전역
> 한도가 아니라 남용 완화가 목적).


## 요청 로그 (#1100)

Builder 는 답한 요청마다 한 줄을 남긴다. 표준 logging 의 `kpubdata_builder.request` 로거로 나가고, 배포가 핸들러를 붙이지 않았으면 stderr 로 나간다(관리자 감사 로그 `kpubdata_builder.admin_audit` 와 같은 방식). 한 줄은 JSON 이다.

```json
{"ts":"2026-10-09T14:51:07.412Z","event":"request","request_id":"3f9c1a7b2d40","method":"GET","route":"/builds/{run_id}","status":404,"duration_ms":3.2,"principal":"oidc","owner":"5b1e0c7a94d2f6e3"}
```

| 필드 | 뜻 |
|---|---|
| `ts` | 답을 다 보낸 시각, UTC |
| `request_id` | 응답의 `X-Request-ID` 헤더와 같은 값. 사용자가 신고한 요청을 이 값으로 찾는다 |
| `route` | 계약의 경로 **템플릿**. 실제 경로가 아니다 — 경로에는 run id, 테이블 이름, 파일 이름이 들어 있다. 계약에 없는 경로는 `unmatched` |
| `status`, `duration_ms` | HTTP 상태와, 요청 줄을 받은 때부터 답을 다 보낼 때까지 걸린 시간 |
| `code` | 답의 `code` 가 있을 때만(`unauthorized`, `auth_throttled`, `provider_credential_required` …). `request_log.KNOWN_CODES` 목록에 있는 값만 그대로 쓰고, 목록에 없는 값은 `other` 로 쓴다 — 코드처럼 생긴 다른 값이 줄에 들어가지 않게 한다 |
| `principal` | `oidc` / `service` / `dev`, 인증되지 않은 요청은 `null` |
| `owner` | 같은 사용자의 줄을 묶는 값. 아래 참고 |

**줄에 들어가지 않는 것.** 헤더(`Authorization`, `X-API-Key`, `X-Provider-Key`, `X-Publish-Credential`, `Cookie` 포함), 쿼리 문자열, 요청·응답 본문, 실제 경로. 이 모듈은 그 값들을 받지 않는다 — 지우는 것이 아니라 처음부터 넘기지 않는다. `tests/unit/test_request_log.py` 가 실제 서버에 각 자리마다 표식 값을 실어 보내고 어느 줄에도 없음을 확인한다.

**`owner` 는 누구인지 말하지 않는다.** `owner_id` 를 프로세스가 시작할 때 만든 키로 HMAC 한 값의 앞 16자다. 키는 어디에도 저장하지 않으므로 값에서 `owner_id` 를 되찾을 수 없고, **재시작하면 같은 사용자의 값이 달라진다.** 이것은 결정이다(#1232, 2026-10-11): **키를 설정으로 받지 않고, `owner` 는 재시작을 넘어 이어지지 않는다.**

- **재시작을 넘어 같은 사용자를 이어 볼 필요가 있는가** — 지금은 없다고 본다. 한 요청의 경로는 `request_id` 가 잇고, 어떤 실행이 누구의 것인지는 build index 와 관리자 화면(`/admin/runs`)이 `owner_id` 로 답하며, 관리자 조치는 감사 로그에 남는다. 요청 로그의 `owner` 가 하는 일은 한 프로세스가 사는 동안 "이 줄들이 같은 사용자"를 보이는 것이고, 사고 조사는 대개 그 범위 안에서 시작한다.
- **키를 어디에 두는가** — 두지 않는다. 키를 배포 설정으로 받으면 그 키를 읽을 수 있는 사람은 알려진 `owner_id` 가 어느 줄인지 계산할 수 있다. 그러면 로그는 가명 처리된 기록이 아니라 사용자별 행동 기록이 되고, 키의 보관과 접근이 곧 로그의 접근 정책이 된다. 지금 있는 것은 아래 "로그의 보관과 열람"의 기본값뿐이고, 그것은 로그만으로 사용자를 가려낼 수 없다는 전제에서 적은 것이다.
- **키를 바꾸는 주기** — 재시작할 때마다 바뀐다. 이전 줄과의 연결이 끊기는 것은 의도다.

다시 검토할 조건: 로그의 보관 기간과 열람 권한이 정해지고(#1100), 재시작을 사이에 둔 요청을 이어야 했던 조사가 실제로 생기면 키를 설정으로 받는 방안을 새 이슈로 다룬다. 그때에는 키가 없을 때 지금처럼 프로세스마다 새 키를 쓰고, 키 값이 설정 출력과 로그에 나오지 않아야 한다.

**줄이 남지 않는 경우.** 핸들러에 닿기 전에 표준 라이브러리가 직접 답하는 요청 — 잘못된 요청 줄, 너무 긴 헤더, `HEAD` 처럼 지원하지 않는 메서드 — 과 CORS preflight(`OPTIONS`)는 줄을 남기지 않는다. 동시 연결 한도에서 바로 거절한 503 은 남긴다: 요청을 읽지 않았으므로 `method` 는 `-`, `route` 는 `unread` 이고, 그 응답에는 `X-Request-ID` 가 없어 줄의 `request_id` 로 응답을 찾을 수는 없다.

**보관.** Builder 는 줄을 내보내기만 한다. 저장소의 compose 로 올린 배포에서 그 줄이 어디에 얼마나 남고 누가 읽는지는 아래 "로그의 보관과 열람"에 있다.

### build 로그

요청 줄은 build 가 제출됐고 그 요청에 어떻게 답했는지를 말한다. 비동기 build 는 그 답보다 한참 뒤에 워커에서 돌기 때문에, 언제 시작해 얼마나 걸려 어떻게 끝났는지는 `kpubdata_builder.build` 로거의 두 줄이 말한다. 나가는 방식은 요청 로그와 같다(핸들러가 없으면 stderr).

```json
{"ts":"2026-10-11T00:37:01.694Z","event":"build_started","run_id":"20261011T101500123456Z-0123456789ab","mode":"async","owner":"e9a122d54c336bfc"}
{"ts":"2026-10-11T00:41:13.020Z","event":"build_ended","run_id":"20261011T101500123456Z-0123456789ab","mode":"async","owner":"e9a122d54c336bfc","status":"failed","duration_ms":251326.0,"code":"credentials_required"}
```

| 필드 | 뜻 |
|---|---|
| `event` | `build_started` 는 build 가 실제로 돌기 시작한 때(비동기 build 는 큐에서 나와 build 슬롯을 얻은 때), `build_ended` 는 끝난 때 |
| `run_id` | 두 줄을 잇는 값. **Builder 가 만든 형식의 run id 만 그대로 쓴다.** 아래 참고 |
| `run_ref` | 클라이언트가 정한 run id 일 때 `run_id` 대신 쓰는 값 |
| `mode` | `async`(`POST /builds`) 또는 `sync`(`POST /build`). 둘 다 워커가 실행한다 — `sync` 는 요청이 그 job 이 끝나기를 기다린다(#1242) |
| `status` | `succeeded` / `failed` / `cancelled`. 비동기 build 는 `GET /builds/{run_id}` 가 답하는 최종 상태와 같다 |
| `duration_ms` | 시작 줄부터 끝 줄까지. 큐에서 기다린 시간은 들어가지 않는다 |
| `code` | 실패한 비동기 build 의 답에 `code` 가 있을 때만. 요청 로그와 같은 목록(`request_log.KNOWN_CODES`)에 있는 값만 그대로 쓰고 나머지는 `other` |
| `error_type` | 아무도 처리하지 않은 예외로 끝났을 때 그 예외의 **클래스 이름**(`RuntimeError`). 메시지는 쓰지 않는다 |
| `reason` | 실행 시간 상한(#1119)에 걸려 취소됐을 때 `time_limit` |
| `owner` | 요청 로그의 `owner` 와 같은 값 — 한 프로세스 안에서 한 사용자의 요청 줄과 build 줄이 같은 값을 갖는다 |

**줄에 들어가지 않는 것.** spec, 응답 본문, 오류 메시지, 예외의 텍스트. 요청 로그와 같은 방식이다 — 이 모듈은 그 값들을 받지 않는다. 예외의 메시지는 provider 의 URL 을 인용할 수 있고 그 URL 에는 키가 들어 있으므로, 줄이 받는 것은 클래스 이름뿐이다. 왜 실패했는지의 정본은 로그가 아니라 실행의 매니페스트(`failures`, #1120)다.

**클라이언트가 정한 run id 는 줄에 쓰지 않는다.** run id 는 클라이언트가 정할 수 있고(영문자·숫자·`.`·`-`·`_`), 그러면 사용자가 쓴 텍스트다. Builder 가 만드는 형식(`20261011T101500123456Z-0123456789ab`, 숫자와 16진수뿐이다)일 때만 `run_id` 로 쓰고, 그 밖의 값은 SHA-256 의 앞 16자를 `run_ref` 로 쓴다. run id 를 아는 사람은 같은 값을 계산해 줄을 찾을 수 있다.

```bash
printf '%s' 'my-run-id' | sha256sum | cut -c1-16
```

**줄이 남지 않는 경우.** 시작하지 못하고 끝난 build — 큐에서 취소됐거나, 종료 절차가 큐에 있던 것을 실패로 끝냈거나, 큐에 넣는 데 실패한 경우 — 는 두 줄 모두 남기지 않는다. 그 경과는 요청 줄과 실행의 이벤트 타임라인(`GET /builds/{run_id}/events`)에 있다. 프로세스가 도중에 죽은 build 는 시작 줄만 남는다.

**다른 로거의 줄.** 위 두 로거와 관리자 감사 로그 말고도 Builder 는 진단용 줄을 쓴다(처리하지 못한 예외의 traceback 등). 이 줄에는 예외의 텍스트가 들어간다. `serve` 는 프로세스가 만드는 모든 로그 레코드에서 자격증명 이름의 쿼리 값(`serviceKey=…`)과, 열려 있는 클라이언트의 키 값을 지운다(`logging_redaction`, #686). `tests/unit/test_canary_leak_gate.py` 가 실제 kpubdata 클라이언트로 build 의 각 실패 경로를 돌려 키가 어느 레코드에도 없음을 확인하고, `tests/unit/test_build_log.py` 가 키가 든 provider URL 을 메시지로 가진 예외에 대해 같은 것을 확인한다. 키가 아닌 예외 텍스트는 진단 줄에 남는다.

### 프록시(Caddy)의 접근 로그

`--profile caddy` 로 올리면 Caddy 가 공개 사이트에 들어온 요청마다 한 줄을 쓴다(`ops/caddy/Caddyfile` 의 `log`). Builder 에 닿지 못한 요청 — Builder 가 내려가 있어 Caddy 가 502 나 503 으로 답한 요청 — 도 여기에는 남는다. 줄은 JSON 이고 Caddy 컨테이너의 stderr 로 나간다.

| 남는 것 | 지우는 것 |
|---|---|
| 시각, 클라이언트 주소(`request.client_ip` — Cloudflare 가 알려 준 주소, "리버스 프록시 뒤에서" 참고), 메서드, 호스트, **경로**, 상태, 응답 크기, 걸린 시간, TLS 정보 | **요청 헤더 전체**(`Authorization`, `X-API-Key`, `X-Provider-Key`, `X-Publish-Credential`, `Cookie` 포함), **응답 헤더 전체**(`Content-Disposition` 의 파일 이름 포함), **쿼리 문자열**(`?` 뒤는 `?[removed]` 로 바뀐다 — 쿼리가 있었다는 사실만 남는다) |

- **이름을 골라 지우지 않고 통째로 지운다.** Caddy 가 스스로 가리는 헤더는 `Authorization`, `Cookie` 등 몇 개뿐이고 `X-API-Key`, `X-Provider-Key`, `X-Publish-Credential` 은 그대로 쓴다. 헤더 이름 목록을 적어 지우면 새 헤더가 생길 때마다 목록을 고쳐야 하므로, 헤더 구역을 통째로 지운다.
- **거름은 Caddy 의 기본 로거에 걸려 있다**(`log default`). 접근 로그뿐 아니라, 업스트림 오류 때 Caddy 가 요청을 덧붙여 쓰는 오류 줄도 같은 거름을 지난다. 사이트의 `log` 에 `output` 이나 `format` 을 적으면 그 사이트의 줄이 별도 로거로 나가 거름을 지나지 않으므로 적지 않는다.
- **경로는 남는다.** 경로에는 run id, 테이블 이름, 파일 이름이 들어 있다. Builder 의 요청 줄은 그래서 경로 대신 템플릿을 쓰지만, 프록시는 템플릿을 모른다. 자격증명을 경로에 넣는 엔드포인트는 Builder 에 없다.
- **프록시 줄에는 `request_id` 가 없다.** 응답 헤더를 통째로 지우므로 `X-Request-ID` 도 지워진다. 프록시 줄과 Builder 의 요청 줄은 시각·메서드·상태로 맞춘다. Builder 에 닿은 요청이라면 Builder 의 줄에서 시작하는 편이 낫다.
- `:80` 의 `/healthz`(Caddy 자체 헬스체크)는 줄을 남기지 않는다.

`scripts/proxy_chain_smoke.py` 가 `Docker` 워크플로에서 실제 Caddyfile 로 Caddy 를 띄우고, 자격증명이 실리는 자리마다(위 다섯 헤더와 쿼리 문자열) 표식 값을 실은 요청을 보낸 뒤 `docker compose logs caddy` 를 읽어 확인한다: 그 요청의 접근 로그 줄이 있고, 어느 줄에도 표식 값·서비스 키·요청 헤더·응답 헤더·쿼리 문자열이 없다. Builder 를 내린 뒤 같은 요청을 한 번 더 보내 오류 줄에 대해서도 확인한다.

### 로그의 보관과 열람

> **현재의 기본값이다.** 저장소의 compose 로 올린 VM 한 대 배포에서 지금 실제로 일어나는 일을 적은 것이고, 법적 보존 의무나 개인정보 처리방침에 맞춘 보관 정책이 아니다. 그런 정책이 필요한 배포는 아래 값을 바꾸고 이 절을 자기 것으로 고쳐 쓴다.

**무엇이 어디에 남는가.**

| 로그 | 쓰는 곳 | 남는 곳 |
|---|---|---|
| 요청 로그, build 로그, 관리자 감사 로그, 진단 로그 | Builder 컨테이너의 stderr | VM 의 Docker 로그 파일 (`/var/lib/docker/containers/<id>/<id>-json.log`) |
| 접근 로그, Caddy 자체의 로그 | Caddy 컨테이너의 stderr | 같은 방식, Caddy 컨테이너의 파일 |

다른 곳으로는 보내지 않는다. 로그 수집기, 외부 저장소, 백업이 없다. `/data` 볼륨에는 로그가 없다 — 실패한 실행의 이유(매니페스트의 `failures`, #1120), 이벤트 타임라인, build index 는 `/data` 에 있는 **기록**이고 로그와 따로 보관된다.

**얼마나 남는가 — 기간이 아니라 크기로 정해진다.**

- compose 의 `logging:` 이 컨테이너마다 파일 하나를 `20m` 까지, 파일을 `5` 개까지 둔다 — **컨테이너당 최대 100MB**. 넘으면 가장 오래된 파일부터 지워진다. `.env` 의 `LOG_MAX_SIZE`, `LOG_MAX_FILE` 로 바꾸고, 바꾼 값은 컨테이너를 다시 만들 때 적용된다.
- 그 크기가 며칠 치인지는 요청량에 달렸다. 사용자 요청이 전혀 없어도 헬스 프로브가 줄을 쓴다: Caddy 가 15초마다, compose 의 healthcheck 가 30초마다 `/healthz` 를 부르고 Builder 는 그때마다 요청 줄을 남긴다. 하루 8,640 줄, Docker 가 저장하는 형식으로 줄당 272바이트, 하루 약 2.4MB — 100MB 는 그것만으로 40일 남짓이다. 사용자 요청이 있으면 그보다 짧다.
- **컨테이너를 다시 만들면 로그는 사라진다.** 새 이미지로 `docker compose up -d` 를 하거나 `down` 을 하면 Docker 가 이전 컨테이너와 함께 그 로그 파일을 지운다. 그래서 실제 보관 기간은 "위 크기에 닿을 때까지"와 "다음 배포까지" 중 짧은 쪽이다. `restart` 와 VM 재부팅은 컨테이너를 다시 만들지 않으므로 로그가 남는다.
- 사고를 조사하는 동안 배포해야 한다면 먼저 로그를 꺼내 둔다.

  ```bash
  docker compose -f docker-compose.prod.app.yml --profile caddy logs --no-color --timestamps > incident-$(date -u +%Y%m%dT%H%M%SZ).log
  ```

  꺼낸 파일은 로그와 같은 것을 담고 있으므로 같은 사람만 읽을 수 있는 곳에 두고, 조사가 끝나면 지운다.

**누가 읽는가.** VM 에 셸로 들어갈 수 있고 root 이거나 `docker` 그룹에 속한 사람. Docker 로그 파일은 root 만 읽을 수 있고, `docker compose logs` 는 Docker 소켓에 접근할 수 있어야 한다 — `docker` 그룹은 사실상 root 와 같은 권한이다. Builder 의 API 로는 로그를 읽을 수 없다: 관리자 화면(`/admin/*`)도 로그를 내주지 않는다. 그러므로 로그의 열람 권한은 **VM 의 셸 접근 권한**이고, 그 목록을 줄이는 것이 로그 접근을 줄이는 방법이다.

**로그에 든 사람에 관한 정보.**

- 요청 줄과 build 줄의 `owner` 는 **사용자 식별자가 아니다.** 프로세스가 시작할 때 만든 키로 만든 값이라 `owner_id` 로 되돌릴 수 없고, 재시작하면 같은 사용자의 값이 달라진다(위 "`owner` 는 누구인지 말하지 않는다"와 그 아래 #1232 의 결정 — 키를 설정으로 받지 않는다). 로그만으로는 어느 줄이 누구의 것인지 알 수 없다. 위의 열람 범위는 이 전제에서 정한 것이므로, 그 결정을 바꿔 키를 보관하게 되면 이 절도 다시 적는다.
- 관리자 감사 로그(`kpubdata_builder.admin_audit`)에는 조치를 한 관리자의 표시 이름과 `owner_id`, 그리고 조치의 대상(run id, 사용자 id)이 그대로 남는다. 조치를 한 사람을 가려내는 것이 그 로그의 목적이기 때문이다.
- Caddy 의 접근 로그에는 클라이언트의 IP 주소와 요청한 경로가 남는다. 둘을 함께 보면 한 주소가 어느 run 과 테이블을 다뤘는지 알 수 있으므로, 이 로그는 개인정보가 들어 있는 것으로 다룬다.

**정하지 않은 것.** 일정 기간 뒤에 반드시 지우는 절차(크기 제한은 오래된 것을 밀어낼 뿐 기한을 보장하지 않는다), 컨테이너를 다시 만든 뒤에도 남는 보관, VM 밖으로의 수집. 이 가운데 하나가 필요해지면 Docker 의 로그 드라이버를 바꾸거나 수집기를 붙이는 일이고, 그때 이 절의 "어디에", "얼마나", "누가"를 다시 적는다.

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

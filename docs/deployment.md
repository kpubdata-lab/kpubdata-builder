# 배포 및 설정 가이드

⚠️ **중요**: 배포 모드가 아직 확정되지 않았습니다 (#682, #635). 아래 절차 중 일부는 결정되지 않은 사항을 확정된 것처럼 서술할 수 있습니다.

---

## 환경변수

| 변수명 | 설명 | 기본값 | 필수 여부 |
| :--- | :--- | :--- | :--- |
| `KPUBDATA_BUILDER_API_KEY` | API 인증 키 (`X-API-Key` 헤더). 미설정 시 모든 요청 401 (fail-closed) | 없음 | **필수** (프로덕션) |
| `KPUBDATA_BUILDER_DEV_MODE` | `true`/`1`이면 인증 생략 (**로컬 개발 전용**, ADR 0006). 기동 시 경고 로그를 남기고, `OIDC_ISSUER`와 함께 설정되면 기동 거부 | 미설정 | 선택 |
| `KPUBDATA_BUILDER_ALLOWED_ORIGINS` | CORS 허용 오리진 (콤마 구분, default-deny). 응답에는 항상 `Vary: Origin`이 붙는다 | 미설정 | 선택 |
| `KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT` | 윈도당 허용할 인증 실패 횟수(클라이언트 IP별). 초과분은 `429 auth_throttled`. `0` 이하면 비활성 | `60` | 선택 |
| `KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS` | 인증 실패 카운트 윈도(초) | `60` | 선택 |
| `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` | 사용자별 Provider credential AES-GCM master key (URL-safe base64 32 bytes) | 미설정 | credential CRUD 사용 시 필수 |
| `KPUBDATA_BUILDER_ADMIN_SUBJECTS` | 관리자로 대우할 `<issuer>\|<sub>` 목록(쉼표 구분, #679). 관리 엔드포인트(`GET /admin/runs`, `GET /admin/config`)를 열지만 남의 run 산출물은 열지 않는다. **issuer 를 반드시 함께 적는다** — `sub` 는 issuer 안에서만 유일하고 `OIDC_ISSUER` 는 복수를 허용한다. issuer 없는 항목은 경고와 함께 무시된다. OIDC 배포에서는 이 변수를 컨테이너까지 전달해야 한다 | 미설정 | 다중 사용자 배포 시 선택 |
| `KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT` | Provider connection test 전송 timeout(초) | `10` | 선택 |
| `KPUBDATA_BUILDER_STORAGE_BACKEND` | 상태 백엔드 (`sqlite`=로컬 기본, `cubrid`=CUBRID, ADR 0016) | `sqlite` | 선택 |
| `KPUBDATA_BUILDER_CUBRID_URL` | CUBRID SQLAlchemy URL (예: `cubrid+pycubrid://user:pass@host:33000/db?charset=utf8`) | 미설정 | `STORAGE_BACKEND=cubrid` 시 필수 |
| `KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS` | `prune-cancelled --apply`가 cancelled partial run을 정리하기까지의 보존 시간(시간). 미설정이면 정리 대상 없음(#549) | 미설정 | 선택 |
| `KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT` | HTTP `local` publish target의 루트 디렉터리(절대 경로). destination은 이 안의 상대 `owner/name`로 한정된다(#550). 미설정이면 local target blocker | 미설정 | local publish 사용 시 필수 |
| `KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL` | `true` 면 **데이터 조회**에도 요청자 자신의 provider 키만 쓰고 운영자 키로 내려가지 않는다(F-07). 폴백을 두면 공유 배포에서 한 사람의 질의가 운영자 쿼터를 쓰고 운영자 신원으로 제공기관에 찍힌다. 미설정이면 폴백 허용(단일 사용자 배포 기본 동작). **다중 사용자 배포(OIDC 또는 `ENFORCE_OWNERSHIP`)에서는 값과 무관하게 켜지고**, 키는 요청의 `X-Provider-Key` 헤더로만 받아 요청·작업 동안만 메모리에 둔다(#683) | 미설정 | 선택 |
| `KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS` | 다중 사용자 배포에서 비동기 작업에 묶인 provider 키를 워커가 가져가기 전까지 메모리에 두는 최대 시간(#683). 지나면 키를 버리고 작업은 키 없이 실패한다 | `3600` | 선택 |
| `KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL` | `true`면 게시 시 요청자에게 저장된 publish credential 만 쓰고 서버 환경변수(`HF_TOKEN` 등)로 내려가지 않는다(#635). 다중 사용자 배포용 — 미설정이면 폴백 허용(단일 사용자 배포 기본 동작) | 미설정 | 선택 |
| `KPUBDATA_BUILDER_MAX_UPLOAD_BYTES` | `POST /uploads`가 받는 최대 본문 크기(바이트). 초과분은 413 | 코드 기본값 | 선택 |
| `KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES` | `kind: url` source가 가져오는 최대 응답 크기(바이트). SSRF 방어의 일부(#498) | 코드 기본값 | 선택 |
| `OIDC_JWKS_URL` | JWKS 엔드포인트를 직접 지정한다. 미설정 시 issuer의 discovery 문서에서 찾는다 | 미설정 | 선택 |
| `OIDC_JWKS_TTL` | JWKS 캐시 수명(초). 만료되면 다음 Bearer 인증이 다시 가져온다 | `3600` | 선택 |

> **fail-closed (ADR 0006)**: `KPUBDATA_BUILDER_API_KEY` 미설정 + `DEV_MODE` 미설정 → 모든 요청 401.
> 로컬 개발에서 인증 없이 띄우려면 `KPUBDATA_BUILDER_DEV_MODE=1`을 명시하세요.
> Docker 컨테이너는 `DEV_MODE` 없이 `API_KEY`가 없으면 기동 자체를 거부합니다 (`docker-entrypoint.sh`).

### Provider credential store 운영 (`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`)

사용자별 Provider credential CRUD(`GET/PUT/DELETE /providers/{provider}/credential`)는
암호화된 credential store를 요구하며, 이 store는 `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`가
설정돼 있을 때만 활성화된다.

- **master key 미설정**: 세 credential endpoint 모두 `503`
  (`{"error": "credential store is not configured"}`)을 반환한다. 이는 **운영자가 store를
  구성하지 않은 상태**이며, "사용자가 아직 credential을 등록하지 않음"(store는 정상이고
  `GET`이 `200 {"configured": false, "masked": null, "updated_at": null}`)과 명확히 다른
  상태다. Studio도 이 둘을 서로 다른 UI로 구분해서 보여준다 — 둘을 하나의 generic 실패로
  뭉개지 않는다.
- **key는 안정적으로 재사용한다**: credential은 이 key로 AES-GCM 암호화되어 저장된다.
  배포·재기동 사이에 **반드시 동일한 key**를 다시 주입해야 한다.
- **key를 바꾸면 기존 credential을 읽을 수 없다**: 다른 key로 교체하면 이전에 저장된
  encrypted credential은 복호화에 실패한다(사실상 폐기). key rotation이 필요하면 각
  사용자가 credential을 다시 등록해야 한다.
- **형식**: URL-safe base64로 인코딩한 32바이트. 예:
  `python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`.
- **secret은 문서/example/로그에 넣지 않는다**: OpenAPI example과 이 문서의 예시는
  placeholder(`replace-with-your-provider-key` 등)만 쓴다. 실제 master key나 provider
  credential 원문을 커밋하거나 로그로 남기지 않는다. `PUT` 응답도 원문을 echo하지 않고
  마스킹 메타데이터만 반환한다.

---

## HTTP 서비스 배포 (Docker)

`Dockerfile`과 `docker-entrypoint.sh`은 `uv sync --no-sources`로 PyPI `kpubdata`를
설치해 `kpubdata-builder serve`를 실행하는 재현 가능한 이미지를 만듭니다 (#320,
ADR 0006). 설정은 환경변수로 주입합니다 — `docker-entrypoint.sh`가 이를 serve CLI
플래그로 변환합니다.

### 컨테이너 환경변수

| 변수 | 설명 | 기본값 | 필수 |
| :--- | :--- | :--- | :--- |
| `KPUBDATA_BUILDER_API_KEY` | `X-API-Key` 인증 키 | 없음 | **필수** (fail-closed) |
| `KPUBDATA_BUILDER_PORT` | 바인딩 포트 | `8000` | 선택 |
| `KPUBDATA_BUILDER_OUTPUT_DIR` | 실행 워크스페이스 루트 | `/data` | 선택 |
| `KPUBDATA_BUILDER_HOST` | 바인딩 호스트 | `0.0.0.0` | 선택 |
| `KPUBDATA_BUILDER_DEV_MODE` | `true`/`1`이면 API 키 없이 기동 (로컬 개발 전용) | 미설정 | 선택 |
| `KPUBDATA_BUILDER_MAX_WORKERS` | 동시 요청 스레드 상한 | `10` | 선택 |
| `KPUBDATA_BUILDER_WAREHOUSE` | 테이블 카탈로그 루트(`serve --warehouse` 와 같다). 설정하면 `POST /build` 가 source 별 Gold 를 커밋된 table snapshot 으로 남기고 응답 `materialized` 에 보고한다 — publish 자격증명이 필요 없다(#703). 미설정이면 카탈로그를 쓰지 않고 응답에 `materialized` 키가 없다 | 미설정 | 선택 |
| `KPUBDATA_QUERY_MAX_CONCURRENCY` | 동시 query child process 상한 | `2` | 선택 |
| `KPUBDATA_BUILDER_ALLOWED_ORIGINS` | CORS 허용 오리진 (콤마 구분, default-deny). 응답에는 항상 `Vary: Origin`이 붙는다 | 미설정 | 선택 |
| `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` | 사용자별 Provider credential AES-GCM master key (URL-safe base64 32 bytes) | 미설정 | credential CRUD 사용 시 필수 |
| `KPUBDATA_BUILDER_ADMIN_SUBJECTS` | 관리자로 대우할 `<issuer>\|<sub>` 목록(쉼표 구분, #679). 관리 엔드포인트(`GET /admin/runs`, `GET /admin/config`)를 열지만 남의 run 산출물은 열지 않는다. **issuer 를 반드시 함께 적는다** — `sub` 는 issuer 안에서만 유일하고 `OIDC_ISSUER` 는 복수를 허용한다. issuer 없는 항목은 경고와 함께 무시된다. OIDC 배포에서는 이 변수를 컨테이너까지 전달해야 한다 | 미설정 | 다중 사용자 배포 시 선택 |
| `KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT` | Provider connection test 전송 timeout(초) | `10` | 선택 |
| `KPUBDATA_BUILDER_STORAGE_BACKEND` | 상태 백엔드 (`sqlite`=로컬 기본, `cubrid`=CUBRID, ADR 0016) | `sqlite` | 선택 |
| `KPUBDATA_BUILDER_CUBRID_URL` | CUBRID SQLAlchemy URL (예: `cubrid+pycubrid://user:pass@host:33000/db?charset=utf8`) | 미설정 | `STORAGE_BACKEND=cubrid` 시 필수 |
| `KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS` | `prune-cancelled --apply`가 cancelled partial run을 정리하기까지의 보존 시간(시간). 미설정이면 정리 대상 없음(#549) | 미설정 | 선택 |
| `KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT` | HTTP `local` publish target의 루트 디렉터리(절대 경로). destination은 이 안의 상대 `owner/name`로 한정된다(#550). 미설정이면 local target blocker | 미설정 | local publish 사용 시 필수 |
| `OIDC_ISSUER` | OIDC 발급자 (설정 시 Bearer 활성, ADR 0015 — Keycloak realm) | 미설정 | 선택 |
| `OIDC_AUDIENCE` | OIDC audience (OIDC_ISSUER 설정 시 필수) | 미설정 | OIDC 시 필수 |
| `OIDC_ALLOWED_HD` | 허용 Workspace 도메인. OIDC 배포는 이 셋 중 하나 이상이 필수 — 없으면 기동 거부(#635) | 미설정 | OIDC 시 셋 중 하나 필수 |
| `OIDC_ALLOWED_SUBJECTS` | 허용 sub 목록 (콤마 구분) | 미설정 | OIDC 시 셋 중 하나 필수 |
| `OIDC_ALLOWED_EMAILS` | 허용 이메일 목록 (콤마 구분) | 미설정 | OIDC 시 셋 중 하나 필수 |
| `ENFORCE_OWNERSHIP` | `true`/`1`이면 run 소유권 강제 (C2, #389). `OIDC_ISSUER`가 있으면 값과 무관하게 켜진다(#635) | 미설정 | 선택 |

> **fail-closed (ADR 0006)**: 컨테이너는 `KPUBDATA_BUILDER_API_KEY`가 없으면 기동을
> 거부합니다. `service/app.py`의 "키 미설정 = 인증 생략" 동작은 로컬 개발 편의 전용이며
> 컨테이너로 누출되지 않습니다. 로컬에서 인증 없이 띄우려면 `KPUBDATA_BUILDER_DEV_MODE=1`을
> 명시하세요.

### Docker 이미지 빌드 및 실행

```bash
# 이미지 빌드
docker build -t kpubdata-builder:latest .

# 실행 — API 키 필수 (fail-closed). 빌드 산출물은 /data 볼륨에 영속화.
docker run --rm -p 8000:8000 \
  -e KPUBDATA_BUILDER_API_KEY="${API_KEY}" \
  -v kpubdata-builder-data:/data \
  kpubdata-builder:latest

# 헬스 체크: 계약 버전 확인
curl -s -H "X-API-Key: ${API_KEY}" http://localhost:8000/version
# {"service": "kpubdata-builder", "api_version": "1.0.0"}

# 로컬 개발 — 인증 생략 (dev-mode)
docker run --rm -p 8000:8000 -e KPUBDATA_BUILDER_DEV_MODE=1 kpubdata-builder:latest
```

### Extra 그룹 선택

`kpubdata`는 `uv sync --no-sources`로 PyPI에서 설치되므로, 빌드 시 형제 디렉터리
(`../kpubdata`)가 필요하지 않습니다 (버전 핀 정책은 [CONTRIBUTING.md](./CONTRIBUTING.md)
참고). 배포 이미지는 빌드 타임 `EXTRAS` ARG로 extra 그룹을 선택하며, **기본값은
`publish`** 입니다 — HuggingFace/Kaggle 게시 타깃이 런타임 `ImportError`로 실패하지
않도록 `huggingface-hub`/`kaggle`/`xmltodict`를 기본 포함합니다 (#373).

```bash
# 기본(publish extra 포함)
docker build -t kpubdata-builder:latest .

# 여러 extra / 최소 이미지
docker build --build-arg EXTRAS="publish parquet" -t kpubdata-builder:full .
docker build --build-arg EXTRAS= -t kpubdata-builder:minimal .
```

> 참고: exporter(parquet/Hugging Face 레이아웃)는 polars·표준 라이브러리만 쓰므로
> extras 없이도 동작합니다. extras가 필요한 것은 **publisher**(huggingface_hub/kaggle)입니다.

---

## CLI 명령 상세

### validate 명령

BuildSpec YAML 파일의 유효성을 검사합니다.

```bash
kpubdata-builder validate specs/weather.yaml
```

### preview 명령

BuildSpec을 실행하지 않고 스키마와 샘플 데이터만 미리볼 수 있습니다.

```bash
kpubdata-builder preview specs/weather.yaml --limit 10
```

### build 명령

BuildSpec을 통해 Medallion 파이프라인을 실행합니다.

```bash
kpubdata-builder build specs/weather.yaml --output-dir ./dist/weather
```

### publish 명령

빌드 결과물을 로컬 또는 원격 저장소로 게시합니다.

```bash
# 로컬 디렉터리로 게시
kpubdata-builder publish specs/weather.yaml --target local --destination ./out --artifacts-dir ./dist/weather/run-001

# Hugging Face에 게시
kpubdata-builder publish specs/weather.yaml --target huggingface --destination my-org/my-dataset --artifacts-dir ./dist/weather/run-001

# Kaggle에 공개 데이터셋으로 게시
kpubdata-builder publish specs/weather.yaml --target kaggle --destination my-username/my-dataset --artifacts-dir ./dist/weather/run-001 --public
```

### serve 명령

Builder HTTP 서비스를 실행합니다 (Studio 연동용).

```bash
# 서버 시작 (기본: 127.0.0.1:8000)
kpubdata-builder serve

# 커스텀 호스트/포트
kpubdata-builder serve --host 0.0.0.0 --port 8080
```

#### replay 모드 — 클라이언트 E2E 용 (#837)

공급자 API 대신 기록된 응답으로 서비스한다. 키 없이, 다른 저장소 checkout 없이 클라이언트
(Studio 실연동 E2E)가 Public API 경로를 돌릴 수 있다.

```bash
# 패키지에 포함된 fixture 로 (datago.air_station gangnam_full_page)
kpubdata-builder serve --replay

# fixture 디렉터리를 지정 — 포함된 세트를 꺼내 쓰거나 늘릴 때
kpubdata-builder fixtures export ./replay-fixtures
kpubdata-builder serve --replay-dir ./replay-fixtures
# 또는 KPUBDATA_BUILDER_REPLAY_DIR=./replay-fixtures kpubdata-builder serve
```

- kpubdata 의 `KPUBDATA_MODE`·`KPUBDATA_REPLAY_DIR` 는 Builder 가 내부에서 설정한다 —
  클라이언트는 그 이름을 몰라도 된다.
- fixture 가 있는 공급자에 키가 설정돼 있지 않으면 자리표시자 키를 넣는다(spec 실행기가
  전송 전에 키를 요구한다). 기록이 없는 요청은 실 API 로 나가 인증에 실패한다.
- `REQUIRE_OWN_PROVIDER_CREDENTIAL` 이 켜진 배포에서는 환경 키를 쓰지 않으므로 자리표시자도
  쓰이지 않는다. 개발·CI 전용 모드다.

### 웨어하우스 운영 — 회수·보존·백업 (#705)

`build --warehouse DIR` 가 커밋한 table snapshot 은 불변이고, 갱신은 새 snapshot 을
쓰고 포인터를 옮긴다(#699). 그래서 지우는 일은 따로 해야 한다.

```bash
# 테이블마다 최근 3개를 남기고 회수, 24시간 넘은 미커밋 snapshot 은 버려진 것으로 본다
kpubdata-builder warehouse-gc DIR --keep 3 --stale-hours 24
```

**`--stale-hours` 는 "빌드 한 번이 이보다 오래 걸리지 않는다" 는 운영자의 진술이다.**
카탈로그는 크래시로 멈춘 빌드와 느린 빌드를 구별하지 못한다 — 둘 다 `staging` 행이다.
기본값 24시간은 가장 긴 예약 빌드(서울 전월세 1,500회 호출)보다 넉넉히 길다. 빌드가
그보다 오래 걸리는 환경이면 값을 올린다. 너무 짧으면 진행 중인 빌드의 snapshot 을
버려진 것으로 표시하고, 그 빌드는 커밋 단계에서 실패한다(데이터는 잃지 않는다).

GC 가 **절대 지우지 않는** 것 — 모두 snapshot 을 `retiring` 으로 바꾸는 트랜잭션 안에서
확인한다:

| 보호 | 설정 |
|---|---|
| 어느 테이블이든 current snapshot | 자동 |
| 질의 lease 가 살아 있는 snapshot | 자동 (`resolve_current`·`pin`, 기본 1시간) |
| hold 가 걸린 snapshot — 저장된 분석·보존 기간·감사 | `TableCatalog.place_hold(snapshot_id, kind=..., reason=..., expires_at=...)` |

hold 에는 이유가 필수다 — 이유 없는 hold 는 아무도 풀지 못한다. `expires_at` 이 없으면
`release_hold` 할 때까지 유지된다. CLI 로도 건다(#797):

```bash
kpubdata-builder warehouse-hold DIR place SNAPSHOT --kind audit --reason "2026 감사" \
  [--expires-at 2027-01-01T00:00:00+00:00]   # hold id 를 출력한다
kpubdata-builder warehouse-hold DIR list SNAPSHOT
kpubdata-builder warehouse-hold DIR release HOLD
```

```bash
# catalog 와 snapshot 파일을 함께 백업 (대상 디렉터리는 비어 있어야 한다)
kpubdata-builder warehouse-backup DIR BACKUP

# 빈 디렉터리로 복원 — catalog 와 파일을 서로 대조한 뒤에만 복원한다
kpubdata-builder warehouse-restore BACKUP NEW_DIR
```

### 커밋된 테이블 읽기 (#797)

서버에 `--warehouse` 가 있으면 호출자가 자기 빌드로 커밋한 테이블을 HTTP 로 읽는다.
테이블 이름은 `<dataset_id>.<source_key>` 이고, 소유권을 강제하면 소유자마다 워크스페이스가
따로라 다른 소유자의 테이블은 404 다.

- `GET /warehouse/tables` — 테이블 목록과 current snapshot
- `GET /warehouse/tables/{name}` — 읽을 수 있는 snapshot 목록(최신순)
- `POST /warehouse/query` `{"table": ..., "snapshot": "current"|<id>, "sql": ..., "limit": ...}`
  — `current` 는 질의 시작 전에 한 번 snapshot id 로 해석되고 lease 로 고정된다. 질의 중
  커밋이 일어나도 읽는 것은 바뀌지 않고, 응답의 `snapshot.snapshot_id` 로 같은 질의를
  다시 돌릴 수 있다. SQL 샌드박스는 `POST /query` 와 같다(테이블 이름은 `dataset`).
- `GET /warehouse/tables/{name}/profile?snapshot=current|<id>` — 열 프로파일(#817). 행 수,
  null 수·비율, float 열의 NaN·무한대 수, 숫자·시간 열의 최소·최대를 **전 행에서 정확히**
  계산한다(표본 없음). NaN·무한대는 범위에서 빼고 `excluded_count` 로 센다. 값이 10개 미만인
  범위는 공개하지 않는다. 값 패턴이나 열 이름으로 개인정보가 의심되는 열은 BuildSpec 의
  `pii` 정책(`mode: allow` 또는 `allow_columns`)이 받아들이지 않는 한 통계를 전부 비운다.
  질의와 같은 한도(자식 프로세스·메모리 상한·동시 실행 슬롯)로 돌고, 결과는 스냅샷을 건드리지
  않고 `tables/<table_id>/_profiles/<snapshot_id>.json` 에 snapshot id·콘텐츠 다이제스트·
  알고리즘 버전과 함께 캐시된다. GC 가 스냅샷을 지우면 함께 지운다. 분위수·히스토그램·
  고유값·상위 값은 비용과 공개 위험을 따져 본 뒤로 미뤘다.

저장된 분석(#783)은 `POST /analyses` `{"name", "table", "snapshot", "sql", "limit"}` 로 만든다.
질의를 한 번 실행하고, 읽은 **구체적 snapshot id** 를 저장하며(`current` 를 저장하지 않는다),
lease 가 풀리기 전에 그 snapshot 에 `saved_analysis` hold 를 건다. `POST /analyses/{id}/run`
은 테이블이 갱신된 뒤에도 저장된 snapshot 을 읽는다. 결과 행은 저장하지 않고 컬럼·행 수·
truncated·실행 시각만 남긴다. `DELETE /analyses/{id}` 가 hold 를 푼다. 저장소는
`<output_root>/.service/analyses.sqlite3` 다.

복원은 다음 중 하나라도 어긋나면 **아무것도 복원하지 않고** 문제를 전부 나열한다:
snapshot 이 가리키는 테이블 존재, current 포인터가 커밋된 snapshot 을 가리킴, 각
snapshot 디렉터리 존재·비어 있지 않음·digest 일치. 파일이 빠진 백업이 빈 테이블로
복원되는 일은 없다. 백업은 읽을 수 있는 snapshot(`committed`·`quarantined`)과 hold 를
담고, lease 와 `staging`·`abandoned`·`retiring` snapshot 은 담지 않는다.

---

## HTTP API 인증

모든 HTTP 엔드포인트는 `X-API-Key` 헤더를 통한 인증을 지원합니다:

```bash
curl -X POST http://localhost:8000/validate \
  -H "Content-Type: application/json" \
  -H "X-API-Key: your-api-key" \
  -d '{"spec": "dataset_id: test\n..."}'
```

인증 실패 시 `401 Unauthorized` 응답이 반환됩니다.

자세한 내용은 [ADR 0006 — 서비스 인증 & 배포(Docker) 스토리](./adrs/0006-service-auth-and-deployment.md)와 [API_CONTRACT.md](./API_CONTRACT.md)를 참고하세요.

---

## CORS 설정

브라우저 클라이언트(Studio 등)와의 연동을 위해 크로스-오리진 요청을 허용해야 합니다.

```bash
# 허용할 오리진 설정 (콤마로 구분)
export KPUBDATA_BUILDER_ALLOWED_ORIGINS=http://localhost:5173,https://studio.example.com

# 인증 키 설정 (선택)
export KPUBDATA_BUILDER_API_KEY=your-secret-key

# 서버 시작
kpubdata-builder serve
```

**보안 참고:** default-deny 정책이 적용되므로, `KPUBDATA_BUILDER_ALLOWED_ORIGINS`를 설정하지 않으면 모든 크로스-오리진 요청이 거부됩니다. 로컬 개발 시에는 `http://localhost:5173`을 명시적으로 설정하세요.

---

## 성능 및 동시성

HTTP, 비동기 build, query의 서로 다른 동시성 상한과 리소스 산정 방법은
`docs/deploy.md`의 동시성·백프레셔 절을 참고하세요.

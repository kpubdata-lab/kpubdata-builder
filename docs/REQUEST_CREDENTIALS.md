# 요청에 실려 오는 키 — 어디서 보이고 언제 버려지는가

기준: 2026-10-07, `kpubdata-builder` `main` (계약 1.106.0), `kpubdata-studio` `main`,
`kpubdata` `main`. 이 저장소의 `docker-compose.prod.app.yml` 과 `ops/caddy/Caddyfile` 이
말하는 배포를 "실제 배포" 로 본다. 다른 프록시를 쓰는 배포는 2절의 표를 자기 체인으로
다시 채워야 한다.

이 문서는 **지금의 동작**을 적는다. `docs/CREDENTIAL_SURFACE.md` 는 2026-09-27 의 조사
기록이고, 그 뒤 #683·#686·#925·#1070·#1118 이 바꾼 것은 여기에 있다. 결정의 근거는
ADR 0012(2026-09-30 개정)와 ADR 0020, 키를 어디에 둘지의 결정은 kpubdata#825 다.

여기서 "키" 는 사용자가 넣는 **provider 키**(공공데이터 서비스 키)와 **publish
토큰**(Hugging Face, Kaggle)이다. 로그인 토큰(OIDC access token)과 `X-API-Key` 는 같은
구간을 지나지만 수명이 다르고, 이 문서의 대상이 아니다.

## 1. 어느 배포의 이야기인가

| 배포 | 키를 누가 주는가 | 저장 | 운영자 키로 대신 호출 |
|---|---|---|---|
| **다중 사용자** — `OIDC_ISSUER` 가 있거나 `ENFORCE_OWNERSHIP` 이 켜짐 | 사용자가 요청마다 헤더로 | **하지 않는다.** `PUT /providers/{provider}/credential` 은 403 `credential_storage_disabled` | **하지 않는다.** 환경변수의 키를 읽지 않는다 |
| 단일 사용자 | 운영자(= 사용자)가 환경변수나 저장 API 로 | AES-GCM 으로 암호화해 SQLite 에 저장 | 한다. `KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL` 로 끌 수 있다 |

2절과 3절은 **다중 사용자 배포**의 이야기다. 단일 사용자 배포에서는 운영자가 곧
사용자이고, 키는 디스크에 암호화되어 남으며 master key 를 가진 사람이 복호화할 수 있다
(#682, ADR 0020 — 결정된 한계). 그 배포에 아래의 수명 표는 적용되지 않는다.

다중 사용자 배포에서 키는 두 헤더로만 온다. URL 에는 싣지 않는다.

- `X-Provider-Key: <provider>=<key>` — 쉼표로 여럿
- `X-Publish-Credential: <VARIABLE>=<value>`

## 2. 구간별 — 키 원문을 볼 수 있는 곳

"볼 수 있다" 는 그 구간의 소프트웨어가 평문 헤더를 손에 쥔다는 뜻이다. 기록한다는
뜻이 아니다.

| 구간 | 암호화 | 키 원문 | 근거 |
|---|---|---|---|
| 브라우저 (Studio) | — | **있다.** 탭의 메모리에만. localStorage·sessionStorage·IndexedDB·쿠키·URL 에 쓰지 않는다 | studio `shared/lib/providerKeys.ts` — persist 없는 store |
| 브라우저 → Cloudflare | HTTPS | 전송 중에는 보이지 않는다 | |
| Cloudflare | TLS 를 여기서 한 번 푼다 | **볼 수 있다** | `Caddyfile` 머리말 — proxied DNS, SSL Full(strict) |
| Cloudflare → Caddy | HTTPS (origin 인증서를 검증) | 전송 중에는 보이지 않는다 | 같은 곳 |
| Caddy | TLS 를 다시 푼다 | **볼 수 있다.** 접근 로그는 켜져 있지 않다 (`log` 지시어 없음) | `ops/caddy/Caddyfile` |
| Caddy → Builder | **평문 HTTP.** 같은 호스트의 Docker 네트워크 `app-net` 안 | 그 네트워크를 볼 수 있으면 보인다 | compose `BACKEND_UPSTREAM: builder:8000` |
| Builder | — | **있다.** 프로세스 메모리에만 (3절) | `service/request_credentials.py` |
| Builder → provider | provider 와 spec 에 달렸다 (아래) | provider 는 당연히 받는다 | kpubdata spec 의 `endpoint.base_url` |

알아 둘 것:

- **Builder 의 8000 포트는 호스트의 루프백에만 묶인다** (compose 기본값). Caddy 없이
  이 포트를 밖으로 열면 키가 평문으로 인터넷을 지난다.
- **provider 로 가는 구간은 전부 HTTPS 가 아니다.** data.go.kr 계열은 키를 URL 쿼리
  (`serviceKey`)에 싣고, kpubdata 의 spec 가운데 `http://` 로 남은 것이 있다 — 2026-10-07
  에 23개이고 kpubdata#738 이 줄여 가고 있다. 그 spec 으로 만든 빌드에서는 **사용자의
  키가 Builder 와 provider 사이를 평문으로 지난다.** 목록은 kpubdata 의
  `scripts/insecure_http_baseline.txt` 다.
- **"저장하지 않는다" 는 "운영자가 절대 볼 수 없다" 가 아니다.** 키는 요청이나 작업이
  도는 동안 Builder 프로세스의 메모리에 있고, 그 프로세스의 메모리를 읽을 수 있는
  사람(호스트의 root)은 그 동안 키를 읽을 수 있다. Cloudflare 와 Caddy 를 운영하는
  쪽도 마찬가지로 지나가는 헤더를 볼 수 있는 자리에 있다.
- Caddy 의 접근 로그나 Cloudflare 의 요청 로깅을 켠다면, 두 헤더가 기록에서 빠지는지
  **켜기 전에** 확인해야 한다. 이 저장소의 설정은 켜지 않는다.

Builder 안에서 키가 가지 않는 곳 — 각각 테스트가 지킨다:

| 가지 않는 곳 | 지키는 것 |
|---|---|
| 디스크(작업 레지스트리 스냅샷, 이벤트, manifest, 산출물) | `tests/unit/test_ephemeral_credentials.py`, `test_canary_leak_gate.py` (#683, #686) |
| 로그 — URL 쿼리에 실린 키 포함 | `logging_redaction` 이 모든 레코드에서 지운다. 같은 canary 게이트 |
| 응답 본문과 오류 메시지 | `test_credential_headers_after_auth.py` (#1105) |
| 다른 사용자의 요청이나 작업 | 작업의 키는 제출한 소유자에게만 넘어간다 (`JobCredentials.take`) |
| 응답 캐시 | 다중 사용자 배포에서는 캐시를 끈다 (#684) |

## 3. 수명 — 키가 메모리에 있는 동안

| 일어나는 일 | provider 키 | 근거 |
|---|---|---|
| 동기 요청 (`POST /preview`, `POST /build`, provider test·probe·status) | 요청이 끝나면 사라진다 | `request_scope` |
| 비동기 제출 (`POST /builds`) | **그 spec 이 쓰는 provider 의 키만** run id 에 묶인다. 요청이 실은 다른 키는 묶이지 않는다 | #1070 |
| 큐에서 대기 | 제출한 때부터 `KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS` (기본 3600초)까지. 지나면 타이머가 그때 지운다. 그 작업은 차례가 왔을 때 `credentials_required` 로 끝난다 | #1070 |
| 워커가 가져간 뒤 (실행 중) | **빌드가 끝날 때까지.** 위의 시간과 무관하다. 빌드는 `KPUBDATA_BUILDER_BUILD_TIME_LIMIT_SECONDS`(기본 6시간)를 넘기면 멈추므로(#1119) 키를 쥐는 시간도 그 안이다 | `_run_build_job` |
| 성공·실패·취소로 끝남 | 사라진다 | 같은 곳의 `finally` |
| 대기 중인 작업을 사용자가 취소 | 그 자리에서 사라진다 | `cancel_build` |
| 서버가 종료 신호를 받음 | 대기 작업의 키는 그 자리에서 사라지고 작업은 `failed` 로 끝난다. 실행 중인 작업은 끝날 때 | #1118 |
| 프로세스 재시작 | **전부 사라진다.** 중단된 run 은 다음 기동 때 `credentials_required` 로 표시되고, 키 없이 다시 시작되지 않는다 | #683 |
| 사용자가 Studio 에서 키를 다시 입력 | 그 뒤의 요청부터 새 값이 간다. **이미 제출된 작업은 제출할 때 묶인 값을 그대로 쓴다** | `JobCredentials.bind` 가 값을 복사한다 |
| 사용자가 탭을 닫거나 새로 고침 | 브라우저가 키를 잊는다. 서버 쪽은 아래 "로그아웃" 과 같다 | studio `providerKeys.ts` |
| **사용자가 로그아웃** | 브라우저가 키를 잊는다. **서버에서 대기·실행 중인 작업은 취소되지 않고, 그 작업에 묶인 키도 위 표대로 남는다** — 대기 중이면 TTL 까지, 실행 중이면 빌드가 끝날 때까지 | studio `features/auth/store.ts`; Builder 에 로그아웃을 받는 경로가 없다 |

**로그아웃은 서버의 작업을 멈추지 않는다.** 로그아웃 뒤에 키가 서버에 남지 않게
하려면 그 전에 작업을 취소해야 한다. 이것은 결정이다(#1070).

- 빌드는 제출하고 자리를 떠도 끝나도록 만든 비동기 작업이다. 로그아웃이나 탭 닫기가 작업을
  취소하면 오래 걸리는 빌드는 화면을 켜 둔 채로만 돌릴 수 있다.
- 로그아웃은 브라우저와 로그인 서비스 사이의 일이고 Builder 는 그 사실을 통보받지 않는다.
  취소로 이어지게 하려면 브라우저가 떠나기 전에 보내는 요청에 기대야 하는데, 탭을 닫거나
  네트워크가 끊기면 그 요청은 오지 않는다. 지켜지지 않을 수 있는 약속은 하지 않는다.
- 작업에 묶인 키가 남는 시간에는 이미 상한이 있다: 대기 중에는 TTL, 실행 중에는 빌드 실행
  시간 상한까지다. 그 전에 지우고 싶은 사용자는 작업을 취소한다.

**키의 대기 시간은 제출한 때부터 센다.** #1070 은 처음에 "TTL 보다 오래 대기한 작업도 키를
가지고 실행된다"를 요구했지만, 그러려면 대기열이 비워질 때까지 키를 기한 없이 메모리에 두어야
한다. 같은 이슈가 요구한 "키를 무기한 보관하지 않는다"와 함께 지킬 수 없어 뒤쪽을 택했다
(#1128). TTL 을 넘겨 대기한 작업은 `credentials_required` 로 끝나고, 사용자는 키를 다시 넣어
같은 정의로 새 실행을 제출한다.

publish 토큰은 더 짧다. 게시는 요청 안에서 끝나는 동기 경로라 토큰은 그 요청 동안만
메모리에 있고, 작업에 묶이지 않는다 (#925). 게시가 비동기가 되면(#1126) 이 문단이
바뀐다.

## 4. 금지되는 것 — 다중 사용자 배포

- **영속 저장.** provider 키도 publish 토큰도 디스크에 쓰지 않는다. 저장 API 는 거부한다.
- **운영자 키로의 fallback.** 사용자의 키가 없으면 운영자의 환경변수 키로 대신 호출하지
  않는다. 키가 필요한데 없는 요청은 provider 를 부르기 전에 400
  `provider_credential_required` 로 답한다 — preview, 동기 빌드, 비동기 제출 모두 (#1070).
- **다른 사용자의 키나 공유 캐시로 대신 호출.** 하지 않는다 (#684).

단일 사용자 배포는 이 셋의 예외다. 운영자가 넣은 환경변수 키로 호출하는 것이 그 배포의
정상 동작이고, 키를 요청마다 받지 않는다. 한 배포가 두 방식을 섞지 않는다 —
`OIDC_ISSUER` 가 있으면 값과 무관하게 다중 사용자 규칙이 적용된다 (#635).

## 5. 확인하지 못한 것

적지 않으면 "문제없다" 로 읽히므로 적는다.

- **실제 운영 환경의 Cloudflare·Caddy 설정.** 이 문서는 저장소의 설정 파일을 읽은
  것이다. 배포된 인스턴스의 로깅·WAF 설정은 보지 못했다.
- **프록시 체인의 최종 형태.** Cloudflare Tunnel 과 nginx 를 쓰는 안이 검토 중이다
  (#1098). 채택되면 2절의 Cloudflare·Caddy 행이 바뀐다.
- **크래시 덤프와 코어 파일.** Python 의 기본 traceback 은 지역 변수를 싣지 않지만,
  코어 덤프를 남기는 배포라면 그 안에 메모리의 키가 들어간다. 배포가 정한다.
- **provider 쪽에서의 취급.** provider 가 받은 키를 어떻게 기록하는지는 알 수 없다.
- 2절의 "전송 중에는 보이지 않는다" 를 패킷으로 확인한 것은 아니다. 설정이 말하는
  것이다.

이 문서가 적은 값(헤더 이름, 기본 TTL, 환경변수 이름, 프록시의 upstream, 접근 로그
여부)이 코드·설정과 어긋나면 `tests/unit/test_request_credentials_doc.py` 가 실패한다.

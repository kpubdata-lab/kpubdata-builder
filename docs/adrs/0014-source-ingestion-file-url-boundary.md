# ADR 0014: Public API·File·URL Source 통합 경계

- 상태: 승인됨
- 관련 이슈: #498, #484(parent)

## 배경

Builder는 지금까지 `sources[].provider`/`dataset`을 통해 kpubdata 호환 client로만
데이터를 가져왔다. Studio prototype(`kpubdata_ui_prototype_v1.html`)의 Add Data
흐름은 Public API 외에도 사용자가 올린 파일(File Upload)과 사용자가 지정한 외부
HTTP(S) endpoint(URL/REST)를 같은 Preview→Build 흐름으로 다뤄야 한다. 두 신규
경로는 서로 다른 보안 위협을 새로 끌어들인다.

- File Upload: 서버가 사용자 content를 저장·재사용해야 한다 — 저장 위치, 소유권
  격리, filename을 filesystem path로 오용하는 경로 주입이 문제가 된다.
- URL/REST: 서버가 사용자가 지정한 임의 endpoint로 직접 요청을 보내야 한다 —
  서버를 프록시 삼아 내부망(loopback/private/link-local/클라우드 metadata IP
  등)을 스캔·접근하는 SSRF가 문제가 된다.

## 결정

세 source kind(`public_api`/`file`/`url`)를 `SourceRef.kind`로 구분하되, 셋
모두 기존 Bronze→Silver→Gold pipeline과 `BronzeArtifact`/`SourceProvenance`
계약을 그대로 재사용한다(새 pipeline을 만들지 않는다). `kind`를 생략한 기존
source는 항상 `public_api`로 해석되어 기존 BuildSpec은 수정 없이 동작한다.

`stages.bronze.resolve.build_bronze_artifact_for_source()`가 kind별 resolver를
호출해 동일한 `BronzeArtifact`로 수렴시키는 단일 진입점이다. Silver 이후 단계는
소스가 어떤 kind였는지 전혀 알 필요가 없다. `source_identity()`가 모든 kind에
대해 기존 `(provider, dataset)` 자리를 채우는 canonical identity를 계산해,
provenance/manifest/output 디렉터리 세그먼트가 새 필드 없이 기존 계약 그대로
동작하게 한다 — file은 `("file", upload_id)`, url은 `("url", <경로-안전
slug>)`다. url의 사람이 읽는(query 제거된) endpoint는 별도로
`fetch_params.endpoint`에만 담기고 경로/식별자에는 쓰이지 않는다.

### File Upload

- `POST /uploads`는 JSON이 아니라 raw binary body를 받는다(multipart 대신
  동등한 binary upload). `format`/`encoding`/`filename`은 query parameter다.
- content는 파일시스템이 아니라 SQLite BLOB(`uploads.sqlite3`)에 저장한다 —
  path traversal 표면 자체를 없앤다. `upload_id`는 서버가
  `secrets.token_hex`로 발급하는 불투명한 식별자(`upl_<hex32>`)이며 사용자가
  filename/path를 직접 지정할 수 없다.
- 모든 조회/삭제는 `(owner_id, upload_id)`로 scoping된다. 다른 owner의
  upload_id는 존재 여부를 구분하지 않고 동일하게 not found로 처리한다
  (fail-closed, #505 ownership 패턴과 동일).
- 업로드 시점에 즉시 파싱을 시도해(`ingestion.tabular_ingest`) 손상·빈 파일을
  fail-fast로 거부한다. `sources[].format`/`encoding`은 업로드 시점에 검증된
  값과 정확히 일치해야 한다 — BuildSpec이 업로드와 다른 해석을 강제할 수 없다.
- `BuilderService`는 업로드 저장소를 지연 생성한다: file source가 없는
  preview/build는 `.service/uploads.sqlite3`를 만들지 않는다(기존 "preview는
  파일을 쓰지 않는다" 계약 유지).

### URL/REST Safe Fetch (P0)

P0는 GET, Auth=None만 지원한다. Bearer credential 연동은 #492 이후 P1이다.
`ingestion.url_fetch.safe_fetch_get()`이 다음을 강제한다.

- scheme은 `https`만 허용한다(file/ftp/http 등은 구조적으로 거부).
- URL에 userinfo(`user:pass@host`)가 있으면 거부한다.
- hostname을 `socket.getaddrinfo`로 직접 resolve하고, 모든 결과가
  `ipaddress.*.is_global`(공인 주소)일 때만 진행한다 — 하나라도 비공인이면
  전체를 거부한다(fail-closed). IP 리터럴 hostname도 이 경로를 통과한다.
- 실제 TCP 연결은 검증한 IP에 직접 연다(`_PinnedHTTPSConnection`) — Host
  header/TLS SNI는 원본 hostname을 유지한다. hostname으로 다시 resolve해
  연결하면 검증 시점과 연결 시점 사이 DNS 응답이 바뀌는 DNS rebinding으로
  검증을 우회당할 수 있어, 검증한 IP를 그대로 재사용한다.
- redirect는 자동으로 따라가지 않고 매 hop마다 위 검증을 처음부터 반복한다
  (최대 5회, 초과 시 거부).
- 응답 크기 상한(기본 20 MiB, 환경변수로 조정)과 connect/read timeout을
  강제한다.
- 사용자 정의 header를 보내지 않는다 — BuildSpec `url` source 계약에 header
  필드 자체가 없어 임의 header/POST/PUT/PATCH를 표현할 수 없다.

## 결과

- 세 source kind가 동일한 Bronze artifact 계약과 pipeline을 공유해 Silver/Gold
  구현이 kind 분기를 추가로 갖지 않는다.
- File Upload와 URL Fetch 모두 provenance/manifest/output 경로에 로컬
  파일시스템 경로나 secret이 될 수 있는 query string을 남기지 않는다.
- 파일 업로드는 SQLite BLOB 저장으로 path traversal 표면이 없고, URL fetch는
  DNS rebinding까지 방어하는 IP-pinned 연결로 SSRF를 구조적으로 차단한다.
- URL Bearer credential, 대용량 업로드 자동 정리(TTL sweep), 비동기 build
  job(`POST /builds`)으로의 owner_id 전파는 이 결정의 범위가 아니다 — 특히
  비동기 경로는 현재 `owner_id`를 만들지 않으므로, file source를 참조하는
  build는 동기 `POST /build`로만 안정적으로 동작한다(비동기 경로는 owner_id
  부재로 해당 소스가 명확한 오류로 실패한다).

> **2026-10-07 보충** — 위 마지막 항목은 이 결정을 내릴 때의 한계였고 지금은 사실이
> 아니다. #998 부터 비동기 job 은 제출한 소유자를 file source resolver 에 넘기므로
> (`BuilderService._run_build_job` 의 `owner_id`), 업로드한 파일을 쓰는 build 도
> `POST /builds` 로 돈다. Studio 는 파일 build 를 비동기 경로로 제출한다
> (kpubdata-studio#786). 업로드 보존 기간 뒤의 자동 정리도 #1045 로 들어왔다. URL
> source 는 다중 사용자 배포에서 거부된다(#685).

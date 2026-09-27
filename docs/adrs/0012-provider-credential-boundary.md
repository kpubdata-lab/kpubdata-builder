# ADR 0012: 사용자별 Provider Credential 저장과 Client 격리

- 상태: 승인됨 — **그러나 제품 원칙과 충돌한다 (아래 참조)**
- 관련 이슈: #492, #505, `yeongseon/kpubdata#263`, #681, #682

> **이 ADR 은 credential 을 저장한다는 전제 위에 있다.** 그런데 POLICY 1.2 는
> "키는 요청·작업이 도는 동안 메모리에만 두고 영속 저장하지 않는다" 고 적는다.
> 두 문서가 모두 현행이고 정면으로 어긋난다.
>
> 이 ADR 이 틀렸다는 뜻이 아니다 — 어느 쪽이 제품의 결정인지가 **아직 정해지지
> 않았다.** #682 가 그것을 정하고, 정해지면 이 ADR 은 유지되거나 대체된다.
> 그때까지 여기 적힌 것이 구현 상태다.
>
> 관련 사실: 암호화 저장은 비저장이 아니다. master key 를 가진 운영자는 모든
> 사용자의 키를 복호화할 수 있다. 그리고 #681 의 조사에서 **데이터 조회 경로에는
> 차단 스위치가 아예 없다는 것**이 드러났다 — `service/providers.py:101` 이
> 요청자 키가 없으면 운영자 키로 폴백하고, publish 쪽의
> `REQUIRE_OWN_PUBLISH_CREDENTIAL` 에 해당하는 것이 없다.

## 배경

Studio의 Provider 설정은 인증된 사용자별 credential CRUD와 Preview/Build/Test의
일관된 credential 사용을 요구한다. Builder는 #505의 stable `owner_id`를 이미
canonical ownership identity로 사용한다. kpubdata는 공개 `Client(provider_keys=...)`
주입 지점을 제공하지만 credential 저장이나 사용자 ownership은 책임지지 않는다.

## 결정

Builder가 `(owner_id, provider)`를 key로 credential을 저장한다. 저장소 abstraction은
원문을 AES-256-GCM으로 암호화하고 DB에는 ciphertext만 기록한다. master key는
`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` 환경변수로 별도 주입하며 credential DB에
기록하지 않는다.

Credential 해석 우선순위는 다음으로 고정한다.

1. 현재 principal의 저장 credential
2. 서버의 kpubdata 기본 credential 환경설정
3. not configured

Preview, Build, Provider Test는 같은 resolver를 사용한다. 요청마다 새 kpubdata
Client를 만들고 닫는다. 또한 `KPUBDATA_CACHE=1` 여부와 무관하게 principal credential을
사용하는 service Client에는 `cache=False`를 명시한다. kpubdata#263이 해결되기 전에는
credential이 response cache key에 포함되지 않으므로, credential-sensitive response
cache를 사용자 사이에서 재사용하지 않는 것이 필수다. Builder는 kpubdata provider
로직을 복제하지 않고 공개 `provider_keys`, runtime catalog, 구조화된 예외만 사용한다.

API는 raw credential과 `owner_id`를 반환하지 않는다. credential GET/PUT 응답은
configured, 고정 masked 값, updated_at 등 metadata만 포함한다. Provider failure는
`auth|network|timeout|provider|unknown`으로 제한해 반환하고 원문 예외 메시지는
응답하지 않는다. response code는 kpubdata 구조화 예외가 제공할 때만 노출한다.

## 결과

- cross-user credential 조회와 client 재사용을 구조적으로 차단한다.
- master key 미설정 시 credential CRUD는 fail-closed(503)하지만 기존 비-credential
  Builder API와 서버 기본 credential은 계속 사용할 수 있다.
- OAuth consent, 조직 공유 credential, arbitrary Base URL override는 이 결정의 범위가
  아니다.

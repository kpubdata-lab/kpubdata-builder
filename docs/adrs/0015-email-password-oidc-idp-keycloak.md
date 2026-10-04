# ADR 0015 — 사용자 인증 IdP: 이메일/비밀번호-capable OIDC(Keycloak) 전환

- 상태: 승인됨(Accepted)
- 관련 이슈: #515(본 결정), #383(ADR 0009), #505(Stable Principal ID)
- 선행 문서: [ADR 0006](./0006-service-auth-and-deployment.md), [ADR 0009](./0009-user-authentication-google-oidc.md)(본 ADR로 대체됨)
- 관련 문서: [BOUNDARY.md](../BOUNDARY.md), [deploy.md](../deploy.md)

> ADR 0009가 "제안됨" 상태로 둔 Google OIDC 기준선과 UI vNext(#246, Studio #263)가
> 요구하는 email/password Login/Signup이 충돌한다. 본 ADR은 **사람 사용자 인증의
> IdP를 확정**하고 ADR 0009의 대체 정책을 정의한다. 인가(run 소유권)는
> #505(Stable Principal ID, CLOSED)가 이미 정본을 마련했다.

## 결정 사항 (이슈 #515)

1. **Builder 검증 계약은 표준 OIDC를 유지한다** — Builder는 특정 IdP를 가정하지
   않는다. 이미 구현된 `OIDC_ISSUER` + `OIDC_AUDIENCE`(필수, fail-closed) +
   JWKS/discovery 캐시(#435) + RS256 고정 검증이 그대로 사용자 인증의 정본 계약이다.
   본 전환은 Builder 코드가 아니라 **설정(issuer/audience 값)** 만 바꾼다.
2. **IdP: self-hosted Keycloak** — email/password Signup·Login, email verification,
   password reset, refresh, logout을 Keycloak이 담당한다(§검토한 대안 참조).
   Google 로그인은 Keycloak의 identity broker(선택적)로 수용해 기존 Google 사용자
   경로를 보존할 수 있다.
3. **인증 플로우: Authorization Code + PKCE** — Studio(public SPA client)는
   Keycloak hosted login 페이지로 이동해 code 교환으로 토큰을 받는다.
   **access token**을 `Authorization: Bearer`로 Builder에 전송한다 — 검증 방식(JWKS
   오프라인 검증, iss/aud/exp)은 ADR 0009와 동일하다. (2026-10-04 정정,
   kpubdata-studio#722: 이 문장은 "ID token"이라고 적혀 있었지만 Studio 는 처음부터
   access token 을 보냈고 Builder 는 토큰 종류를 가리지 않는다. API 호출에는 access
   token 이 OAuth 의 통상 용법이므로 그쪽을 계약으로 정한다. access token 의 `aud` 에
   Builder audience 를 넣는 mapper 가 필요하다 — `docs/deploy.md`.) Resource Owner Password Credentials(ROPC) grant는 사용 금지
   (deprecated, MFA/SSO/세션 관리 불가).
4. **password/secret 책임 경계(원칙 재확정)** — Builder와 Studio 어느 쪽도
   password 원문·해시를 저장·검증·전송하지 않는다. Studio의
   `mockAuthProvider`(#289)는 실연동 전 로컬 개발용 mock일 뿐 실 IdP가 아니다.
5. **allowlist는 2층으로 유지** — (1) Keycloak realm 정책이 signup·계정 수명을
   관리한다(기본 public signup **OFF**, 관리자 초대/이메일 도메인 제한).
   (2) Builder env allowlist(`OIDC_ALLOWED_SUBJECTS`/`OIDC_ALLOWED_EMAILS`,
   fail-closed)를 심층 방어로 유지한다. 공개 IdP가 아니게 되므로 Google 전용
   `OIDC_ALLOWED_HD`는 `hd` claim 의존을 제거하고 이메일 도메인 판정으로
   일반화한다(구현 후속 이슈에서 반영).
6. **profile metadata 최소 PII** — Builder는 stable principal(`sha256(issuer‖sub)`,
   #505)과 표시용 `name`/`email`(ID token claim)만 다룬다.
   `account_type`/`organization_name`은 IdP 커스텀 claim 또는 principal-keyed
   Builder 프로필 후속 이슈에서 정의하되, 본 ADR은 "password 없음 + 최소 수집"만
   확정한다.
7. **service principal(`X-API-Key`) 병행 유지** — 스케줄 워크플로는 사람 로그인이
   불가하므로 ADR 0006의 서비스 키 경로를 그대로 유지한다. 두 경로 모두
   `Principal`(`oidc`/`service`/`dev`)로 정규화된다(변경 없음).

## 배경

- Studio vNext는 email/password Login/Signup UX를 요구한다(Studio #263, #289 —
  generic `AuthProvider` 계약은 이미 본 결정을 기다리도록 설계됨).
- ADR 0009의 Google OIDC 기준선은 공개 IdP 특성상 허용 목록이 유일 방어선이며,
  이메일/비밀번호 가입 정책·계정 수명 관리를 Builder가 통제할 수 없다.
- ADR 0009 결정 3(Internal ingress — Builder는 공개 인그레스를 갖지 않음)은
  유지한다. 따라서 IdP도 내부망에서 운영 가능해야 한다.

## 검토한 대안 (IdP)

| 대안 | 판정 | 근거 |
| :--- | :--- | :--- |
| **Keycloak(self-hosted)** | **채택** | 완전한 OIDC OP. email/password·verification·reset·logout 기본 제공. internal ingress 원칙과 정합(같은 VPC). Google broker 내장. 운영 부담(컨테이너+DB)은 단일 realm 수준으로 억제 가능. |
| Auth0/Clerk/Okta(SaaS) | 기각 | 외부 SaaS 의존·데이터 주권·비용. internal ingress 모델과 어긋남(로그인 페이지가 공개 인터넷 경유). |
| Zitadel(self-hosted, Go) | 기각 | Keycloak 대비 경량이나 생태계·레퍼런스 부족, 한국어 자료 희소. |
| Supabase Auth/Firebase Auth | 기각 | 완전한 OIDC OP가 아니거나(lock-in) 이메일 정책 커스텀 한계. |
| Builder 자체 계정(비밀번호 저장) | 기각 | ADR 0009 대안 1A와 동일한 기각 사유 — password 저장·해시·로테이션 부담과 취약면. |

## Studio ↔ Builder 설정 분리

| 항목 | Studio(public, 번들 포함 가능) | Builder(secret/서버 env) |
| :--- | :--- | :--- |
| issuer | `VITE_OIDC_ISSUER`(Keycloak realm URL) | `OIDC_ISSUER`(동일 값) |
| client/audience | `VITE_OIDC_CLIENT_ID` | `OIDC_AUDIENCE`(Builder를 가리키는 audience) |
| allowlist | — | `OIDC_ALLOWED_SUBJECTS`/`OIDC_ALLOWED_EMAILS`(최소 1개 필수, fail-closed) |
| secret | **없음**(public client + PKCE) | API 키/시크릿 기존 정책 유지 |

## ADR 0009와의 관계(전환 정책)

- ADR 0009는 **대체됨(Superseded by 0015)**. 단, 대체되는 것은 "Google 공개 IdC를
  직접 audience로 쓴다"는 부분뿐이고, 아래는 그대로 승계된다.
  - ID token 오프라인 검증 원칙(JWKS·RS256·iss/aud/exp, `pyjwt[crypto]`)
  - Bearer 전송 계약과 401/403/503 의미론
  - env allowlist fail-closed 정책
  - internal ingress 기본 배포형
  - `X-API-Key` 서비스 경로 병행
- 전환 절차(구현 후속): (1) Keycloak realm 구축 + `kpubdata-builder` audience
  등록 → (2) Builder env 교체(issuer/audience/allowlist) → (3) Studio
  `AuthProviderId`에 keycloak provider 추가(`"google" \| "mock"` 어휘 확장) →
  (4) 기존 Google 사용자는 broker 또는 이메일 기반 계정 연결로 마이그레이션.
    기존 run 소유권은 issuer+sub 해시(#505)로 귀속되므로, Keycloak 전환 후
    **principal이 달라지면 기존 run이 새 계정에서 보이지 않는다** — 마이그레이션
    시 구 owner_key → 신 owner_key 매핑 작업이 별도 필요하다(후속 이슈).

## 영향

- Builder: 코드 변경 없음(설정 값만 교체). `OIDC_ALLOWED_HD` 일반화는 후속.
- Studio: `AuthProvider` 구현체 추가(keycloak), 로그인 UX는 hosted login 위임.
- 인프라: Keycloak 컨테이너 + 영구 볼륨(또는 managed DB)이 내부망에 추가된다.
- API 계약: 변경 없음(bearerAuth 스키마 동일).

## 미해결 질문 (후속 이슈로 분리)

- owner_key 마이그레이션 매핑 방식(일괄 스크립트 vs 이중 허용 기간) — 전환 시점에 결정.
- Keycloak 고가용성/백업 정책 — 운영 도입 시 `deploy.md`에 보강.
- `account_type`/`organization_name`의 저장 위치(IdP claim vs Builder 프로필).
# 2026-09 정책 보충

Keycloak realm의 account/signup 정책은 공개 사용자 접근의 1차 경계다. Builder OIDC
allowlist는 필요한 제한 배포에서 활성화하는 선택적 2차 제한이며, allowlist를 비워도
OIDC 검증(issuer, audience, JWKS 서명, exp, RS256 fail-closed)을 우회하지 않는다.

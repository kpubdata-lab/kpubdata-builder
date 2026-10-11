# ADR 0009 — 사용자 인증 모델: Google OIDC ID 토큰 검증

- 상태: 대체됨(Superseded by [ADR 0015](./0015-email-password-oidc-idp-keycloak.md)) — 검증 계약·Bearer 전송·allowlist fail-closed·internal ingress는 0015가 승계
- 관련 이슈: #383, #312(ADR 0006), #384(B2 Principal), #385(B3 Google 검증), #386(B4 허용 목록), #387(B5 계약 1.1.0)
- 관련 문서: [ADR 0006](./0006-service-auth-and-deployment.md), [API_CONTRACT.md](../API_CONTRACT.md), [BOUNDARY.md](../BOUNDARY.md)

> ADR 0006이 "키 로테이션·다중 소비자·스코프는 후속"으로 유예한 항목 중 **사람 사용자 인증**을 결정한다. 본 ADR은 인증(authentication)에 한정하며, 인가(run 소유권)는 C1/C2(#388/#389, v0.5)로 분리한다.

## 결정 필요 사항 (이슈 #383)

1. 노출 최소화(internal ingress)를 기본 배포 형태로 확정할 것인가.
2. Google ID 토큰 오프라인 검증 채택 여부 — 대안: 자체 계정, oauth2-proxy, Studio BFF.
3. 허용 목록의 위치(env vs 저장소)와 미설정 시 fail-closed 정책.
4. 인가(run 소유권)를 어느 단계까지 이번 범위에 포함할 것인가.

## 배경

현재 인증은 단일 정적 `X-API-Key`(ADR 0006)만 지원한다. 이는 신뢰 네트워크/단일 소비자(Studio, 스케줄 워크플로)에는 충분하나, 사람 사용자가 직접 Builder를 호출하는 운영 시나리오에서는 한계가 있다.

- Studio는 서버 런타임이 없는 정적 SPA(GitHub Pages)라 BFF를 둘 수 없다 → Builder가 직접 Bearer를 검증해야 한다.
- 서명 검증을 직접 구현하면 취약점 위험이 크다 → 검증 라이브러리 필수.

## 검토한 대안

### 1. 인증 주체

- **A. 자체 계정(사용자/비밀번호)** — Builder가 자격증명 저장·검증. 암호 해시·로테이션·세션 관리 부담. 권장하지 않음.
- **B. oauth2-proxy / 리버스 프록시** — Builder 앞에 별도 프로세스. 정적 배포·단일 컨테이너 기준 인프라 부담 과대.
- **C. Studio BFF** — Studio에 서버 런타임 필요. Studio 구조(정적 SPA)와 충돌.
- **D. Google OIDC ID 토큰 오프라인 검증** — Builder가 JWKS로 직접 서명 검증. 별도 토큰 교환 없이 GIS 콜백의 `credential`(ID token JWT)을 그대로 `Authorization: Bearer`로 받아 검증.

### 2. 허용 목록

Google은 **공개 IdP**다. 계정만 있으면 누구나 서명·`aud` 검증을 통과하는 토큰을 받는다. 허용 목록 없이 배포하면 Builder가 인터넷 전체에 열린다(Entra ID/Keycloak과 달리 이는 선택이 아니다).

- **A. env 기반**(`OIDC_ALLOWED_HD`/`OIDC_ALLOWED_SUBJECTS`/`OIDC_ALLOWED_EMAILS`) — 단순, 배포와 동일한 설정면. `OIDC_ISSUER` 설정 시 3종이 모두 비어 있으면 기동 거부(fail-closed).
- **B. 저장소 기반**(DB/파일) — 동적 갱신 가능하나 인프라 추가.

### 3. 노출 범위

- **A. internal ingress 기본** — Builder는 공개 인그레스를 갖지 않는다(Studio가 same-network/VPC에서 호출). 공격면 최소화.
- **B. 공개 인그레스 + 인증** — 인터넷 노출. 허용 목록이 유일 방어선.

## 권고 (제안)

본 ADR은 **제안됨** 상태로, 아래 방향을 후속 논의의 기준선으로 제시한다.

1. **인증 주체: 대안 D(Google OIDC 오프라인 검증)** — `pyjwt[crypto]` extra 도입, 서명 검증 직접 구현 금지. `PyJWKClient`로 JWKS 캐시·키 회전(TTL 3600s). `algorithms=["RS256"]` 고정(`alg: none`/HS* 거부), `iss`/`aud`/`exp`/`nbf`/`iat`(60s leeway)/`email_verified` 검증. 미해결 `kid` → 1회 재조회(레이트 리밋 필수, JWKS DoS 방지). 상세는 #385(B3).
2. **허용 목록: 대안 A(env, fail-closed)** — `OIDC_ISSUER` 설정 시 `OIDC_ALLOWED_HD`/`OIDC_ALLOWED_SUBJECTS`/`OIDC_ALLOWED_EMAILS` 중 최소 하나는 필수, 아니면 기동 거부. 허용 목록 밖은 403(인증 성공·인가 실패). 상세는 #386(B4).
3. **노출: 대안 A(internal ingress 기본)** — "Builder는 공개 인그레스를 갖지 않는다"를 배포 기본형으로. 허용 목록은 심층 방어.
   > **2026-10-11 보충** 이 결정은 [ADR 0023](./0023-public-endpoint-with-auth-and-cors.md) 이 대체했다. Studio 가 정적 SPA 가 되어 사용자의 브라우저가 Builder 를 직접 부르므로, Studio 가 붙는 배포의 기본형은 공개 엔드포인트 + 인증 + CORS allowlist 다.
4. **인가 범위: 본 ADR은 인증만** — run 소유권(C1/C2, #388/#389)은 v0.5로 분리. B2(#384, Principal 추상화)가 이미 토대를 마련했다.
5. **서비스 계정은 `X-API-Key` 병행 유지** — 스케줄 워크플로는 Google 로그인이 불가하므로 `X-API-Key`를 유지한다. 두 인증 경로 모두 `Principal`(kind=`oidc`/`service`/`dev`)로 정규화.

> ID token 수명(약 1시간)이 긴 빌드보다 짧다. 동기 `/build`는 요청 시점 토큰으로 검증하고, 비동기 job(#334/ADR 0008) 도입 시 job은 제출 시점 principal로 고정 후 폴링은 새 토큰으로 소유권만 대조한다.

## 영향

- 신규 `auth` extra(`pyjwt[crypto]>=2.8,<3`). `OIDC_ISSUER` 미설정 시 Bearer 비활성(기존 배포 무영향).
- `service/auth.py`의 `authenticate()`(#384)가 Bearer 경로를 추가해 `Principal(kind="oidc", identifier=sub)` 반환.
- API 계약 1.1.0(#387, B5)에 `bearerAuth` securityScheme 추가. 401/403/503 응답 스키마 명시.
- Studio S2-S10(#187-#195)이 본 ADR에 수렴 — GIS 로그인, 메모리 토큰 보관, 오리진 정합.
- `VITE_GOOGLE_CLIENT_ID`는 번들에 포함 가능(public). **Builder API 키를 `VITE_*`로 주입 금지** — 번들에 평문 노출.

## 미해결 질문

- 인가(C1/C2)에서 기존 run의 `created_by`가 NULL인 경우 — service principal 공개 vs 하위호환 플래그? (본 ADR은 인증만 다루므로 C1에서 결정)
- JWKS 조회 실패 시 503(일시 장애) vs 401(거부) — 권고는 503, 확정은 #385에서.
- 허용 목록 변경 시 재기동 없이 반영할 것인가(env는 재기동 필요)?

# ADR 0023 — Builder 는 공개 엔드포인트를 갖는다: 경계는 인증과 CORS allowlist

- 상태: 승인됨 — 2026-10-11, 소유자가 결정을 위임(#1215)
- 관련 이슈: #1215 (이 ADR), #1098·#1235 (프록시 신뢰 체인), kpubdata-lab/kpubdata-studio#841
- 관련 문서: [ADR 0006](./0006-service-auth-and-deployment.md), [ADR 0009](./0009-user-authentication-google-oidc.md),
  [ADR 0015](./0015-email-password-oidc-idp-keycloak.md), [ADR 0017](./0017-fullstack-oci-deployment.md), [deploy.md](../deploy.md)

> 번호 0022 는 SQLite 단일 상태와 CUBRID 제거를 다루는 ADR(#1093)을 위해 비워 둔다.

## 맥락

ADR 0009 의 결정 3(ADR 0015 가 승계)은 "Builder 는 공개 인그레스를 갖지 않는다(internal ingress)"를
배포 기본형으로 정했다. 그 전제는 Studio 가 Builder 와 같은 네트워크에서 Builder 를 부른다는 것이었다.

지금의 구조는 그렇지 않다.

- Studio 는 정적 SPA 다. Builder 를 부르는 것은 Studio 의 서버가 아니라 **사용자의 브라우저**다.
- 참조 배포(ADR 0017, `docs/deploy.md` §12)는 Cloudflare → Caddy → Builder 로 공개 주소를 낸다.
  `docker-compose.prod.app.yml`, `ops/caddy/Caddyfile`, CORS 설정이 모두 그 주소를 전제한다.
- 그 경로에서 클라이언트 주소를 어떻게 믿을지는 #1235 로 정해졌다.

결정과 실제 구조가 달랐고, `docs/deploy.md` §1 은 그 차이를 적어 둔 채 결정을 기다리고 있었다.

## 검토한 대안

| | 내용 | 얻는 것 | 잃는 것 |
|---|---|---|---|
| **A. 공개 엔드포인트 + 인증 + CORS allowlist** | 지금의 참조 배포를 기본형으로 받아들인다 | Studio 를 정적 호스팅만으로 배포할 수 있다. 지금 있는 구성·문서·테스트가 그대로 맞다 | Builder 가 인터넷에서 닿는다. 경계는 네트워크 위치가 아니라 인증이다 |
| B. Studio 앞에 같은 오리진의 프록시 | Studio 를 서빙하는 쪽이 `/api` 를 Builder 로 넘긴다 | CORS 가 필요 없고 Builder 는 내부에 남는다 | Studio 가 정적 호스팅만으로는 배포되지 않는다. 프록시를 운영해야 하고, 그 프록시가 결국 같은 요청을 인터넷에서 받는다 |

B 에서 Builder 가 "내부에 남는다"는 것은 주소가 하나 줄어든다는 뜻일 뿐, 인터넷의 요청이 Builder 의
같은 코드에 닿는다는 점은 A 와 같다. 인증을 통과하지 못한 요청을 막는 것은 두 대안 모두에서 Builder 다.

## 결정

**A 를 기본형으로 한다.** Builder 는 사용자의 브라우저가 부르는 공개 엔드포인트를 갖는다. 그 주소로
들어오는 것을 좁히는 것은 다음이다.

1. **인증.** `/healthz` 같은 무인증 프로브를 빼면 모든 요청은 `X-API-Key` 또는 OIDC Bearer 를 가져야
   한다. 인증 수단이 하나도 없으면 컨테이너가 기동하지 않는다(ADR 0006). OIDC 배포에서는 그 위에
   allowlist 와 가입 원장이 있다.
2. **CORS allowlist.** `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 에 적은 Studio 오리진만 허용하고 기본은 전부
   거부한다. CORS 는 다른 사이트의 페이지가 사용자의 브라우저로 Builder 를 부르는 것을 막는 장치이고,
   브라우저가 아닌 호출자를 막지 않는다. 그쪽을 막는 것은 1 이다.
3. **인증 실패 스로틀과 프록시 신뢰 체인**(#1031, #1235). 반복되는 인증 실패를 클라이언트 주소별로
   제한하고, 그 주소는 신뢰하는 프록시가 정한 값만 쓴다.
4. **Builder 컨테이너의 포트는 호스트의 루프백에만 묶는다**(`BUILDER_BIND` 기본값). 밖으로 여는 것은
   프록시(Caddy)의 80/443 뿐이다.

**ADR 0009 결정 3 은 이 ADR 로 대체한다.** ADR 0009·0015 의 나머지(검증 계약, Bearer 전송, allowlist
fail-closed)는 그대로다.

## 그대로 두는 것

- **브라우저가 부르지 않는 배포는 Builder 를 내부 서비스로 둘 수 있고, 그럴 수 있으면 그렇게 둔다.**
  스케줄 워크플로만 Builder 를 부르는 배포가 그렇다. 공격면이 작은 쪽이 기본 선택이라는 ADR 0009 의
  판단은 여전히 맞고, 이 ADR 은 Studio 가 붙는 배포의 기본형을 정한 것이다.
- 대안 B 는 금지가 아니다. 같은 오리진 프록시를 운영하는 배포는 CORS 를 비워 둘 수 있다.

## 영향

- `docs/deploy.md` §1 의 "ADR 과의 차이" 문단을 이 결정으로 바꾼다.
- `infra/cubrid/README.md` 의 "internal ingress (공개 노출 없음)"을 고친다. 그 compose 는 Builder 포트를
  루프백에 묶고 프록시를 앞에 두는 구성이라는 점은 같다.
- 남은 일은 배포가 있어야 할 수 있다: origin 을 Cloudflare 로 제한하는 것과 실제 경로의 확인(#1236).

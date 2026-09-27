# ADR 0011 — BuildSpec 어시스턴트 그라운딩 계약

- 상태: 승인됨(Accepted) — studio 에 구현 완료

> `kpubdata-studio` 의 `src/features/assistant/` 에 구현되어 있다 —
> `scrub.ts`(전송 전 시크릿 마스킹), `columnMeaning.ts`, `AssistantChat.tsx`.
> evidence 를 데이터로 다루고 그 안의 지시를 따르지 않는다는 이 ADR 의 핵심은
> 프롬프트 구성에 반영되어 있다.
- 관련 이슈: #415, ADR 0009(인증), ADR 0005(API 계약 단일 소소스)
- 관련 문서: [BUILD_SPEC.md](../BUILD_SPEC.md), [BOUNDARY.md](../BOUNDARY.md), [API_CONTRACT.md](../API_CONTRACT.md)

## 결정 (제안)

Studio에 BuildSpec 작성 어시스턴트(챗봇)를 도입하기 위해 Builder가 담당할 범위를 다음과 같이 정한다.

1. **Builder는 LLM을 호출하지 않는다.** 어시스턴트 엔드포인트(`/assist`, `/chat` 등)를 추가하지 않으며, LLM SDK를 의존성에 넣지 않는다.
2. **Builder는 그라운딩 자산을 제공한다.** (a) provider/dataset 카탈로그(`GET /catalog`)와 (b) 기계가 읽을 수 있는 검증 결과(`/validate` problems 구조화)를 노출한다.
3. **검증은 Builder가 최종 판정한다.** 어시스턴트가 생성한 스펙은 반드시 `POST /validate`를 통과해야 실행 후보가 된다.
4. **LLM 호출 위치는 Studio BYOK.** 사용자가 자기 API 키로 브라우저에서 LLM API를 직접 호출한다. v1에서는 이 방식만 지원한다.

## 배경

Studio는 서버 런타임이 없는 정적 SPA다. LLM 호출 주체를 어디에 둘지가 핵심 설계 질문이다.

## 문제

Builder에 LLM 프록시를 두면:
- **워커 고갈**: `ThreadingHTTPServer` 워커 10개, SSE 스트리밍이 워커 점유 → 동시 대화 10건이면 `/build`·`/validate` 마비.
- **역할 오염**: BOUNDARY.md는 Builder를 결정적 빌드 엔진으로 규정. 비결정적 컴포넌트·외부 API 비용이 들어오면 성질이 깨짐.
- **남용 표면**: 인증된 LLM 프록시도 principal별 레이트 리밋·비용 상한 필요. Builder는 이를 위한 상태 저장소가 없음.

반면 Builder만이 제공할 수 있는 것이 있다:
- 어떤 provider와 dataset이 실제로 존재하는가 (**현재 조회 수단 없음**)
- 어떤 스펙이 유효한가 (`/validate`가 이미 보유)

## 검토한 대안

### A. Builder에 `/assist` 프록시 추가
Studio 변경이 적고 키가 서버에 머문다. 그러나 워커 고갈·역할 오염·남용 문제를 그대로 받는다. **기각.**

### B. 별도 `kpubdata-assistant` 서비스
경계가 깨끗하고 독립 스케일. 그러나 배포 대상 증가, OSS 사용자 진입 장벽. **v1 기각, 조직 배포 시 전환 경로로 남김.**

### C. Studio BYOK — **v1 채택**
서버 추가 없음. 사용자가 자기 키를 쓰므로 남용·비용 문제가 구조적으로 소멸. GitHub Pages 데모에서도 동작.

## Builder가 추가할 것

### 1. `GET /catalog` (#416)
provider/dataset 카탈로그. 출처는 `kpubdata` 패키지의 provider 레지스트리. 자격증명 파라미터는 필요 여부만 표기, 값 노출 금지. 인증 뒤에 배치.

### 2. `/validate` problems 구조화 (#417)
```json
{"code": "missing_required_field", "path": "sources[0].params.base_date", "message": "base_date는 필수입니다.", "hint": "YYYYMMDD 형식"}
```
`message`를 항상 포함해 기존 소비자 호환 유지. 계약 버전으로 협상.

### 3. API 계약 `1.2.0` (#418)
`/catalog` 추가 + problems 스키마 변경. `1.1.0`은 인증에 배정됨.

## 환각 차단 — 4중 게이트

```
LLM 출력 → ① zod 파싱 → ② 카탈로그 대조 → ③ Builder /validate → ④ 사용자 승인
```
- **③이 핵심**: Builder가 실제로 실행 가능한지 판정. LLM이 아니라 Builder가 결정.
- **④는 타협 불가**: LLM 스펙을 자동 실행하지 않음.
- 리페어 루프 상한: 각 게이트 최대 2회 재생성.

## 시크릿 스크러빙

`sourceParams`의 서비스 키 등이 LLM으로 전송되는 것을 막는다. 호출 경로 공통 계층에 배치 (프록시 모드 전환 시에도 우회 불가).

## 영향

- `contract/builder-api.yaml`: `GET /catalog` 추가, `problems` 스키마 변경
- `API_CONTRACT_VERSION` → `1.2.0`
- LLM 관련 의존성·환경변수·비용은 Builder에 **추가되지 않음**
- 인증 범위: `/catalog`는 로그인 필요, `/validate`는 기존과 동일

## 미해결 질문

- 카탈로그 응답 캐시 정책 (provider 레지스트리 조회 비용)
- dataset params 스키마 깊이 (kpubdata에서 어디까지 얻을 수 있는가)
- 조직 배포에서 대안 B 전환 시 M2M 자격증명 (ADR 0009 `X-API-Key` 재사용?)

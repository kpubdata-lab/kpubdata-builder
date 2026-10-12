# ADR 0002 — Build 실행 모델: 동기 vs 비동기(job)

- 상태: 승인됨(Accepted)
- 관련 이슈: #308, #241, #209, kpubdata-studio#102
- 관련 문서: [API_CONTRACT.md](../API_CONTRACT.md), [BUILD_STATE.md](../BUILD_STATE.md)

## 결정 (승인됨)

권고안을 **수정 채택**한다. 하이브리드의 방향성은 유지하되, 비동기 job은 v0.4의 **호출 가능한 계약에서 제외**한다.

1. v0.4에서는 **동기 `POST /build`만** 유지·구현한다(200/502 계약 그대로).
2. 비동기 `POST /builds`(202) / `GET /builds/{run_id}`는 `contract/builder-api.yaml`의 **호출 가능 계약에 넣지 않는다.** 이 ADR과 로드맵에만 서술하거나, 부득이 yaml에 둔다면 `x-planned: true`로 표기하여 **codegen·계약 테스트(ADR 0005)에서 반드시 제외**한다.
3. 비동기는 상태 머신·실패 처리·취소·멱등성·부분 산출물(partial manifest) 시맨틱을 **완비한 뒤** 별도 후속 ADR/이슈에서 구현한다. 반쪽짜리 비동기 계약을 노출하지 않는다.
4. 실행 시퀀싱상 이 ADR은 **가장 마지막**이다(0005 → 0006 → 0004 → 0003 → 0002). 계약 정직성(0005)과 완료 이력 저장소(0003)가 선행되어야 한다.

> 근거: 미완성 async 계약을 SSOT에 노출하면 계약 드리프트를 오히려 키운다. "마법 같은 동작보다 결정적 동작 우선"(AGENTS.md) 원칙에 따라, 완전한 시맨틱 없이 async 표면을 만들지 않는다.

## 배경

현재 `POST /build`(`service/app.py:BuilderService.build`)는 **동기 실행**이다. 요청 스레드 안에서 Bronze→Silver→Gold 파이프라인을 모두 수행하고, 완료 후에야 `run_id`·`outcomes`·`manifest` 경로를 담아 200(성공) 또는 502(upstream 실패)를 반환한다.

반면 문서와 소비자 기대는 비동기 모델을 전제한다.

- `API_CONTRACT.md` §2/§8은 "실제 build와 publish는 비동기식으로 모델링하는 것을 기본값으로 둔다", `createBuild`는 "계약은 비동기 `POST /builds` 지향"이라고 서술한다.
- Studio(`kpubdata-studio#102`)의 빌드 이력/상태 표시는 `run_id` 생성 후 상태를 폴링하는 흐름을 가정한다.

이 동기 구현 ↔ 비동기 문서/기대 사이의 간극이 계약 드리프트(#241)의 핵심 원인이다.

## 문제

장시간 빌드(대형 소스, 다수 export)에서 동기 모델은 다음 한계가 있다.

- HTTP 요청 타임아웃/프록시 버퍼링에 취약하다.
- 진행 상태(단계별 진척) 노출이 불가능하다.
- 취소/재시도 훅이 없다.
- 클라이언트가 연결을 유지해야 하므로 UI 응답성이 떨어진다.

## 결정 필요 사항

1. `/build`를 **동기 유지**할지, **비동기 job 모델**로 전환할지, 또는 **양쪽 병행**할지.
2. 비동기 채택 시 상태 머신 정의: `queued → running → succeeded | failed`(+ `cancelled`).
3. 계약 형태: `POST /builds` → `202 Accepted` + `{run_id}`, 상태 폴링 `GET /builds/{run_id}`.
4. 타임아웃/취소/동시성 상한 정책(#253의 `BoundedThreadingHTTPServer`와 정합).

## 검토한 대안

### 대안 A — 동기 유지(현행)
- 장점: 구현 단순, 결정적, 현재 테스트 그대로 유효.
- 단점: 장시간 빌드/진척 노출/취소 불가. 문서·Studio 기대와 불일치 지속.

### 대안 B — 비동기 job 모델 전환
- 장점: 장시간 빌드 대응, 진척/취소 가능, 문서·Studio와 정합.
- 단점: 상태 저장소(→ ADR 0003) 필요, 워커/큐 도입으로 복잡도·운영부담 증가.

### 대안 C — 하이브리드 (동기 기본 + 비동기 옵션)
- `POST /build`(동기)는 소규모/CLI용으로 유지하고, `POST /builds`(비동기)를 신설.
- 장점: 기존 계약 보존 + 확장. 단점: 두 경로 유지 비용.

## 권고 (제안)

**대안 C(하이브리드)를 v0.4 목표로 제안**한다. 단, 실제 job 실행 인프라(워커/큐)는 ADR 0003(영속 저장소) 확정에 의존하므로, v0.4에서는 다음 최소 단계를 우선한다.

1. 상태 머신을 `BUILD_STATE.md`에 명문화(queued/running/succeeded/failed/cancelled).
2. `POST /builds`(202+run_id) / `GET /builds/{run_id}`(상태) 계약을 `contract/builder-api.yaml`에 **선언만** 추가(구현 전, planned 표기).
3. 동기 `POST /build`는 하위호환으로 유지.

## 영향

- `API_CONTRACT.md`의 "비동기 지향" 서술이 실제 계약과 일치하게 됨(#241 잔여 해소).
- ADR 0003(영속 저장소)과 강하게 결합 — 상태를 어디에 보관할지 함께 결정해야 함.
- Studio #102는 우선 동기 `/build` + `GET /builds` 목록으로 연동하고, 비동기 폴링은 후속 단계로 분리 가능.

## 미해결 질문

- 워커 실행 모델: 인프로세스 스레드풀 vs 별도 프로세스/큐?
- 취소 시 부분 산출물(partial manifest) 처리 규약?
- run_id 생성 주체(클라이언트 지정 허용 범위, 현재 `validate_path_segment`로 검증됨).

> **2026-10-12 보충 (#1242).** 동기 `POST /build` 는 요청과 응답을 그대로 둔 채, 비동기 `POST /builds` 와
> 같은 job 으로 제출되고 요청이 그 job 이 끝나기를 기다린다. 요청 스레드에서 따로 돌던 동안 취소(#481),
> 실행 시간 상한(#1119), 사용자별 동시 build 상한(#1189), 종료 drain(#1118)이 동기 build 에 닿지 않았기
> 때문이다. 이 ADR 이 정한 "동기 `POST /build` 는 하위호환으로 유지" 는 바뀌지 않는다.

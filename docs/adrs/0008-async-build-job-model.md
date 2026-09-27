# ADR 0008 — 비동기 build job 모델: 상태·실패·취소·멱등성·부분 산출물

- 상태: 승인됨(Accepted) — 구현 완료

> 상태가 "제안됨" 으로 남아 있었지만 `service/jobs.py` 의 `AsyncBuildExecutor`·
> `JobRegistry` 로 구현되어 있고 `POST /builds` 가 이 모델로 동작한다. 제안된
> 것이 아니라 동작하는 것이다.
- 관련 이슈: #334, #308(ADR 0002), #309(ADR 0003)
- 관련 문서: [ADR 0002](./0002-build-execution-model.md), [ADR 0003](./0003-persistent-build-store.md), [BUILD_STATE.md](../BUILD_STATE.md), [API_CONTRACT.md](../API_CONTRACT.md)

> 본 ADR은 ADR 0002에서 "시맨틱을 완비한 뒤 별도 후속에서 구현"으로 연기한 **비동기 job 실행 모델**의 설계를 다룬다. v0.4 범위 밖이며, 승인 후 구현 이슈로 분해된다.

> **현재 구현 주의**: 제안 이후 process-local `AsyncBuildExecutor`(worker 10, queued job
> 10)와 active registry가 일부 구현되었다. 이는 본 ADR을 승인됨으로 전환하지 않는다.
> 취소, queue 영속성, crash recovery, partial manifest 규약은 여전히 미해결이며 운영 시
> [배포 가이드](../deploy.md#8-동시성풀백프레셔)의 제한을 따른다.

## 결정 필요 사항 (이슈 #334)

1. 상태 머신 확정(`queued → running → succeeded | failed`, `+ cancelled`).
2. 실패 처리·재시도 정책, 취소 시 부분 산출물(partial manifest) 규약.
3. 멱등성(`run_id` 생성 주체, 중복 제출 시맨틱), 동시성 상한 정합.
4. 워커 실행 모델(인프로세스 스레드풀 vs 별도 프로세스/큐).
5. job 상태 저장(ADR 0003의 완료 이력 인덱스와 **별개** 설계).

## 배경

ADR 0002는 v0.4에서 **동기 `POST /build`만** 유지하기로 결정했다. 비동기 `POST /builds`(202) / `GET /builds/{run_id}` 계약은 시맨틱이 불완전한 상태로 노출하면 계약 드리프트(ADR 0005)를 키우므로, 상태 머신·실패·취소·멱등성·부분 산출물을 완전히 정의한 뒤에만 도입한다.

현재 실행 인프라:
- `service/http.py`의 `BoundedThreadingHTTPServer`(#253)가 동시성 상한을 갖는다.
- HTTP pool과 별도로 `service/jobs.py`의 in-process async build pool이 실행되며, bounded
  queued-job 수를 넘는 제출은 `429`로 거절한다.
- `/query`는 별도 semaphore로 동시 spawned child process 수를 제한한다. 이 permit은 build
  worker나 HTTP worker를 예약하지 않는다.
- `store/build_index.py`(ADR 0003)는 **완료된 빌드의 파생 인덱스**다. 빌드 진행 중에는 `manifest.json`이 없으므로 이 인덱스로 `queued`/`running`을 표현하지 않는다(ADR 0003 §2).
- `run_id`는 현재 클라이언트 지정 가능(`validate_path_segment`로 경로 안전성 검증).

## 검토한 대안

### 1. 워커 실행 모델

- **A. 인프로세스 스레드풀(`concurrent.futures.ThreadPoolExecutor`)** — 빌드는 kpubdata HTTP 호출·디스크 I/O 중심(I/O-bound)이므로 GIL 영향이 적다. `BoundedThreadingHTTPServer`의 동시성 상한과 자연스럽게 정합한다.
- **B. 별도 프로세스/큐** — 장기 실행·크래시 격리에 유리하나 외부 의존(Redis/RQ/Celery 등) 또는 멀티프로세스 오케스트레이션이 필요하다. AGENTS.md '무외부의존 지향'·'결정적 동작 우선'과 충돌.

### 2. job 상태 저장

- **A. 인메모리 잡 레지스트리(active only) + 완료 시 ADR 0003 인덱스로 승격** — 활성 잡(queued/running)은 프로세스 메모리에만 존재. 종료(성공/실패) 시점에 `manifest.json` 작성 + ADR 0003 인덱스에 기록. 크래시 시 활성 잡은 소실(재시작 시 'unknown' → failed로 조정).
- **B. 별도 job-state 테이블(SQLite)** — 활성 상태까지 영속. 복구 가능성은 높아지나 ADR 0003 인덱스와 관심사가 중복되고 동기화 규약이 다시 필요해진다.

### 3. 멱등성

- **A. 클라이언트 지정 `run_id`를 멱등 키로 사용** — 동일 `run_id`로 중복 `POST /builds` 시 활성 잡이 존재하면 그 잡을 반환(200 또는 409). 서버는 미지정 시 ULID로 생성.
- **B. 서버 단독 발급** — 중복 제출은 항상 새 잡. 멱등성 없음(클라이언트 재시도 폭증).

### 4. 취소·부분 산출물

- **A. 협력적 취소 + 부분 manifest 보존** — 단계 경계(Bronze/Silver/Gold)에서 취소 플래그 점검. 취소 시 이미 생성된 산출물은 `partial: true` manifest와 함께 보존(디버깅·감사). 정리(cleanup) 정책은 별도 설정.
- **B. 즉시 강제 종료 + 산출물 폐기** — 빠르지만 디버깅 단서 손실.

### 5. 실패·재시도

- **A. 실패 = 종단 상태, 재시도는 새 `run_id` 재제출** — 자동 in-place 재시도 금지(결정성 보존). 에러는 manifest/인덱스에 기록.
- **B. 서버 자동 재시도(횟수 제한)** — 일시적 업스트림 장애 회복에 유리하나 비결정적 동작·부분 산출물 축적으로 복잡.

## 권고 (제안)

본 ADR은 **제안됨** 상태로, 아래 방향을 후속 논의의 기준선으로 제시한다. 승인 전에 미해결 질문이 확정되어야 한다.

1. **워커**: **대안 A(인프로세스 `ThreadPoolExecutor`)** — `BoundedThreadingHTTPServer` 동시성 상한에 정렬. 외부 큐/프로세스는 신뢰경계·운영부담이 확정된 후 별도 ADR로 검토.
2. **상태 저장**: **대안 A(인메모리 active 레지스트리 + 완료 시 ADR 0003 승격)** — ADR 0003 인덱스를 '완료 이력 전용'으로 유지하는 결정을 훼손하지 않는다.
3. **상태 머신**: `queued → running → (cancelling) → succeeded | failed | cancelled`. `cancelling`은 협력적 취소의 과도기 상태(단계 경계에서 전이).
4. **멱등성**: **대안 A(클라이언트 `run_id` = 멱등 키)** — 중복 제출은 기존 활성 잡 반환. 서버 ULID 폴백 유지.
5. **취소**: **대안 A(협력적 + partial manifest 보존)** — 단계 경계 취소 점검, `partial: true` manifest로 부분 산출물 명시.
6. **재시도**: **대안 A(실패 종단 + 새 제출)** — 자동 재시도 없음.
7. **동시성 상한**: 잡 큐 + bounded workers. 초과 시 백프레셔(큐잉) 또는 `429/503`(설정 가능). #253 `BoundedThreadingHTTPServer` 정책과 단일 소스로 정합.

## 영향

- `POST /builds`(202 + `{run_id}`) / `GET /builds/{run_id}` / `POST /builds/{run_id}/cancel` 계약이 `contract/builder-api.yaml`에 추가(ADR 0005 SSOT). codegen·커버리지 테스트 대상에 포함.
- `BUILD_STATE.md`에 상태 머신·전이 규칙 명문화.
- ADR 0003 인덱스는 종단 상태 도달 시에만 기록(활성 상태 비기록 원칙 유지).
- Studio(kpubdata-studio#102)의 빌드 이력/상태 UI가 폴링 모델로 연동 가능해진다.
- AGENTS.md '매니페스트 누락 금지' — partial manifest도 manifest로 간주하여 항상 기록.

## 미해결 질문

- **크래시 복구**: in-flight 활성 잡을 (a) failed로 확정할 것인가, (b) manifest/중간 체크포인트가 있으면 재개할 것인가? (본 권고는 (a) 기준)
- **partial 산출물 보존 기한/정책**: 디스크 한도·TTL·명시적 cleanup 커맨드 여부.
- **run_id 포맷 표준**: ULID(시간 정렬) vs UUID vs 클라이언트 지정 허용 범위(현재 `validate_path_segment` 검증).
- **잡 큐 지속성**: 프로세스 재시작 시 큐에 대기 중이던 잡의 운명(재등록 vs 폐기).
- **진척(partial progress) 노출**: 단계별 진행률을 `GET /builds/{run_id}` 응답에 포함할지, 별도 스트림(SSE/WS)을 둘지.

# Architecture Decision Records (ADR)

KPubData Builder의 주요 설계 결정을 기록합니다. 각 ADR은 배경·문제·검토한 대안·권고·영향을 담으며, 상태(제안됨/승인됨/대체됨)를 표기합니다.

| ADR | 제목 | 상태 | 관련 이슈 |
| :--- | :--- | :--- | :--- |
| [0001](./0001-builder-as-orchestrator.md) | 오케스트레이터로서의 Builder | 승인됨 | — |
| [0002](./0002-build-execution-model.md) | Build 실행 모델: 동기 vs 비동기(job) | 승인됨 | #308 |
| [0003](./0003-persistent-build-store.md) | 영속 Build 저장소: 파일시스템 스캔 대체 | 승인됨 | #309 |
| [0004](./0004-plugin-exporter-contract.md) | Plugin Exporter API 계약 안정화 | 승인됨 | #310 |
| [0005](./0005-api-contract-single-source.md) | API 계약 단일 소스 & 코드 생성 전략 | 승인됨 | #311 |
| [0006](./0006-service-auth-and-deployment.md) | 서비스 인증 & 배포(Docker) 스토리 | 승인됨 | #312 |
| [0007](./0007-kpubdata-version-compatibility-policy.md) | kpubdata 버전 호환성 정책 및 핀 강화 | 승인됨 | #213 |
| [0008](./0008-async-build-job-model.md) | 비동기 build job 모델: 상태·실패·취소·멱등성·부분 산출물 | 제안됨 | #334 |
| [0009](./0009-user-authentication-google-oidc.md) | 사용자 인증 모델: Google OIDC ID 토큰 검증 | 대체됨(0015) | #383 |
| [0010](./0010-artifactstore-state-backend.md) | ArtifactStore 추상화 + BuildIndex 백엔드 분리 | 제안됨 | #375 |
| [0011](./0011-buildspec-assistant-grounding.md) | BuildSpec 어시스턴트 그라운딩 계약 | 제안됨 | #415 |
| [0012](./0012-provider-credential-boundary.md) | Provider credential 저장·주입 경계 | 승인됨 (2026-09-30 개정: 다중 사용자 규칙) | #492, #505, #682 |
| [0013](./0013-api-contract-release-policy.md) | API 계약 버전과 릴리스 경계 정책 | 승인됨 | #521 |
| [0014](./0014-source-ingestion-file-url-boundary.md) | Public API·File·URL Source 통합 경계 | 승인됨 | #498 |
| [0015](./0015-email-password-oidc-idp-keycloak.md) | 사용자 인증 IdP: 이메일/비밀번호-capable OIDC(Keycloak) 전환 | 승인됨 | #515 |
| [0016](./0016-cubrid-state-backend.md) | CUBRID 상태 백엔드 전환 + OCI Compute VM 배포 | 승인됨 | #579 |
| [0017](./0017-fullstack-oci-deployment.md) | 풀스택 배포 토폴로지: OCI 단일 VM(Builder) + Cloudflare Pages(Studio) | 제안됨 | — |
| [0018](./0018-legacy-publish-pipeline.md) | 레거시 publish 파이프라인과 BuildSpec 경로의 공존 | 승인됨(2026-09-30, 선택지 C) | #636, #659 |
| [0019](./0019-column-metadata-semantics.md) | 컬럼 메타: 저장 타입·의미·표시·단위·출처의 분리 | 제안됨 | #813 |
| [0020](./0020-credential-lifetime-by-deployment.md) | 배포 형태에 따른 자격 증명의 수명 | 제안됨(D1 문장화, 확인 필요) | #682, #683 |
| [0021](./0021-duckdb-tabular-engine.md) | DuckDB 를 단일 tabular 엔진으로 | 제안됨(소유자 ADR 검토) | #864, #622, #701, #704 |

## 작성 규칙

- 파일명: `NNNN-kebab-title.md` (4자리 일련번호).
- 언어: 한국어(문서 정책).
- 상태 흐름: `제안됨(Proposed) → 승인됨(Accepted) → (필요시) 대체됨(Superseded)`.
- 이 목록은 v0.4 마일스톤(Beyond-MVP 통합, epic #313)의 설계 결정을 추적합니다.

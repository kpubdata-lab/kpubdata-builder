# Builder-Studio Boundary Rules — KPubData Builder

## 1. 목적

이 문서는 `kpubdata-builder`와 `kpubdata-studio` 사이의 책임 경계를 고정합니다. 목적은 중복 구현과 계약 드리프트를 막는 것입니다.

## 2. 핵심 원칙

1. **BuildSpec source of truth는 Builder입니다.**
2. **Preview logic는 Builder가 계산하고 Studio는 표시만 합니다.**
3. **Manifest schema는 Builder가 소유합니다.**
4. **Publish workflow는 Builder가 실행하고 Studio는 요청합니다.**
5. **Studio는 BuildSpec 계약이나 파이프라인 로직을 재정의할 수 없습니다.**
6. **Builder는 Medallion stage(Bronze/Silver/Gold), staging layout, stage promotion rules, canonical tabular engine(지금 Polars, DuckDB 로 전환 중 — [ADR 0021](https://github.com/yeongseon/kpubdata-builder/blob/main/docs/adrs/0021-duckdb-tabular-engine.md))을 소유합니다. Studio는 stage-specific preview 요청과 stage artifact 표시를 할 수 있지만, stage transform을 구현하거나 promotion rule을 재정의해서는 안 됩니다.**
7. **Builder는 Silver/Gold artifact에 대한 read-only SQL sandbox를 소유합니다. Studio는 명시적인 query 요청과 결과 표시를 담당합니다.**
8. **Builder는 Provider credential의 해석·연결 테스트와 수명 규칙을 소유합니다.** 단일 사용자 배포에서는 stable principal별로 암호화 저장하고, 다중 사용자 배포(`multi_user_mode()`)에서는 요청·작업이 도는 동안만 두고 영속 저장하지 않습니다([ADR 0012 개정](https://github.com/yeongseon/kpubdata-builder/blob/main/docs/adrs/0012-provider-credential-boundary.md), [ADR 0020](https://github.com/yeongseon/kpubdata-builder/blob/main/docs/adrs/0020-credential-lifetime-by-deployment.md)). **Studio는 raw credential을 저장하지 않고 Builder API에 입력한 뒤 metadata만 표시합니다.**
9. **Builder는 wire 어휘를 소유합니다.** 상태 축(`AccessStatus` 등)과 카탈로그 어휘는 Builder 가 정의하고 kpubdata 값을 명시적으로 매핑합니다(`service/vocabulary.py`, #831). **Studio는 Builder HTTP/OpenAPI 계약만 소비하며 kpubdata 의 구현 세부(모듈·상수·저장소 경로)를 참조하지 않습니다** (kpubdata [ADR 0007](https://github.com/yeongseon/kpubdata/blob/main/docs/adrs/0007-independence-rules.md) Rule 8·9).

## 3. 책임 분리표

| 영역 | Builder 책임 | Studio 책임 |
| :--- | :--- | :--- |
| BuildSpec | 계약 정의, 버전 관리, 검증 | 작성 UI, 폼 입력, 저장/불러오기 보조 |
| Preview | source 샘플 실행, schema 계산, stage-aware export preview 생성 | preview 결과 렌더링 |
| Build execution | 상태 전이, Bronze/Silver/Gold 승격, artifact 생성, manifest 기록 | 실행 요청, 상태 표시 |
| Manifest | 스키마 정의, 직렬화, 보관 정책 | manifest 표시, 링크 제공 |
| Publish | 원격 게시 수행, 성공/실패 기록 | publish 요청, 결과 표시 |
| Query | Silver/Gold table resolve, read-only 실행, limit/timeout | SQL 명시 실행 요청, result preview 표시 |
| Provider credential | 단일 사용자: owner_id별 encrypted-at-rest 저장·server default fallback. 다중 사용자: 요청·작업 수명만, 운영자 키 폴백 없음(ADR 0020). 요청별 client 격리, status/test | credential 입력 UX, masked/configured/status 표시 |
| Monitoring(#516) | latency/queue/worker/artifact 상태 측정, BuildIndex 기반 build 통계 집계, availability 판정 | Monitoring 대시보드 렌더링, 폴링 |

## 4. Studio가 해서는 안 되는 일

Studio는 다음을 구현하거나 소유하면 안 됩니다.

- 자체 BuildSpec 문법 정의
- Builder와 다른 별도 검증 규칙
- 독자적인 preview 계산 엔진
- manifest 스키마의 임의 확장/변형
- exporter/publisher 파이프라인 로직 재구현
- Bronze/Silver/Gold stage transform 구현
- stage promotion rule 재정의
- Builder 의 canonical tabular engine 과 별개의 변환 엔진 도입
- raw Provider credential을 browser storage나 Studio backend의 정본으로 보관
- Builder의 owner_id 또는 credential resolution 우선순위를 재구현
- Builder가 `unavailable`/`partial`로 보고한 Monitoring 값을 임의로 `healthy`/`0`으로
  대체해 표시

## 5. 허용되는 Studio 역할

Studio는 다음 역할을 수행할 수 있습니다.

- BuildSpec 편집기 제공
- Builder API 호출 래퍼 제공
- 실행 상태 시각화
- stage artifact/manifest 브라우징
- publish 요청 UX 제공

단, 계산 결과의 기준은 항상 Builder 응답이어야 합니다.

## 6. 변경 관리 규칙

- BuildSpec 변경은 Builder 문서와 구현에서 먼저 정의합니다.
- Manifest 필드 변경은 Builder 릴리스 노트와 계약 문서에서 먼저 공지합니다.
- Studio는 Builder 계약 버전을 따라가며, 선행 독자 확장을 금지합니다.

## 7. 한 문장 요약

> Builder는 파이프라인 엔진이고, Studio는 그 엔진을 조작하는 외부 UI 클라이언트입니다.

## 8. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 전체 레이어 구조 |
| [API_CONTRACT.md](./API_CONTRACT.md) | Builder 서비스 계약 |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | Builder 소유 입력 계약 |

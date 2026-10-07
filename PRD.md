# PRD — KPubData Builder

## 1. 제품 요약

KPubData Builder는 `kpubdata`의 정규화된 레코드를 받아
Markdown, Hugging Face 데이터셋, JSONL, Parquet, 메타데이터 산출물 같은 게시 가능한 결과물을 만드는 데이터셋 산출물 파이프라인이다.

이것은 저수준 API 래퍼가 아니다.
이것은 UI 제품이 아니다.
이것은 수집과 게시 사이의 오케스트레이션 및 빌드 계층이다.

## 2. 문제

사용자는 공공데이터를 가져올 수 있지만, 가져온 레코드를 재사용 가능하고 문서화되며 버전 관리 가능한
데이터셋 산출물로 바꾸는 과정은 반복적이고 일관되지 않다.

대표적인 문제점:
- 모든 데이터셋 프로젝트가 메타데이터와 파일 레이아웃을 다시 만든다.
- 내보내기 로직이 가져오기 로직과 강하게 결합되어 있다.
- Markdown, 데이터셋 카드, Hugging Face 업로드 로직이 뒤섞여 있다.
- 데이터셋 빌드를 재현하기 어렵다.
- 무엇이 생성되었는지 감사할 수 있는 안정적인 build manifest가 없다.

## 3. 목표

### 주요 목표
- build spec을 통해 데이터셋을 선언적으로 정의한다.
- 하나 이상의 소스를 결정적 산출물로 조립한다.
- 게시 준비가 된 결과물을 생성한다.
- provenance와 source metadata를 유지한다.
- 게시 전 로컬 미리보기를 지원한다.

### 비목표
- 수집 코어로서 `kpubdata`를 대체하지 않는다.
- 상호작용형 최종 사용자 UI로 동작하지 않는다.
- 모든 도메인 스키마 정합성 문제를 해결하지 않는다.
- 임의 ETL을 위한 워크플로 엔진이 되지 않는다.

## 4. 대상 사용자

### 4.1 개발자 / OSS 유지관리자
재현 가능한 설정으로 한국 공공데이터에서 데이터 산출물을 만들고 싶어 한다.

### 4.2 연구자 / 분석가
원시 공공데이터를 노트북 친화적이고 공유 가능한 데이터셋으로 바꾸고 싶어 한다.

### 4.3 데이터 큐레이터
Markdown 페이지, Hugging Face 데이터셋, 메타데이터 카드를 일관되게 게시하고 싶어 한다.

## 5. 제품 원칙

- 임시 스크립트보다 선언적 구성
- 암묵적 변경보다 결정적 빌드
- 출처 추적 우선
- exporter는 플러그형
- build manifest는 일급 산출물
- 사람이 읽을 수 있는 출력과 기계가 읽을 수 있는 출력은 공존해야 함

## 6. 사용자 스토리

- 유지관리자로서, 재현 가능한 빌드를 위해 YAML/TOML로 데이터셋을 정의하고 싶다.
- 유지관리자로서, 여러 소스를 하나의 산출물로 결합하고 싶다.
- 큐레이터로서, 게시 전에 레코드와 생성된 Markdown를 미리 보고 싶다.
- 큐레이터로서, 업로드 전에 검증 오류를 확인하고 싶다.
- 큐레이터로서, Markdown, JSONL, Parquet, Hugging Face 형식으로 내보내고 싶다.
- 큐레이터로서, 빌드 출력에 metadata와 provenance가 포함되길 원한다.

## 7. 기능 요구사항

### FR-1 빌드 명세
시스템은 다음을 포함하는 선언적 빌드 명세를 받아야 한다:
- 데이터셋 식별 정보
- source 정의
- 선택 및 매핑 규칙
- 정규화 토글
- export 대상
- metadata 필드
- 게시 옵션

### FR-2 소스 실행
시스템은 하나 이상의 데이터셋에서 레코드를 가져오기 위해 `kpubdata`를 호출해야 한다.

### FR-3 조립
시스템은 다음을 지원해야 한다:
- 단일 소스 데이터셋의 통과형 처리
- 호환 가능한 소스의 병합 / 합집합
- 파생 필드
- 필터링
- 정렬
- 컬럼 선택
- 데이터셋 수준 메타데이터 보강

### FR-4 검증
시스템은 다음을 검증해야 한다:
- 필수 build spec 필드
- exporter별 요구사항
- 누락된 자격 증명
- 빈 데이터셋 출력(configurable fail/warn)
- 잘못된 메타데이터

### FR-5 내보내기
시스템은 최소한 다음을 지원해야 한다:
- Markdown
- JSONL
- Parquet
- CSV
- Hugging Face 데이터셋 export 패키지 레이아웃
- Kaggle 데이터셋 export 레이아웃

exporter는 레지스트리 기반 플러그인이며, 서드파티 exporter를 entry point로 확장할 수 있어야 한다.

### FR-6 빌드 manifest
시스템은 다음을 포함하는 manifest를 생성해야 한다:
- build ID
- 시각 정보
- source 입력
- 행 수
- 스키마 요약
- 출력 산출물 위치
- 경고/오류

### FR-7 CLI
시스템은 로컬 및 CI 사용을 위한 CLI를 제공해야 한다. 최소한 다음 서브커맨드를 포함한다:
- `validate` — BuildSpec 검증
- `preview` — 게시 전 레코드/산출물 미리보기
- `build` — 빌드 실행 및 export
- `publish` — 생성된 산출물을 publisher 대상(local/HuggingFace/Kaggle)으로 전송
- `serve` — Builder HTTP 서비스 실행(Studio 등 제어 인터페이스가 호출)

## 8. 성공 지표

- 하나의 build spec으로 최소 3가지 artifact 형식을 생성할 수 있어야 한다.
- 동일한 spec + 동일한 source snapshot에 대해 build는 결정적이어야 한다.
- 모든 실행마다 manifest가 생성되어야 한다.
- MVP에는 최소 2개의 exporter가 포함되어야 한다.
- 최종 출력에서 source provenance가 보여야 한다.

## 9. MVP 범위

> **2026-10-07 보충** — 아래는 첫 MVP 를 정할 때의 범위이고, 지금의 제품은 그보다 넓다.
> "제외" 에 있는 것 가운데 **다중 사용자**는 들어와 있다: OIDC 로그인(ADR 0015), run 소유권
> (`ENFORCE_OWNERSHIP`), 가입 승인 원장(#785), 요청·작업 수명의 provider 키(ADR 0020).
> **브라우저 UI** 는 이 저장소가 아니라 KPubData Studio 가 맡는다. HTTP 서비스(`serve`),
> 비동기 build job(ADR 0008), 테이블 저장소와 SQL 도 MVP 뒤에 더해졌다. 지금의 실행 구조는
> [ARCHITECTURE.md](./ARCHITECTURE.md) 의 "현재 실행 구조" 절에 있다. 여러 사람이 한 데이터셋을
> 함께 고치는 **협업** 기능은 여전히 없다 — 사용자는 각자의 run 과 테이블만 본다.

### 포함
- YAML build spec
- 로컬 파일시스템 출력
- Markdown exporter
- Hugging Face 레이아웃 exporter
- JSONL/Parquet exporter
- manifest 생성
- preview 명령
- validation 명령

### 제외
- 다중 사용자 협업
- 브라우저 UI
- 원격 워크플로 스케줄러
- 서로 관련 없는 도메인 간 자동 스키마 정합화

---

## 관련 문서

### 이 저장소 내 문서
| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 설계 |
| [ROADMAP.md](./ROADMAP.md) | 프로젝트 로드맵 |

### KPubData Product Family
| 저장소 | 문서 | 설명 |
| :--- | :--- | :--- |
| [kpubdata](https://github.com/kpubdata-lab/kpubdata) | [PRD.md](https://github.com/kpubdata-lab/kpubdata/blob/main/PRD.md) | 코어 제품 요구사항 |
| [kpubdata-studio](https://github.com/kpubdata-lab/kpubdata-studio) | [PRD.md](https://github.com/kpubdata-lab/kpubdata-studio/blob/main/PRD.md) | 스튜디오 제품 요구사항 |

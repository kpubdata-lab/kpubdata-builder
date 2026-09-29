# 에러 처리 설계 — KPubData Builder

## 1. 개요

이 문서는 KPubData Builder의 에러/예외 처리 설계를 정의합니다.
Builder는 여러 단계(Spec 로딩 → 검증 → 실행 → 조립 → 내보내기 → Manifest)를 거치며,
각 단계에서 발생할 수 있는 실패를 명확하게 분류하고 처리하는 것이 핵심입니다.

## 2. 설계 원칙

1. **모든 런타임 실패는 `BuildError` 계층으로 수렴** — stdlib 예외(`ValueError` 등)가 사용자에게 직접 노출되지 않아야 함
2. **원인 보존** — `kpubdata`의 `PublicDataError`는 `__cause__`로 항상 보존
3. **단계별 에러 분리** — 어떤 단계에서 실패했는지 예외 타입만으로 구분 가능
4. **Manifest는 항상 생성** — 실패한 빌드도 manifest를 남겨 감사 추적 가능
5. **로그 1회 + 예외 전파** — 같은 에러를 매 레이어에서 중복 로깅하지 않음

## 3. 예외 계층

```
BuildError (base)
├── SpecLoadError          — YAML 파일 I/O 또는 파싱 실패
├── ValidationError        — 스펙 검증 실패 (여러 문제를 problems 리스트로 집계)
├── ExecutionError         — source 데이터 fetch 실패
├── AssemblyError          — 데이터 조립 실패 (누락 source 등)
├── ExportError            — 파일 생성/쓰기 실패
│   └── PathTraversalError — output_path가 output_dir를 벗어남
├── ManifestError          — manifest 직렬화 또는 디스크 기록 실패
├── PublishError           — 외부 저장소 배포 실패
├── TabularError           — 원시 레코드의 표 변환/정규화에서 데이터 무결성 문제
└── DatasetValidationError — 조립된 데이터셋이 검증을 통과하지 못함 (problems 리스트)
```

### 3.1 예외 컨텍스트

```python
class BuildError(Exception):
    """Builder 모든 에러의 기반 클래스."""

class ValidationError(BuildError):
    """빌드 스펙 검증 실패. 여러 문제를 한 번에 집계."""
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Validation failed: {'; '.join(problems)}")

class ExecutionError(BuildError):
    """소스 실행 또는 실행 준비 과정이 실패했음을 나타낸다."""

class ManifestError(BuildError):
    """매니페스트 직렬화 또는 디스크 기록이 실패했음을 나타낸다."""

class PathTraversalError(ExportError):
    """output_path가 허용된 output_dir를 벗어남을 나타낸다.
    ExportError를 상속하므로 기존 except ExportError 경로에서도 처리된다."""

class TabularError(BuildError):
    """원시 레코드의 표 변환/정규화에서 데이터 무결성 문제가 발생했음을 나타낸다."""

class DatasetValidationError(BuildError):
    """조립된 데이터셋이 검증을 통과하지 못했음을 나타낸다."""
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Dataset validation failed: {'; '.join(problems)}")
```

## 4. 단계별 에러 처리

### 4.1 Spec 로딩 (`spec/loader.py`)

| 실패 원인 | 변환 |
|:---|:---|
| 파일 없음 / 읽기 권한 없음 (`OSError`) | `SpecLoadError` |
| YAML 문법 오류 (`yaml.YAMLError`) | `SpecLoadError` |
| YAML은 유효하나 필수 키 누락 또는 타입 불일치 | `SpecLoadError` |

### 4.2 Spec 검증 (`spec/validator.py`)

| 실패 원인 | 변환 |
|:---|:---|
| `dataset_id` 비어 있음 | `ValidationError` (problems에 추가): `"dataset_id must be a non-empty string"` |
| sources 없음 | `ValidationError` (problems에 추가): `"at least one source is required"` |
| exports 없음 | `ValidationError` (problems에 추가): `"at least one export target is required"` |
| source.provider 또는 source.dataset 비어 있음 | `ValidationError` (problems에 추가): `"sources[i].provider must be a non-empty string"` 등 |
| source.alias가 공백 문자열로만 구성됨 | `ValidationError` (problems에 추가): `"sources[i].alias must not be blank when provided"` |
| export.kind가 레지스트리에 없는 값 | `ValidationError` (problems에 추가): `"exports[i].kind ... is not supported; supported kinds: [...]"` |

**핵심**: 첫 번째 오류에서 즉시 중단하지 않고, 모든 문제를 모아 한 번에 보고

### 4.3 Source 실행 (`stages/bronze/`)

> **Medallion 파이프라인**: `pipeline/orchestrator._run_source_pipeline()`은 단계 예외를
> `ExecutionError`로 감싸지 않습니다. `except Exception as exc:` 블록에서 `str(exc)`를
> `SourceBuildOutcome.error`에 그대로 기록합니다.

아래 변환 표는 `executor.py`(레거시) 흐름을 참고용으로 나타낸 것이며,
Medallion 파이프라인에서는 단계 예외가 모두 `str(exc)`로 직렬화되어 `outcomes[].error`에 저장됩니다.

```
kpubdata.PublicDataError
  ├── TransportTimeoutError       → str(exc)가 outcomes[].error에 기록
  ├── AuthError                   → str(exc)가 outcomes[].error에 기록
  ├── RateLimitError              → str(exc)가 outcomes[].error에 기록
  ├── ServiceUnavailableError     → str(exc)가 outcomes[].error에 기록
  ├── ParseError                  → str(exc)가 outcomes[].error에 기록
  ├── ProviderResponseError       → str(exc)가 outcomes[].error에 기록
  ├── DatasetNotFoundError        → str(exc)가 outcomes[].error에 기록
  ├── ConfigError                 → str(exc)가 outcomes[].error에 기록 (API 키 미설정 등)
  ├── InvalidRequestError         → str(exc)가 outcomes[].error에 기록 (잘못된 쿼리 파라미터)
  ├── UnsupportedCapabilityError  → str(exc)가 outcomes[].error에 기록 (미지원 operation)
  └── ProviderNotRegisteredError  → str(exc)가 outcomes[].error에 기록 (미등록 provider)
```

**정책**:
- 기본: source 하나라도 실패하면 build 실패
- 오케스트레이터는 여러 source 결과를 계속 수집 (에러 집계용)
- 향후: `allow_partial=True` 옵션으로 부분 빌드 허용

### 4.4 데이터 조립 (`stages/`)

| 실패 원인 | 변환 |
|:---|:---|
| spec의 source key 누락 | `AssemblyError` |
| 빈 레코드 (0건) | 허용 (warning) |

**핵심**: silent skip 제거. 누락 key는 명시적 실패.

### 4.5 내보내기 (`exporters/`)

| 실패 원인 | 변환 |
|:---|:---|
| 디스크 공간 부족 (`OSError`) | `ExportError` |
| 파일 쓰기 권한 없음 (`OSError`) | `ExportError` |
| 지원하지 않는 export kind | `ExportError` |
| output_path가 output_dir 외부 (`..` 포함 등) | `PathTraversalError` (`ExportError` 하위) |

**부분 산출물 정리 정책**:
- exporter가 여러 파일을 생성하는 도중 실패하면, 이미 생성된 파일을 정리(cleanup)합니다
- 방법: staging 디렉토리(`output_dir/.staging/`)에 먼저 쓰고, 전체 성공 시 최종 위치로 이동
- 실패 시 staging 디렉토리를 삭제하여 부분 산출물이 남지 않도록 보장
- 이 정책은 "partial artifact 없음" 원칙과 일관됨

### 4.6 Manifest 쓰기 (`manifest/writer.py`)

| 실패 원인 | 변환 |
|:---|:---|
| 디스크 I/O 실패 (`OSError`) | `ManifestError` |

**원자적 쓰기**: temp file → `os.replace()`

### 4.7 배포 (`publishers/`)

| 실패 원인 | 변환 |
|:---|:---|
| 네트워크 실패 | `PublishError` |
| 인증 실패 | `PublishError` |
| 원격 저장소 오류 | `PublishError` |

## 5. Manifest와 에러의 관계

### 5.1 BuildManifest 실제 필드

```python
@dataclass(frozen=True)
class BuildManifest:
    build_id: str
    started_at: datetime
    finished_at: datetime
    schema_version: str = MANIFEST_SCHEMA_VERSION  # "1.0.0"
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)
    schema_summaries: dict[str, SchemaSummary] = field(default_factory=dict)
    provenance: tuple[SourceProvenance, ...] = ()
    build_environment: BuildEnvironment | None = None
    inputs_fingerprint: str | None = None
```

> **참고**: 빌드 상태(`"ok"` | `"failed"`)는 디스크에 저장되는 `BuildManifest`가 아닌,
> 파이프라인 실행 결과인 `BuildResult.status`에 담깁니다.
> 취소(cancelled) 상태 및 `spec_digest` 필드는 계획(planned)/미구현입니다.

### 5.2 에러 흐름

```
Orchestrator
  ├── executor → 실행 결과 (errors 수집)
  ├── if errors → BuildResult.status="failed", manifest 생성, export/publish 건너뜀
  ├── assembler → AssemblyError 시 status="failed"
  ├── exporter → ExportError 시 status="failed"
  └── manifest_writer → 항상 실행 (실패 빌드도 기록)
```

### 5.3 역할 분리

| 컴포넌트 | 역할 |
|:---|:---|
| `manifest/models.py` | BuildManifest 데이터 클래스 정의 |
| `manifest/writer.py` | 직렬화 + 원자적 파일 쓰기만 담당 |
| Orchestrator | 에러 수집 → BuildManifest 생성 → manifest_writer 호출 |

## 6. 에러 → 사용자 메시지 변환

에러 메시지 변환은 **CLI/Studio 같은 최외곽 boundary**에서 수행합니다.

- Builder 내부: 구조화된 예외 + 메타데이터만 유지
- CLI: `format_user_error(exc)` 같은 함수로 한국어 메시지 변환
- Manifest: machine-readable summary (UI 문장 아님)

## 7. kpubdata (core)와의 에러 경계

```
[kpubdata]                              [kpubdata-builder]
PublicDataError 계층                    BuildError 계층
  ├── TransportError              →       ExecutionError.__cause__
  ├── TransportTimeoutError       →       ExecutionError.__cause__
  ├── AuthError                   →       ExecutionError.__cause__
  ├── RateLimitError              →       ExecutionError.__cause__
  ├── ServiceUnavailableError     →       ExecutionError.__cause__
  ├── ParseError                  →       ExecutionError.__cause__
  ├── ProviderResponseError       →       ExecutionError.__cause__
  ├── DatasetNotFoundError        →       ExecutionError.__cause__
  ├── ConfigError                 →       ExecutionError.__cause__
  ├── InvalidRequestError         →       ExecutionError.__cause__
  ├── UnsupportedCapabilityError  →       ExecutionError.__cause__
  └── ProviderNotRegisteredError  →       ExecutionError.__cause__

Builder는 kpubdata 예외를 직접 surface하지 않음.
항상 BuildError 하위로 감싸서 전파.
cause.retryable, cause.provider 등 메타데이터는 ExecutionError.__cause__에서 접근 가능.
```

## 8. 놓치기 쉬운 에러 시나리오

| 시나리오 | 대응 |
|:---|:---|
| 부분 데이터 수신 / 깨진 payload | `ParseError` → `ExecutionError` |
| API 키 mid-build 만료 | `AuthError` → 같은 provider 후속 source 중단 |
| 디스크 공간 부족 | `OSError` → `ExportError` / `ManifestError` |
| Concurrent build manifest 충돌 | output_dir을 `build_id` 기준으로 분리 |
| YAML spec 파일 I/O 에러 | `OSError` → `SpecLoadError` |
| 미완성 adapter 호출 (seoul, airkorea) | `NotImplementedError` → `ExecutionError` |
| Concurrent build artifact 충돌 | output_dir을 `build_id` 기준으로 분리 (manifest뿐 아니라 artifact 경로도) |
| Exporter 실패 시 부분 파일 잔존 | staging 디렉토리 사용 → 실패 시 cleanup |
| `RateLimitError` quota 소진 (retryable=False) | `cause.retryable` 계승하여 재시도 여부 판단 |
| output_path에 `..` 또는 절대 경로 | `PathTraversalError` (ExportError 하위) |
| 혼합 타입 컬럼 / 손실 캐스팅 | `TabularError` |
| Silver 단계 데이터셋 검증 실패 | `DatasetValidationError` (problems 리스트) |

## 9. 빌드 취소 정책

> 계획(planned)/미구현: 취소 기능은 현재 구현되지 않았습니다.

빌드 취소는 예외 타입이 아닌 **상태**로 처리하는 것을 목표로 합니다.

- 취소 시 진행 중인 source fetch를 중단하고, 수집된 결과까지만 manifest에 기록
- 취소된 빌드의 manifest: `errors`에 취소 사유 기록
- 예외 계층에 `BuildCancelledError`는 추가하지 않음 — 취소는 정상 흐름의 일부

## 10. Manifest 생성 보장 범위

"Manifest는 항상 생성"은 **best-effort** 원칙입니다.

- 정상 빌드: manifest 생성 보장
- 빌드 실패: manifest 생성 **시도** — 실패 정보를 기록
- `ManifestError` 발생 시: manifest 자체를 쓸 수 없는 상황 (디스크 꽉 참 등)
  - 이 경우 manifest는 생성되지 않으며, `ManifestError`가 최종 에러로 전파됨
  - CLI/Studio는 이 예외를 잡아 "빌드 결과를 기록할 수 없습니다" 메시지를 표시

---

## 관련 문서

| 문서 | 설명 |
|:---|:---|
| [ARCHITECTURE.md](../ARCHITECTURE.md) | 시스템 아키텍처 설계 |
| [API_CONTRACT.md](../API_CONTRACT.md) | API 인터페이스 규약 |
| [DOMAIN_MODEL.md](../DOMAIN_MODEL.md) | 도메인 모델 정의 |

### KPubData Product Family
| 저장소 | 문서 | 설명 |
|:---|:---|:---|
| [kpubdata](https://github.com/yeongseon/kpubdata) | exceptions.py | Core 예외 계층 정의 |

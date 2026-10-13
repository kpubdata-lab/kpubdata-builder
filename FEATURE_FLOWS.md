# 기능 처리 흐름 (기능처리도) — KPubData Builder

이 문서는 KPubData Builder의 **기능처리도(機能處理圖)**입니다. 각 기능(CLI 하위 명령)이 요청을 받아 어떤 순서로 내부 컴포넌트를 호출하고, 성공/실패를 어떻게 결정하는지 **기능 단위 시퀀스 다이어그램**으로 정리합니다.

- **알고리즘 관점**(단계별 처리 순서·검증 게이트·상태 전이)은 [ALGORITHM.md](./ALGORITHM.md)를 참고하세요.
- **데이터 흐름 관점**(Bronze→Silver→Gold 승격)은 [ARCHITECTURE.md](./ARCHITECTURE.md)를 참고하세요.
- 기준 구현은 `kpubdata_builder.cli`와 `kpubdata_builder.pipeline`입니다. **코드와 문서가 어긋나면 코드가 정답입니다.**

## 0. 공통 처리 규약

모든 기능은 아래 공통 전처리를 거칩니다. 이 단계 중 하나라도 실패하면 즉시 `stderr` 출력 후 종료 코드 `1`로 **빠르게 실패(fail-fast)**합니다.

```mermaid
sequenceDiagram
    autonumber
    actor User as 사용자
    participant CLI as CLI (dispatch)
    participant Spec as load_spec / validate_spec
    User->>CLI: kpubdata-builder <command> <spec.yaml>
    CLI->>Spec: load_spec(Path(spec))
    alt 로드 실패 (SpecLoadError)
        Spec-->>CLI: SpecLoadError
        CLI-->>User: "error: failed to load spec" (stderr, exit 1)
    else 로드 성공
        Spec->>Spec: validate_spec(spec)
        alt 검증 실패 (ValidationError)
            Spec-->>CLI: ValidationError(problems)
            CLI-->>User: "error: spec validation failed" + 문제 목록 (stderr, exit 1)
        else 검증 통과
            Spec-->>CLI: 검증된 BuildSpec
            Note over CLI: 명령별 처리 진행 (아래 참고)
        end
    end
```

- `_create_client()`는 `kpubdata.Client.from_env()`로 소스 클라이언트를 만듭니다. **실제 네트워크 호출은 preview/build 실행 시점에만** 발생합니다(validate는 네트워크 없음).

## 1. validate — 명세 검증

BuildSpec YAML을 로드·검증만 수행합니다. 네트워크 호출도, 산출물 기록도 없습니다.

```mermaid
sequenceDiagram
    autonumber
    actor User as 사용자
    participant CLI as _run_validate
    participant Spec as load_spec / validate_spec
    User->>CLI: validate <spec.yaml>
    CLI->>Spec: load_spec + validate_spec
    alt 로드/검증 실패
        Spec-->>CLI: SpecLoadError / ValidationError
        CLI-->>User: 오류 메시지 (stderr, exit 1)
    else 통과
        Spec-->>CLI: 검증된 BuildSpec
        CLI-->>User: "spec is valid: {dataset_id}" (exit 0)
    end
```

## 2. preview — 스키마·샘플 미리보기

각 소스를 fetch하여 메모리상에서 Silver까지 구성하고, **스키마와 샘플 행만** 출력합니다. 어떤 산출물 파일도 기록하지 않습니다(`preview_build`, `pipeline/preview.py`).

```mermaid
sequenceDiagram
    autonumber
    actor User as 사용자
    participant CLI as _run_preview
    participant PB as preview_build
    participant Client as SourceClient (kpubdata)
    participant Silver as Bronze fetch + Silver(메모리)
    User->>CLI: preview <spec.yaml> [--limit N]
    Note over CLI: 공통 전처리 (load_spec + validate_spec)
    CLI->>PB: preview_build(spec, client, limit)
    alt limit < 1
        PB-->>CLI: ValueError
        CLI-->>User: "error: invalid preview input" (stderr, exit 1)
    else 유효한 limit
        loop 각 source
            PB->>Client: build_bronze_artifact (fetch)
            Client-->>PB: 원시 레코드
            PB->>Silver: build_silver_dataset(preview_limit=N)
            Silver-->>PB: schema + preview slice
        end
        PB-->>CLI: PreviewResult(previews)
        CLI-->>User: 소스별 스키마 + 샘플 행 출력
        alt 실패한 source 존재
            CLI-->>User: "error: preview failed for one or more sources" (stderr, exit 1)
        else 모두 성공
            CLI-->>User: exit 0
        end
    end
```

- 개별 소스 fetch 실패는 예외로 던지지 않고 `SourcePreview(status="failed")`로 수집됩니다. 하나라도 실패하면 CLI가 `stderr` + exit 1로 변환하여 CI/자동화가 성공으로 오판하지 않도록 합니다.

## 3. build — Medallion 파이프라인 실행

BuildSpec을 Bronze→Silver→Gold로 승격시키고 산출물과 manifest를 기록합니다(`run_build`, `pipeline/orchestrator.py`). 단계별 상세는 [ALGORITHM.md](./ALGORITHM.md) 참고.

```mermaid
sequenceDiagram
    autonumber
    actor User as 사용자
    participant CLI as _run_build
    participant RB as run_build (orchestrator)
    participant Client as SourceClient (kpubdata)
    participant FS as run workspace / manifest
    User->>CLI: build <spec.yaml> [--output-dir DIR] [--run-id ID]
    Note over CLI: 공통 전처리 (load_spec + validate_spec)
    CLI->>RB: run_build(spec, client, output_root, run_id)
    loop 각 source
        RB->>Client: Bronze fetch
        Client-->>RB: 원시 스냅샷
        RB->>RB: Silver 변환/검증 → Gold 패키징
        RB->>FS: 산출물 기록
    end
    RB->>FS: manifest.json 기록
    RB-->>CLI: BuildResult(outcomes, manifest_path, status)
    CLI-->>User: source별 status + manifest 경로 출력
    alt status != "ok"
        CLI-->>User: "error: build failed" + 실패 source 오류 (stderr, exit 1)
    else warehouse_failures 있음 (--warehouse 커밋 거부)
        CLI-->>User: "error: build did not commit its table" + source별 사유 코드 (stderr, exit 1)
    else 모두 성공
        CLI-->>User: exit 0
    end
```

## 4. publish — 산출물 게시

이미 만들어진 산출물 디렉터리를 지정한 target(local/huggingface/kaggle 등)으로 게시합니다. 빌드를 재실행하지 않고 `--artifacts-dir`의 파일을 전송합니다(`PUBLISHER_REGISTRY`).

```mermaid
sequenceDiagram
    autonumber
    actor User as 사용자
    participant CLI as _run_publish
    participant Reg as PUBLISHER_REGISTRY[target]
    participant Pub as Publisher
    User->>CLI: publish <spec.yaml> --target T --destination D --artifacts-dir A
    Note over CLI: 공통 전처리 (load_spec + validate_spec)
    CLI->>CLI: artifacts-dir 존재 확인
    alt 디렉터리 없음
        CLI-->>User: "error: no artifacts found" (stderr, exit 1)
    else 존재
        CLI->>Reg: publisher 조회
        alt publisher.expects_directory
            Note over CLI: 디렉터리 자체를 전달 (레이아웃 단위, 예: Kaggle)
        else 파일 단위
            Note over CLI: rglob으로 개별 파일 수집 (local/HF)
        end
        CLI->>Pub: publish(paths, destination[, public])
        alt PublishError / RuntimeError
            Pub-->>CLI: 예외
            CLI-->>User: "error: publish failed" (stderr, exit 1)
        else 성공
            Pub-->>CLI: PublishResult(reference, artifact_count)
            CLI-->>User: target 참조 + 산출물 개수 출력 (exit 0)
        end
    end
```

- `--public`은 kaggle target에만 적용되며, 다른 target에서는 무시됩니다.
- publisher별 입력 계약(디렉터리 vs 파일) 불일치는 `expects_directory` 플래그로 해소합니다.

## 5. serve — HTTP 서비스 (참고)

`BuilderService`를 HTTP 서버로 노출합니다. Studio 등 상위 계층은 CLI 대신 이 서비스의 `validate`/`preview`/`build` 메서드를 호출합니다.

```mermaid
sequenceDiagram
    autonumber
    actor User as 운영자
    participant CLI as _run_serve
    participant Svc as BuilderService
    participant HTTP as service.http.serve
    User->>CLI: serve [--host H] [--port P] [--output-dir DIR]
    CLI->>Svc: BuilderService(output_root, client_factory)
    CLI->>HTTP: serve(service, host, port)
    Note over HTTP: 요청마다 validate/preview/build → ServiceResponse
    HTTP-->>User: HTTP 응답 (Ctrl-C 종료 시 exit 0)
```

## 관련 문서

| 문서 | 관점 |
| :--- | :--- |
| [ALGORITHM.md](./ALGORITHM.md) | 빌드 알고리즘(처리 순서·검증 게이트·상태 전이) |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | Medallion 단계 설계·레이어 분리 |
| [API_CONTRACT.md](./API_CONTRACT.md) | Builder Service/API 계약 |
| [EXPORT_MODEL.md](./EXPORT_MODEL.md) | Exporter 처리 흐름 |
| [BUILD_STATE.md](./BUILD_STATE.md) | 빌드 실행 상태 머신 |

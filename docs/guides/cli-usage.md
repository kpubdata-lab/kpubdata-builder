# CLI 사용 가이드 — KPubData Builder

이 문서는 `kpubdata-builder` 명령줄 도구(CLI)의 실제 사용법을 다룹니다.
각 명령의 출력은 실제 실행 결과를 그대로 수록했습니다.
라이브 API 키가 필요한 `preview` 및 `build` 명령의 출력은 테스트 픽스처 기반 예시임을 명시합니다.

---

## 설치 확인

설치가 완료되면 `kpubdata-builder` 실행 파일이 PATH에 등록됩니다.
버전을 확인하려면 `--version` 옵션을 사용합니다.

```console
$ kpubdata-builder --version
kpubdata-builder 0.4.0
```

---

## 전체 도움말

```console
$ kpubdata-builder --help
usage: kpubdata-builder [-h] [--version] command ...

KPubData Builder command-line interface.

positional arguments:
  command
    validate         Validate a BuildSpec YAML file.
    preview          Preview a BuildSpec: schema and sample rows without
                     writing artifacts.
    build            Execute a BuildSpec through the Medallion pipeline.
    publish          Publish build artifacts to a local or remote destination.
    serve            Run the Builder HTTP service.
    fixtures         Export the replay fixtures bundled with this package
                     (#837).
    rebuild-index    Rebuild the build index from filesystem scans.
    discover         Discover API metadata from a data.go.kr URL.
    monitor          Check pending dataset applications for approval.
    pipeline         Run automated onboarding pipeline for a dataset.
    verify           Verify dataset specs against live APIs.
    prune-cancelled  List (and optionally delete) cancelled partial-run
                     artifacts past a TTL (#549).
    warehouse-gc     Reclaim snapshots and staging directories nothing needs
                     any more (#738).
    warehouse-backup
                     Back up the table catalog and its snapshot files together
                     (#705).
    warehouse-restore
                     Restore a warehouse backup into an empty directory, after
                     checking the catalog and the snapshot files against each
                     other (#705).
    warehouse-hold   Place, release or list holds that keep a snapshot past
                     garbage collection (#705, #797).

options:
  -h, --help  show this help message and exit
  --version   show program's version number and exit
```

이 목록은 `tests/unit/test_cli_docs_contract.py` 가 실제 파서와 대조한다 — 서브커맨드가
늘거나 줄면 이 문서가 낡았다는 사실이 테스트로 드러난다.

---

## 하위 명령 (Subcommands)

### validate — 빌드 명세 검증

`validate` 명령은 BuildSpec YAML 파일의 구조와 필드 유효성을 검사합니다.
실제 데이터 소스(data.go.kr 등)에는 접근하지 않으므로 API 키 없이 오프라인에서 실행할 수 있습니다.

```console
$ kpubdata-builder validate --help
usage: kpubdata-builder validate [-h] spec

positional arguments:
  spec        Path to the BuildSpec YAML file.

options:
  -h, --help  show this help message and exit
```

#### 검증 성공 예시

다음은 유효한 BuildSpec YAML 파일 `apt_trade_spec.yaml` 예시입니다.

```yaml
# apt_trade_spec.yaml
dataset_id: dataset.seoul_apt_trade
title: 서울 아파트 실거래가
description: |
  data.go.kr 국토교통부 아파트매매 실거래가 데이터를 수집해
  JSONL 형식으로 내보내는 BuildSpec 예제입니다.
sources:
  - provider: datago
    dataset: apt_trade
    alias: gangnam_202401
    params:
      LAWD_CD: "11680"
      DEAL_YMD: "202401"
exports:
  - kind: jsonl
    output_path: out/apt_trade.jsonl
```

```console
$ kpubdata-builder validate apt_trade_spec.yaml
spec is valid: dataset.seoul_apt_trade
```

성공 시 종료 코드는 `0`이며, 표준 출력에 `spec is valid: <dataset_id>`가 출력됩니다.

#### 검증 실패 예시 1 — 로드 오류

`sources` 목록이 비어 있는 YAML은 로드(파싱) 단계에서 오류가 발생합니다.

```yaml
# broken_empty_sources.yaml
dataset_id: dataset.broken
title: Broken Spec
description: sources 목록이 비어 있어 검증에 실패하는 예제입니다.
sources: []
exports:
  - kind: jsonl
    output_path: out/data.jsonl
```

```console
$ kpubdata-builder validate broken_empty_sources.yaml
error: failed to load spec: Failed to parse build spec: sources must not be empty
```

종료 코드는 `1`입니다. 오류 메시지는 표준 오류(stderr)로 출력됩니다.

#### 검증 실패 예시 2 — 스펙 유효성 오류

지원하지 않는 `export.kind` 값은 파싱은 성공하지만 유효성 검사(validation) 단계에서 실패합니다.

```yaml
# broken_export_kind.yaml
dataset_id: dataset.broken_export
title: 잘못된 Export Kind 예제
description: 지원하지 않는 export kind가 포함된 스펙으로 검증 실패를 보여줍니다.
sources:
  - provider: datago
    dataset: apt_trade
exports:
  - kind: unsupported_format
    output_path: out/data.unsupported
```

```console
$ kpubdata-builder validate broken_export_kind.yaml
error: spec validation failed:
  - exports[0].kind 'unsupported_format' is not supported; supported kinds: ['csv', 'huggingface', 'jsonl', 'kaggle', 'markdown', 'parquet']
```

지원되는 export kind는 `csv`, `huggingface`, `jsonl`, `kaggle`, `markdown`, `parquet`입니다.

---

### preview — 스키마·샘플 미리 보기

`preview` 명령은 BuildSpec을 검증한 뒤 각 소스(source)의 스키마(컬럼 목록)와 샘플 행을 가져옵니다.
아티팩트(artifact) 파일은 생성하지 않습니다.

```console
$ kpubdata-builder preview --help
usage: kpubdata-builder preview [-h] [--limit LIMIT] spec

positional arguments:
  spec           Path to the BuildSpec YAML file.

options:
  -h, --help     show this help message and exit
  --limit LIMIT  Maximum sample rows per source (default: 5).
```

> **[예시 — 픽스처 기반 출력]** `preview` 명령은 실제 data.go.kr API에 접근하므로
> 아래 출력은 라이브 실행이 아닌 테스트 픽스처를 바탕으로 재구성한 대표 예시입니다.
> 실제 실행을 위해서는 `KPUBDATA_DATAGO_API_KEY` 환경 변수에 유효한 API 키가 필요합니다.

```console
$ export KPUBDATA_DATAGO_API_KEY="<your-data-go-kr-api-key>"
$ kpubdata-builder preview apt_trade_spec.yaml --limit 3
preview: dataset.seoul_apt_trade
  - gangnam_202401: sggCd (Utf8), umdNm (Utf8), aptNm (Utf8), excluUseAr (Float64), floor (Int64), buildYear (Int64), dealYear (Int64), dealMonth (Int64), dealDay (Int64), dealAmount (Utf8)
    sample (3 of 142 rows):
      {'sggCd': '11680', 'umdNm': '개포동', 'aptNm': '개포주공1단지', 'excluUseAr': 41.96, 'floor': 3, 'buildYear': 1982, 'dealYear': 2024, 'dealMonth': 1, 'dealDay': 6, 'dealAmount': '82,500'}
      {'sggCd': '11680', 'umdNm': '개포동', 'aptNm': '개포주공2단지', 'excluUseAr': 36.0, 'floor': 9, 'buildYear': 1983, 'dealYear': 2024, 'dealMonth': 1, 'dealDay': 8, 'dealAmount': '53,500'}
      {'sggCd': '11680', 'umdNm': '역삼동', 'aptNm': '역삼래미안', 'excluUseAr': 59.97, 'floor': 12, 'buildYear': 2003, 'dealYear': 2024, 'dealMonth': 1, 'dealDay': 11, 'dealAmount': '120,000'}
```

소스 fetch에 실패하면 표준 오류(stderr)에 오류 메시지가 출력되고 종료 코드 `1`이 반환됩니다.

---

### build — 파이프라인 전체 실행

`build` 명령은 BuildSpec의 Medallion 파이프라인(Bronze → Silver → Gold)을 전체 실행하고
아티팩트(artifact)와 매니페스트(manifest)를 생성합니다.

```console
$ kpubdata-builder build --help
usage: kpubdata-builder build [-h] [--output-dir OUTPUT_DIR] [--run-id RUN_ID]
                              [--warehouse DIR] [--workspace-id WORKSPACE_ID]
                              [--warehouse-keep N]
                              spec

positional arguments:
  spec                  Path to the BuildSpec YAML file.

options:
  -h, --help            show this help message and exit
  --output-dir OUTPUT_DIR
                        Run workspace root directory (default: build).
  --run-id RUN_ID       Run identifier (default: generated timestamp).
  --warehouse DIR       Commit each source's Gold output as a table snapshot
                        under DIR, so the build ends at a queryable table.
                        Needs no publish credential. Omitted, nothing is
                        written to a catalog.
  --workspace-id WORKSPACE_ID
                        Owning workspace for materialised tables (default:
                        ws_personal).
  --warehouse-keep N    Keep the N most recent snapshots of each table this
                        build commits and reclaim the rest (default: 3). Use
                        -1 to keep every snapshot. Ignored without
                        --warehouse.
```

> **[예시 — 픽스처 기반 출력]** `build` 명령은 실제 data.go.kr API에 접근하므로
> 아래 출력은 테스트 픽스처를 바탕으로 재구성한 대표 예시입니다.
> 실제 실행을 위해서는 `KPUBDATA_DATAGO_API_KEY` 환경 변수에 유효한 API 키가 필요합니다.

```console
$ export KPUBDATA_DATAGO_API_KEY="<your-data-go-kr-api-key>"
$ kpubdata-builder build apt_trade_spec.yaml --output-dir ./build --run-id run-20240601
build: dataset.seoul_apt_trade (run run-20240601)
  - gangnam_202401: ok [bronze, silver, gold]
manifest: <path>/build/run-20240601/manifest.json
```

`--run-id`를 생략하면 타임스탬프 기반 식별자가 자동 생성됩니다.

빌드가 실패하면 실패한 소스 키와 오류 내용이 표준 오류(stderr)에 출력되고
종료 코드 `1`이 반환됩니다.

```console
$ kpubdata-builder build apt_trade_spec.yaml
build: dataset.seoul_apt_trade (run 20240601-153022)
  - gangnam_202401: failed [-]
error: build failed for one or more sources
  - gangnam_202401: ConfigError: KPUBDATA_DATAGO_API_KEY not set
```

---

### warehouse-gc — 창고 회수

`--warehouse` 로 빌드하면 갱신할 때마다 새 snapshot 이 커밋된다. **커밋만 하고 회수하지
않으면 Gold 한 벌이 통째로 남는다** — 매일 갱신하는 데이터셋이면 1년에 365벌이다.

빌드는 **자기가 방금 커밋한 테이블만** 회수한다(`--warehouse-keep`, 기본 3). 그것으로
닿지 않는 두 가지가 있고, 이 명령이 그 둘을 맡는다.

- **다시 빌드하지 않는 테이블.** 빌드가 안 도니 빌드 시점 회수도 안 돈다.
- **크래시가 남긴 `staging`.** 나이로만 판단할 수 있어서 **그 빌드 안에서는 판단이 안 된다.**
  카탈로그가 그 디렉터리를 알고 있으므로 고아 회수(`collect_orphan_staging`)가 일부러
  건너뛴다 — 아무도 손대지 않는 자리다.

```console
$ kpubdata-builder warehouse-gc --help
usage: kpubdata-builder warehouse-gc [-h] [--keep N] [--stale-hours H]
                                     [--workspace-id WORKSPACE_ID]
                                     DIR

positional arguments:
  DIR                   Table catalog root, the same directory `build
                        --warehouse` was given.

options:
  -h, --help            show this help message and exit
  --keep N              Committed snapshots to retain per table, newest first
                        (default: 3).
  --stale-hours H       Treat an uncommitted snapshot older than H hours as a
                        crashed build and mark it reclaimable (default: 24).
                        The catalog cannot tell a crashed build from a slow
                        one, so this is a statement about how long a build of
                        yours may take, not a fact the catalog knows.
  --workspace-id WORKSPACE_ID
                        Only collect tables of this workspace (default: every
                        workspace).
```

```console
$ kpubdata-builder warehouse-gc ./warehouse
  dataset.seoul_apt_trade.gangnam_202401: removed 2
  dataset.seoul_apt_trade.seocho_202401: removed 0, kept 1 leased
warehouse warehouse: reclaimed 2 directories
```

무엇을 **남겼는지**도 말한다. "할 일이 없었다" 와 "전부 사용 중이었다" 를 구분하지 못하면
회수가 멈춘 창고를 진단할 수 없다.

지우지 못하는 것이 두 가지 있고, 둘 다 정상이다.

- **현재 snapshot.** 지우면 읽을 것이 없어진다.
- **리스(lease)가 살아 있는 snapshot.** 쿼리가 읽는 중이다. 리스가 끝난 뒤 다시 돌리면 된다.

회수는 **빌드를 실패시키지 않는다.** 커밋까지 간 빌드는 사용자가 요청한 데이터셋을 이미
만들었고, 청소가 실패했다고 그걸 버리는 건 잘못된 교환이다. 실패하면 로그에 남고, 나중에
이 명령으로 따라잡는다.

---

### publish — 아티팩트 게시

`publish` 명령은 이미 생성된 아티팩트를 로컬 디렉터리, Hugging Face, 또는 Kaggle에 게시합니다.

```console
$ kpubdata-builder publish --help
usage: kpubdata-builder publish [-h] [--target {huggingface,kaggle,local}]
                                --destination DESTINATION --artifacts-dir
                                ARTIFACTS_DIR [--public]
                                spec

positional arguments:
  spec                  Path to the BuildSpec YAML file.

options:
  -h, --help            show this help message and exit
  --target {huggingface,kaggle,local}
                        Publish target (default: local).
  --destination DESTINATION
                        Local directory path (local) or HF repo id
                        (huggingface).
  --artifacts-dir ARTIFACTS_DIR
                        Directory whose files will be published.
  --public              Create new datasets as public (kaggle only; default:
                        private).
```

로컬 디렉터리로 복사하는 예시입니다.

```console
$ kpubdata-builder publish apt_trade_spec.yaml \
    --target local \
    --destination ./dist/apt_trade \
    --artifacts-dir ./build/run-20240601
publish: dataset.seoul_apt_trade -> local
  target: <path>/dist/apt_trade
  artifacts: 2
```

---

## 서비스 모드 (HTTP API)

`kpubdata-builder` 패키지는 HTTP 서비스로 실행할 수도 있습니다.
서비스는 stdlib `http.server` 어댑터 기반이며 추가 의존성 없이 실행됩니다.

### 서비스 기동

```console
$ kpubdata-builder serve --output-dir build --host 127.0.0.1 --port 8000
```

라이브러리로 직접 기동할 수도 있습니다 — 커스텀 `client_factory` 를 주입하거나
임베드해 실행할 때 쓰입니다.

```python
# serve_dev.py  —  개발·로컬 테스트용 기동 스크립트 예시
from pathlib import Path
from kpubdata_builder.service import BuilderService, serve
from kpubdata_builder.stages.bronze.build import SourceClient

def _client_factory() -> SourceClient:
    from kpubdata import Client
    return Client.from_env()  # type: ignore[return-value]

service = BuilderService(
    output_root=Path("build"),
    client_factory=_client_factory,
)
serve(service, host="127.0.0.1", port=8000)
```

```console
$ python serve_dev.py
# 서버가 127.0.0.1:8000에서 요청을 대기합니다 (Ctrl-C로 종료).
```

`/validate`와 `/version`은 API 키 없이 오프라인으로 사용할 수 있습니다.
`/preview`와 `/build`는 `KPUBDATA_DATAGO_API_KEY` 환경 변수가 필요합니다.

### GET /version

서비스가 노출하는 API 계약 버전을 확인합니다.

```console
$ curl -s http://127.0.0.1:8000/version
```

```json
{
  "service": "kpubdata-builder",
  "api_version": "1.0.0"
}
```

### POST /validate

BuildSpec YAML 문자열을 JSON body의 `spec` 필드로 전달합니다.

검증 성공:

```console
$ curl -s -X POST http://127.0.0.1:8000/validate \
    -H "Content-Type: application/json" \
    -d '{
      "spec": "dataset_id: dataset.sample\ntitle: Sample\ndescription: test\nsources:\n  - provider: datago\n    dataset: apt_trade\nexports:\n  - kind: jsonl\n    output_path: out/data.jsonl\n"
    }'
```

```json
{
  "status": "valid",
  "dataset_id": "dataset.sample",
  "api_version": "1.0.0"
}
```

검증 실패 (지원하지 않는 export kind):

```json
{
  "status": "invalid",
  "problems": [
    "exports[0].kind 'unsupported_format' is not supported; supported kinds: ['csv', 'huggingface', 'jsonl', 'kaggle', 'markdown', 'parquet']"
  ]
}
```

### POST /build

> **[예시 — 대표 응답]** `POST /build`는 실제 data.go.kr API에 접근하므로
> 아래 응답은 테스트 픽스처를 바탕으로 재구성한 대표 예시입니다.
> 실제 실행을 위해서는 `KPUBDATA_DATAGO_API_KEY` 환경 변수에 유효한 API 키가 필요합니다.

BuildSpec YAML 문자열을 JSON body의 `spec` 필드로 전달합니다. 선택적으로 `run_id`를 지정할 수 있습니다.

빌드 성공 (HTTP 200):

```console
$ curl -s -X POST http://127.0.0.1:8000/build \
    -H "Content-Type: application/json" \
    -d '{"spec": "<spec-yaml-string>", "run_id": "run-20240601"}'
```

```json
{
  "status": "ok",
  "run_id": "run-20240601",
  "outcomes": [
    {
      "source_key": "gangnam_202401",
      "status": "ok",
      "stages_completed": ["bronze", "silver", "gold"],
      "error": null
    }
  ],
  "manifest": "build/run-20240601/manifest.json",
  "api_version": "1.0.0"
}
```

빌드 실패 (HTTP 502 — 업스트림 소스 fetch 실패):

```json
{
  "status": "failed",
  "run_id": "run-20240601",
  "outcomes": [
    {
      "source_key": "gangnam_202401",
      "status": "failed",
      "stages_completed": [],
      "error": "ConfigError: KPUBDATA_DATAGO_API_KEY not set"
    }
  ],
  "manifest": "build/run-20240601/manifest.json",
  "api_version": "1.0.0"
}
```

### GET /artifacts/{run_id}

지정한 실행 워크스페이스의 산출물 파일 목록을 반환합니다.

```console
$ curl -s http://127.0.0.1:8000/artifacts/run-20240601
```

```json
{
  "run_id": "run-20240601",
  "files": [
    "gangnam_202401/apt_trade.jsonl",
    "manifest.json"
  ]
}
```

실행 ID가 존재하지 않으면 HTTP 404가 반환됩니다.

---

구현된 엔드포인트는 `GET /version`, `POST /validate`, `POST /preview`, `POST /build`,
`GET /artifacts/{run_id}`이며, 이것이 실제 동작의 기준입니다.
공식 API 계약(스키마·버전 협상 포함)은 PR #231에서 실제 구현과 동기화된
[`contract/builder-api.yaml`](https://github.com/yeongseon/kpubdata-builder/blob/main/contract/builder-api.yaml)을 참고하세요.

---

## 환경 변수

| 변수 | 설명 |
| :--- | :--- |
| `KPUBDATA_DATAGO_API_KEY` | data.go.kr 공공데이터 API 인증 키. `preview` 및 `build` 명령 실행 시 필요합니다. |

API 키는 [data.go.kr](https://www.data.go.kr/)에서 회원가입 후 발급받을 수 있습니다.

---

## 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [에러 처리](error-handling.md) | 에러 계층 및 단계별 에러 처리 정책 |
| [예제: 서울 아파트 실거래가](../examples/seoul-apt-trade.md) | 종단 간 데이터 수집·게시 예제 |
| [API 규약](../API_CONTRACT.md) | 서비스 HTTP API 상세 규약 |

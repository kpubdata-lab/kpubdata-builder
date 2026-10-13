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
    settings         Print the settings this environment gives Builder, with
                     every secret redacted, and check them as `serve` does at
                     start (#1108).
    fixtures         Export the replay fixtures bundled with this package
                     (#837).
    rebuild-index    Rebuild the build index from filesystem scans.
    discover         Discover API metadata from a data.go.kr URL.
    monitor          Check pending dataset applications for approval.
    pipeline         Run automated onboarding pipeline for a dataset.
    verify           Verify dataset specs against live APIs.
    prune-cancelled  List (and optionally delete) cancelled partial-run
                     artifacts past a TTL (#549).
    prune-runs       List (and optionally delete) succeeded and failed runs
                     past their retention, keeping any run something still
                     refers to (#1099).
    delete-user      Delete one user and their data step by step, and shut
                     them out first (#1102). Without --apply, only says what
                     would go.
    warehouse-gc     Reclaim snapshots and staging directories nothing needs
                     any more (#738).
    warehouse-backup
                     Back up the table catalog and its snapshot files together
                     (#705).
    warehouse-restore
                     Restore a warehouse backup into an empty directory, after
                     checking the catalog and the snapshot files against each
                     other (#705).
    backup           Write all of Builder's state — every SQLite store, the
                     runs, the uploads, the warehouse — into one archive, while
                     nothing writes it (#1095).
    verify-backup    Check a state archive as a restore would — checksums,
                     SQLite integrity, schema versions, the warehouse and the
                     uploads — and change nothing (#1095).
    restore          Replace the output directory's state with a verified state
                     archive, by staging and then swapping it; the earlier state
                     is kept aside (#1095).
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

`--warehouse` 를 준 빌드는 소스가 모두 성공해도 **테이블 커밋이 거부되면 종료 코드 `1`** 입니다(#1329).
산출물과 manifest 는 그대로 남고, manifest 의 `warehouse_failures` 와 표준 오류의 사유 코드가 같습니다.
서비스의 `POST /build` 가 409 로 답하는 것과 같은 경우입니다.

| 사유 코드 | 뜻 |
|---|---|
| `conflict` | 이 빌드가 시작한 뒤 다른 빌드가 같은 테이블을 커밋했다. 더 새 snapshot 이 유지된다 |
| `empty_result` | 빌드 결과가 0행이고 테이블에 이미 snapshot 이 있다. 소스에 `allow_empty: true` 가 없으면 덮어쓰지 않는다 |
| `commit_failed` | snapshot 을 커밋하지 못했다 |

```console
$ kpubdata-builder build air_spec.yaml --warehouse ./warehouse
build: air (run 20240601-153022)
  - m: ok [bronze, silver, gold]
manifest: <path>/build/20240601-153022/manifest.json
  no table committed
error: build did not commit its table for one or more sources
  - m: empty_result: the build found no rows; the current snapshot (22 rows) stays current. Declare allow_empty: true on the source to commit an empty result
$ echo $?
1
```

`--warehouse` 없이 실행한 빌드는 커밋을 요청하지 않은 것이므로, 소스가 성공하면 종료 코드 `0` 입니다.

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

### prune-runs — 기한이 지난 run 정리

성공·실패한 run 의 작업 공간은 아무것도 지우지 않으면 디스크에 계속 쌓인다. `prune-runs` 는
manifest 의 `finished_at` 이 보관기한(`--retention-days`, 없으면
`KPUBDATA_BUILDER_RUN_RETENTION_DAYS`)보다 오래된 run 을 찾는다. 기본은 dry run 이라 지울 run 과
크기, 남길 run 과 이유만 적고, `--apply` 를 줘야 지운다. 실행 중이거나 재시도가 이어받는 run, 게시한
run, warehouse snapshot(hold·lease 포함)이 있는 run 은 지우지 않는다. 보관기한의 기본은 끔이다 —
값은 메인테이너가 정한다. 자세한 동작과 중단 뒤의 복구는 [배포 가이드 §6.3](../deploy.md) 에 있다.

```console
$ kpubdata-builder prune-runs --output-dir /data --warehouse /data/warehouse --retention-days 30
pruning expired runs under /data, retention 30 day(s) — dry run (nothing will be deleted)
would delete: 20260801T000000Z (ok, finished 2026-08-01T00:10:00+00:00, 12.3 MB)
kept: 20260802T000000Z (published)
1 run(s) to delete, would free 12.3 MB; 1 expired run(s) kept, 4 within the retention
```

---

### delete-user — 사용자와 그 데이터의 삭제

삭제 요청을 받은 사용자 한 명과 그 데이터를 지운다. `USER_ID` 는 `GET /admin/users` 가 주는
`user_id`(`oidc:...`)다. 기본은 dry run 이라 단계마다 지울 개수만 적고 아무것도 쓰지 않는다.
`--apply` 를 주면 먼저 삭제 기록(tombstone)을 남겨 서버가 그 사용자를 거부하게 하고, 그다음
단계별로 지운다. 무엇이 막으면(끝나지 않은 빌드, hold·lease 가 있는 snapshot) 그것을 적고 종료
코드 1 로 멈추며, 다시 돌리면 이어서 끝낸다. 자세한 동작은 [배포 가이드 §6.4](../deploy.md) 에 있다.

```console
$ kpubdata-builder delete-user oidc:3f9a… --output-dir /data --warehouse /data/warehouse
deletion 5c1e… — dry run (nothing will be changed)
would delete: analyses (1)
would delete: exports (0)
would delete: warehouse (2)
would delete: runs (3)
would delete: uploads (1)
would delete: provider_keys (1)
would delete: provider_tests (1)
would delete: ledger (1)
$ kpubdata-builder delete-user oidc:3f9a… --output-dir /data --warehouse /data/warehouse --apply --wait 60
…
deletion 5c1e… is complete
$ kpubdata-builder delete-user --resume --output-dir /data --warehouse /data/warehouse
no unfinished deletion in /data
```

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

### discover · monitor · pipeline — 정기 보고

`dataset-automation.yml` 워크플로가 매일(02:43 UTC) `scripts/dataset_automation_report.py`
를 돌린다 (kpubdata#448). 읽기 전용이고 보고만 한다.

- `monitor --check`: kpubdata 카탈로그가 `application_required` 로 표시한 데이터셋 전부를,
  작업 디렉터리(`RUNNER_TEMP`)의 상태 파일로 확인한다. `HEALTHY` 가 나오면 활용신청이
  승인된 것이다.
- `discover`: 그 데이터셋들이 가리키는 data.go.kr API 페이지마다 한 번씩 돌려, spec 초안에
  엔드포인트가 잡히는지만 본다. 초안은 파일로 쓰지 않는다.
- `pipeline`: 돌리지 않는다. `--skip-pr` 로도 실 API 로 fixture 를 기록하므로 읽기 전용이
  아니다. 승인된 데이터셋에 대해 보고서가 다음 단계로 안내만 한다. 자동 신청(apply)과
  실 API 기록·PR 생성은 신뢰된 러너와 secret 이 준비될 때까지 하지 않는다.

결과는 이슈 **하나**("scheduled report of dataset application approvals and spec drafts",
`epic:datasets`)에 남는다. 승인이나 초안이 있으면 열고, 지난 실행과 같으면 쓰지 않고, 달라지면
두 표지(`dataset-automation:begin/end`) 사이만 고쳐 쓰고 댓글을 단다. 닫지는 않는다.
rate limit 같은 그 밖의 상태는 지문(fingerprint)에 넣지 않아 밤마다의 잡음으로 이슈가
바뀌지 않는다.

data.go.kr 키(`KPUBDATA_DATAGO_API_KEY` secret)가 없으면 monitor 가 승인과 거절을 가를 수
없으므로, "secret not configured" notice 를 남기고 아무것도 돌리거나 쓰지 않은 채 0 으로
끝난다 (secret 등록은 kpubdata#900). pull request 와 `dry_run` 은 출력만 한다. 권한은
`contents: read`, `issues: write` 이고, action 은 커밋 SHA 로 고정한다.

`monitor --check` 는 데이터셋마다 이렇게 판정한다 (#1288).

- **spec 이 있는 데이터셋**: 지금처럼 `verify_dataset` 으로 확인한다.
- **spec 이 없는 데이터셋**(`application_required` 데이터셋은 모두 어댑터 카탈로그
  데이터셋이다): 먼저 kpubdata 공개 API `Client.probe` 에 묻고, probe 가 답하지 않으면
  (kpubdata 0.10 의 probe 는 spec 데이터셋만 판정한다) `list(page_size=1)` 로 한 건만
  요청해 kpubdata 의 표준 예외로 판정한다. 키는 `KPUBDATA_DATAGO_API_KEY` 하나를
  `datago` 이름으로만 넘기고(`localdata`·`semas` 도 이 키를 쓴다), 출력이나 상태 파일에
  쓰지 않으며, 클라이언트가 열려 있는 동안 로그 마스킹에 등록한다.

| 결과 | monitor 상태 |
|---|---|
| probe `available`, `list` 성공 | `HEALTHY` (승인됨) |
| probe `application_required`, 403 (`AuthError` 또는 403 을 담은 `TransportError`) | `NEEDS_APPLICATION` (아직 대기) |
| 401 | `INVALID_KEY` (아직 대기) |
| probe `retired` | `BROKEN_ENDPOINT` |
| kpubdata 가 모르는 데이터셋 | `SPEC_NOT_FOUND` |
| rate limit, timeout, 5xx, 키 없음, 파라미터 오류, 그 밖의 probe 상태 | 판정 없음 — 상태를 바꾸지 않는다 |

호출은 데이터셋마다 한 번이고, provider 별 egress 한도(`KPUBDATA_BUILDER_PROVIDER_CALLS_PER_MINUTE`,
#1245)에 차례를 기다린 뒤 나간다. 데이터셋 사이에는 1초(`CHECK_INTERVAL_SECONDS`)를 쉬고,
응답은 15초까지만 기다린다(재시도 없음). 정기 보고는 열린 이슈가 승인됨으로 적어 둔 데이터셋을
`HEALTHY` 로 시작하므로, 판정이 없는 밤에는 승인 목록이 그대로이고 실제 거절이 나와야
빠진다. dry run 은 이슈를 읽지 않는다.

data.go.kr 은 등록되지 않은 키와 활용신청하지 않은 서비스를 모두 403 으로 답하므로, 가짜 키로
돌린 dry run 은 94개 모두 `NEEDS_APPLICATION` 이 된다.

```bash
KPUBDATA_DATAGO_API_KEY=... uv run --no-sources \
  python scripts/dataset_automation_report.py --dry-run --work-dir /tmp/report
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
[`contract/builder-api.yaml`](https://github.com/kpubdata-lab/kpubdata-builder/blob/main/contract/builder-api.yaml)을 참고하세요.

---

### settings — 지금 환경의 설정 보기

```console
$ kpubdata-builder settings
$ kpubdata-builder settings --json
```

이 환경에서 Builder 가 읽게 될 설정을 모두 출력하고, `serve` 가 시작할 때 하는 검사를 그대로 합니다.
아무것도 띄우지 않습니다. 비밀 값을 담는 설정은 값 대신 `<redacted>` 로 나옵니다. `serve` 가 기동을
거부할 값이 있으면 종료 코드가 1 입니다. 출력의 읽는 법은
[배포 및 설정 가이드](../deployment.md)의 "실제로 쓰이는 설정 보기"에 있습니다.

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

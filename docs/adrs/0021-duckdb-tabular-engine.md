# ADR 0021 — DuckDB 를 단일 tabular 엔진으로

- 상태: 제안됨 — 2026-09-30 소유자 실행 계획(#864)을 ADR 로 옮긴 것. 소유자 ADR 검토 대상
- 관련 이슈: #864 (이 ADR), #865–#877 (실행 단계), #622, #701, #704
- 관련 문서: [ADR 0018 — 레거시 publish 파이프라인](./0018-legacy-publish-pipeline.md),
  [ADR 0020 — 자격 증명의 수명](./0020-credential-lifetime-by-deployment.md)

## 맥락

Builder 의 canonical tabular runtime 은 Polars 다. 처음 Builder 는 artifact 를 만드는 파이프라인이었고,
그 역할에는 Polars 가 맞았다. 지금 Builder 는 다음을 한 실행 모델에서 다룬다.

```text
Bronze ingestion / Silver normalization / Gold composition / Warehouse snapshots
SQL query / Aggregate·Profile·Export / Saved Analysis
```

그리고 여러 지점에서 **전체 데이터를 Python/Polars 메모리에 올린다.**

| 단계 | 지금 | 
|---|---|
| Bronze | `records: list` → `tuple` |
| Silver | `pl.DataFrame` |
| Gold | `pl.DataFrame` |
| Export | `table.to_dicts()` → `tuple(records)` |
| Query | `pl.scan_parquet()` → `collect()` |

### 근거 — 측정과 관찰

- **측정.** 같은 BuildSpec 으로 서울 공공자전거 파일 source 를 빌드했을 때 945.6 MiB 는 성공하고
  1,444 MiB 는 실패했다(#622). 실패 지점은 업로드 BLOB 이었고, 그것을 파일로 옮긴(#653) 뒤에도
  records list·DataFrame 의 전량 materialization 은 그대로다 — 한계가 옮겨졌을 뿐이다.
- **관찰.** 위 표의 다섯 지점이 각각 전체 데이터를 들고 있다. 질의(#701)와 내보내기(#819)는
  별도 자식 프로세스에서 `collect()` 하므로 질의 하나가 테이블 전체 크기만큼 메모리를 쓸 수 있다.
- 이 두 가지가 "엔진 교체는 측정된 한계에서 따라 나와야 한다"(#701·#704 본문)의 조건을 채운다.
  **이 ADR 이 그 두 문장을 대체한다.**

## 결정

production canonical tabular engine 을 **DuckDB 로 통일한다.** 최종 architecture 에 Polars/DuckDB
dual engine 은 두지 않는다 — **big-bang cutover, staged implementation.** 전환 중에는 migration
branch 에서 Polars 결과와 DuckDB 결과를 비교(#865 parity baseline)하지만, cutover(#876) 뒤에
`src/kpubdata_builder` 는 DuckDB 만 쓴다.

```text
Bronze (file-backed / streaming)
        ↓
DuckDB (single canonical tabular runtime)
        ↓
Silver / Gold / Quality
        ↓
Parquet
        ↓
Warehouse / Query / Export
```

### D1 — legacy publish

ADR 0018 에 따라 `scripts/pipeline/`·`scripts/publish_to_hf.py` 는 마지막 legacy config 가 BuildSpec
으로 옮겨질 때까지 남는다. 그래서 cutover 뒤에도:

- `src/kpubdata_builder` → **DuckDB only**
- `scripts/pipeline` → legacy Polars 를 **임시로** 허용

Polars 는 runtime core dependency 에서 빠지고 `legacy-publish` 전용 extra 로 격리된다.

### D2 — DuckDB dependency

정상 build·query 에 필요하므로 optional extra 가 아니라 **core dependency** 다. 최소 버전은 다음이
모두 있는 **최초 버전을 확인한 뒤** 고정한다: `allowed_paths`, `allowed_directories`,
`lock_configuration`, `enable_external_access`, `max_temp_directory_size`. 실험에 쓴 1.5.6 을
검증 없이 최소 버전으로 선언하지 않는다(확인은 #866).

### D3 — dtype vocabulary

기존 문자열 `Int64`, `Float64`, `String`, `Boolean`, `Date`, `Datetime`, `Decimal(...)` 은 유지하되,
뜻을 *Polars dtype* 에서 **Builder canonical dtype** 으로 바꾼다. DuckDB `BIGINT` → `Int64`,
`VARCHAR` → `String`, `DOUBLE` → `Float64` 처럼 옮긴다. storage engine 의 타입 이름이 public
contract 가 되지 않게 한다(#866, #868).

### D4 — Exporter contract

`ArtifactDataset.records: tuple[dict, ...]` 계약을 없애고 **replayable data source** 계약을 둔다.
원칙: one-shot iterator 금지(두 번 읽을 수 있어야 한다), 전체 tuple materialization 금지, Parquet
fast-path 허용, batch iteration 허용(#873).

### D5 — checksum / digest

**logical data checksum** 과 **artifact byte digest** 를 나누고, checksum 에 algorithm identifier 를
기록한다. 기존 checksum 과 새 streaming checksum 을 값만으로 비교하지 않는다(#867).

### D6 — Saved Analysis

저장된 분석에 `sql`, `sql_dialect`, `engine`, `engine_version`, `query_contract_version` 을 함께
저장한다. Polars 시대 SQL 을 DuckDB 에서 **묵시적으로 다시 실행하지 않는다**(#875).

### D7 — ratio split

`random.Random(seed).shuffle(range(N))` 은 O(N) index 메모리를 쓴다. 대용량에서는 새 deterministic
split algorithm 을 쓴다. 권고는 **`hash-sort-v2`** — 명시적 row ordinal 과 seed 로 결정적 key 를
만들고 DuckDB external sort 로 정확한 split 개수를 배분한다. manifest 에 algorithm version 을
기록한다(#871).

### D8 — row order

DuckDB 의 암묵적 insertion order 에 재현성을 기대지 않는다. Bronze ingestion 때 내부 ordinal
`_kpubdata_row_seq` 를 붙이고, 순서가 계약인 작업은 반드시 이 컬럼으로 순서를 명시한다. 이 내부
컬럼은 최종 사용자 schema 에 드러나지 않는다.

### D9 — resource budget

DuckDB `memory_limit` 만으로 프로세스 RSS 가 묶인다고 보지 않는다. 다음 층을 함께 쓴다:
프로세스 `RLIMIT_AS`, DuckDB `memory_limit`, DuckDB `threads`, DuckDB `max_temp_directory_size`,
질의 동시 실행 수, 질의 메모리 admission(#701, #858), build·source 동시 실행 수.

## 하지 않는 것

- #704 multi-table SQL 을 이 전환에 넣지 않는다 — 전환 뒤 따로 한다
- `.duckdb` 파일을 snapshot artifact 로 쓰지 않는다 — snapshot 은 Parquet 그대로다
- Warehouse catalog 를 DuckDB DB 파일로 바꾸지 않는다 — catalog 는 SQLite(ADR 0016 의 백엔드 선택 포함) 그대로다
- 기존 Parquet snapshot 을 강제로 변환하지 않는다
- 제품명을 `Engine` 으로 바꾸지 않는다(#833 이 되돌린 그대로)
- 관련 없는 dead-code 정리를 전환에 섞지 않는다

## 기각한 대안

| 대안 | 기각 이유 |
|---|---|
| Polars 유지 + lazy/streaming 확대 | 질의·집계·내보내기는 이미 `scan_parquet` 이지만 SQL 경로가 `collect()` 로 끝나고, Polars streaming 은 모든 연산을 지원하지 않아 materialization 지점이 연산 종류에 따라 달라진다. 자원 상한(D9)을 연산마다 따로 보장해야 한다 |
| Polars 와 DuckDB 를 영구히 함께 (dual engine) | 같은 타입·캐스트·정렬 의미를 두 엔진에서 지켜야 한다. dtype vocabulary(D3)가 두 엔진에 묶이고, 결과가 엔진 선택에 따라 달라질 수 있다 — 재현성 약속과 맞지 않는다 |
| Arrow compute / pyarrow dataset 직접 사용 | SQL 이 없어 query·aggregate 를 따로 구현해야 하고, external sort·spill 을 직접 만들어야 한다 |
| 외부 엔진(Spark, DataFusion 서버 등) | self-hosted 단일 호스트 배포(ADR 0017)에 별도 서비스·JVM 을 요구한다 |
| SQLite 를 tabular 엔진으로 | 컬럼 지향이 아니고 Parquet 을 직접 읽지 못한다 |

## 다른 결정과의 관계

- **ADR 0018** — 레거시 publish 경로는 D1 대로 Polars 를 임시로 유지한다. 마지막 config 가
  옮겨질 때 레거시 코드와 함께 Polars 도 저장소에서 빠진다.
- **#622** (대용량 source) — 전환의 4단계다. file-backed/streaming Bronze 가 DuckDB 입력이 된다.
- **#701** (자원 상한) — 12단계에서 남은 자원·sandbox 항목을 DuckDB 기준으로 마무리한다. 이미
  들어간 질의 메모리 admission(#858)은 D9 의 한 층으로 남는다. #701 본문의 "Polars 교체는 하지
  않는다" 는 이 ADR 이 대체한다.
- **#704** (multi-table SQL) — 전환 뒤 따로 한다. #704 본문의 "엔진 교체는 측정된 한계에서"
  역시 이 ADR 이 대체한다.

## 실행 순서

```text
[01] ADR 0021 (이 문서)            #864
[02] Polars parity baseline         #865
[03] DuckDB runtime foundation      #866
[04] Upload/Bronze streaming        #622
[05] checksum / digest versioning   #867
[06] type/cast compatibility        #868
[07] Silver migration               #869
[08] Gold select/compose            #870
[09] deterministic split v2         #871
[10] Quality migration              #872
[11] Exporter data-source contract  #873
[12] 남은 자원·sandbox               #701
[13] Query migration                #874
[14] Saved Analysis dialect         #875
[15] Polars runtime removal         #876
[16] docs / API contract / 배포     #877
→ #704 multi-table SQL (전환 뒤)
Studio 후속: kpubdata-lab/kpubdata-studio#565
```

## 결과

- 한 엔진이 build 와 질의를 모두 맡아, 메모리 상한과 spill 을 한곳(D9)에서 설정한다.
- 대용량 source 가 메모리가 아니라 디스크 크기로 제한된다.
- dtype·checksum·split·saved analysis 는 버전이 붙은 계약이 되어, 전환 전후 결과를 값만으로
  섞어 비교하지 않는다.
- 전환 기간에는 두 엔진의 결과를 비교하는 비용이 들고, cutover 뒤에는 사라진다.

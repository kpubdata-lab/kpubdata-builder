# BuildSpec Contract — KPubData Builder

## 1. 목적

BuildSpec은 Builder가 실행하는 모든 빌드의 **단일 입력 계약**입니다. Builder는 BuildSpec을 기준으로 source 실행, export, output 디렉터리 결정, publish 요청, manifest 기록을 수행합니다.

> 참고: Bronze/Silver/Gold는 Builder 내부의 Medallion pipeline stage이며 orchestrator가 관리합니다. 현재 BuildSpec에는 이를 직접 제어하는 사용자 노출 필드(예: `pipeline.stages`)를 추가하지 않습니다.

## 2. 최소 필수 구조

다음 구조가 최소 요구사항입니다.

```yaml
dataset_id: weather-village-forecast
title: "동네예보 데이터셋"
description: "기상청 동네예보 서비스에서 수집한 기상 예보 데이터"

sources:
  - provider: datago
    dataset: village_fcst
    params:
      base_date: "20250401"
      nx: 55
      ny: 127

exports:
  - kind: markdown
    output_path: artifacts/weather_report.md
```

## 3. 필드 분류

### 3.1 Required fields

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `dataset_id` | string | 빌드 대상 데이터셋의 전역 식별자 |
| `title` | string | 사람이 읽는 데이터셋 제목 |
| `description` | string | 빌드 목적과 데이터 설명 |
| `sources` | array<object> | 1개 이상의 입력 소스 정의 |
| `exports` | array<object> | 1개 이상의 출력 대상 정의 |

### 3.2 Optional fields

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `publish` | boolean | 빌드 후 게시까지 수행할지 여부 (기본값: `false`) |
| `metadata` | object | 산출물에 실을 임의 메타데이터 (JSON 호환 값) |
| `splits` | object | 데이터셋 분할 정의 |
| `pii` | object | PII 검출 정책 |
| `license` | string | 데이터셋 라이선스 또는 이용허락 범위 |
| `quality` | object | Silver 통계 기반 품질 임계 정책 |
| `composition` | object | 두 source를 join해 하나의 결합 Gold dataset을 추가로 만드는 계약 (#506) |

## 4. 필드 상세

### 4.1 `dataset_id`

- 타입: `string`
- 예시: `weather-village-forecast`
- 의미: 빌드 전체를 식별하는 dataset ID입니다.

### 4.2 `title`

- 타입: `string`
- 예시: `"2025년 동네예보 데이터셋"`
- 의미: 사람이 읽는 데이터셋 제목입니다.

### 4.3 `description`

- 타입: `string`
- 예시: `"기상청 동네예보 서비스에서 수집한 기상 예보 및 실제 관측 데이터"`
- 의미: 빌드 목적과 데이터 내용을 설명합니다.

### 4.4 `sources` (배열)

`kind` 필드로 세 종류의 source를 표현합니다(#498). `kind`를 생략하면 항상
`public_api`로 해석되므로 기존 BuildSpec은 수정 없이 그대로 동작합니다.

| kind | 의미 |
| :--- | :--- |
| `public_api` (기본) | 기존 kpubdata provider/dataset 참조 |
| `file` | `POST /uploads`로 사전 업로드한 파일(CSV/JSON/JSONL/Parquet) 참조 |
| `url` | 안전한 GET(Auth=None) HTTP(S) 소스 |

`alias`/`schema`는 세 kind 공통 필드입니다. 그 외 필드는 kind별로 서로
배타적이며, 다른 kind의 필드가 섞이면 BuildSpec 로드 시 즉시 거부됩니다.

#### `kind: public_api` (기본값)

```yaml
sources:
  - provider: datago
    dataset: village_fcst
    params:
      base_date: "20250401"
      nx: 55
      ny: 127
    alias: forecast
    schema:
      required: [base_date, nx, ny]
      dtypes:
        nx: int64
        ny: int64
      casts:
        nx: int64
        ny: int64
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `kind` | string | 아니오 | 생략 시 `public_api` |
| `provider` | string | 예 | `kpubdata` provider 이름 |
| `dataset` | string | 예 | provider 내부 dataset 이름 |
| `params` | object | 아니오 | source 호출 파라미터 (JSON 호환 값) |
| `alias` | string | 아니오 | 조립 단계에서 사용할 사용자 정의 소스 이름 |
| `schema` | object | 아니오 | Silver 정규화 및 required-column/dtype 검증 계약 |

#### `kind: file` (#498)

`POST /uploads`가 발급한 `upload_id`를 참조합니다. 파일시스템 경로를 직접
쓸 수 없습니다 — `upload_id`는 서버가 발급한 불투명한 식별자(`upl_<hex32>`)이며,
업로드 자체는 principal의 owner_id로 격리됩니다(다른 사용자의 upload_id를
참조할 수 없음). `format`/`encoding`은 업로드 시점에 검증된 값과 정확히
일치해야 합니다.

```yaml
sources:
  - kind: file
    upload_id: upl_0123456789abcdef0123456789abcdef
    format: csv
    encoding: utf-8
    alias: uploaded_trades
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `kind` | string | 예 | `file` |
| `upload_id` | string | 예 | `POST /uploads` 응답의 `upload_id` |
| `format` | string | 예 | `csv` \| `json` \| `jsonl` \| `parquet` (업로드 시 검증된 값과 일치해야 함) |
| `encoding` | string | 아니오 | 텍스트 포맷(csv/json/jsonl) 디코딩 인코딩. 기본값 `utf-8`. parquet은 무시됨 |
| `alias` | string | 아니오 | 조립 단계에서 사용할 사용자 정의 소스 이름 |
| `schema` | object | 아니오 | Silver 정규화 및 required-column/dtype 검증 계약 |

지원 포맷은 CSV/JSON(top-level array of objects)/JSONL/Parquet입니다.
Excel/ZIP은 범위 밖입니다.

#### `kind: url` (#498 P0)

GET, Auth=None인 안전한 HTTP(S) 소스만 P0 범위입니다. Bearer credential
연동은 #492 이후 P1 확장입니다.

```yaml
sources:
  - kind: url
    endpoint: https://example.org/data.json
    method: GET
    alias: external_feed
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `kind` | string | 예 | `url` |
| `endpoint` | string | 예 | fetch할 절대 URL. `https`만 허용하며 userinfo(`user:pass@host`)는 금지 |
| `method` | string | 아니오 | 기본값 `GET`. P0는 `GET`만 허용 |
| `format` | string | 아니오 | `json` \| `jsonl` \| `csv`. 생략 시 응답 `Content-Type`으로 추론(기본 `json`) |
| `alias` | string | 아니오 | 조립 단계에서 사용할 사용자 정의 소스 이름 |
| `schema` | object | 아니오 | Silver 정규화 및 required-column/dtype 검증 계약 |

**SSRF 방어(#498)**: `https` 외 scheme(`http`/`file`/`ftp` 등) 거부, userinfo
포함 URL 거부, hostname을 DNS로 직접 resolve해 loopback/private/link-local/
reserved 등 비공인(non-global) 주소로의 fetch 거부(하나라도 비공인이면 전체
거부), 실제 TCP 연결은 검증된 IP에 직접 연결(DNS rebinding 방지), redirect마다
동일 검증 반복(최대 5회), connect/read timeout과 응답 크기 상한 적용. 임의
header나 POST/PUT/PATCH는 계약에 필드 자체가 없어 표현할 수 없습니다.
provenance/manifest에는 query string이 제거된 endpoint만 남습니다.

`schema`는 다음 선택 필드를 지원합니다.

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `required` | array<string> | 반드시 존재해야 하는 컬럼 목록 |
| `dtypes` | object<string, string> | 컬럼별 기대 dtype |
| `casts` | object<string, string> | 정규화 단계에서 적용할 컬럼별 dtype 캐스팅 |
| `rename` | object<string, string> | 원 필드명 → canonical 컬럼명 매핑 (#611) |
| `derived` | array<object> | 기존 컬럼에서 새 컬럼을 만드는 규칙 (#611) |

제거된 `normalization_mode`는 허용하지 않습니다. 정규화는 `schema.casts`로 선언합니다.

적용 순서는 `rename` → `casts` → `derived`입니다. 따라서 `required`/`dtypes`/`casts`/
`derived`의 컬럼명은 모두 **rename 이후의 이름**을 가리킵니다. `rename`이 원본에 없는
컬럼을 가리키면 `TabularError`로 실패합니다 — 상류 스키마 변경을 조용히 넘기지 않습니다.

`rename`은 이름만 바꾸고 컬럼을 버리지 않습니다. 배포용 스크립트(`scripts/pipeline/
transform.py`)의 `column_mapping`은 매핑되지 않은 컬럼을 드롭하지만, Silver는 Bronze의
행과 열을 보존하는 계층이므로 여기서는 드롭하지 않습니다.

`casts`는 `_NAMED_DTYPES`(bool/date/datetime/float/int/str 계열)에 더해 named formatted
cast를 받습니다.

| cast | 동작 |
| :--- | :--- |
| `int_comma` | 천단위 구분자와 주변 공백을 제거한 뒤 `Int64`로 캐스팅 (`"120,000"` → `120000`) |

`int_comma`가 없으면 금액 컬럼에 `int`를 선언했을 때 모든 값이 null이 되어 #188의
data-loss 가드가 빌드를 실패시킵니다.

`derived`의 각 규칙은 `name` / `kind` / `columns`를 갖습니다. 자유형 표현식은 받지
않습니다(`RangeRule`/`CompareColumnsRule`과 같은 typed rule 관례).

| kind | columns | 결과 |
| :--- | :--- | :--- |
| `date_parts` | 정확히 3개 (year, month, day) | `Date` 컬럼 |
| `join_key` | 1개 이상 | `\|`로 이어붙인 문자열 복합키 |

`join_key`는 composition의 단일 컬럼 equi-join으로 다중 키 조인을 표현할 때 씁니다.

```yaml
    schema:
      rename:
        sggCd: district_code
        dealAmount: deal_amount
      casts:
        deal_amount: int_comma
      derived:
        - name: deal_date
          kind: date_parts
          columns: [dealYear, dealMonth, dealDay]
        - name: join_key
          kind: join_key
          columns: [district_code, year_month]
```

`rename`과 `derived`는 canonical BuildSpec snapshot에 실리므로 spec digest에 반영됩니다
— 변환 규칙을 바꾸면 digest가 바뀝니다.

### 4.5 `exports` (배열)

각 export 대상은 다음 필드를 가집니다.

```yaml
exports:
  - kind: markdown
    output_path: artifacts/weather_report.md
  - kind: jsonl
    output_path: artifacts/data.jsonl
    options:
      indent: 2
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `kind` | string | 예 | exporter 레지스트리 키 (`markdown`, `jsonl`, `parquet`, `csv`, `huggingface`, `kaggle` 등) |
| `output_path` | string | 예 | output_dir 기준 상대 출력 경로 |
| `options` | object | 아니오 | exporter별 선택 옵션 (JSON 호환 값) |

#### Studio → Builder 필드명 매핑 (#264)

KPubData Studio에서는 `ExportTarget.format`이라는 필드명을 사용하지만, Builder에서는 `kind`를 사용합니다. Studio에서 Builder로 BuildSpec을 전송할 때, Studio의 `specMapping.ts:toBuilderSpec()` 함수가 자동으로 필드명을 변환합니다.

| Studio (format) | Builder (kind) | 비고 |
| :--- | :--- | :--- |
| `"markdown"` | `"markdown"` | |
| `"jsonl"` | `"jsonl"` | |
| `"parquet"` | `"parquet"` | |
| `"csv"` | `"csv"` | |
| `"huggingface"` | `"huggingface"` | |
| `"kaggle"` | `"kaggle"` | |

> **참고**: 값 자체는 동일하며, 단지 필드명만 `format` → `kind`로 변환됩니다. Builder API를 직접 호출하는 경우(CLI 등)에는 항상 `kind`를 사용해야 합니다.

### 4.6 `publish` (optional)

```yaml
publish: true
```

빌드 후 자동으로 게시(publish) 단계까지 실행할지 여부입니다. 기본값은 `false`입니다.

> 계획(planned)/미구현: 현재 `publish: true`로 설정해도 게시 로직이 실행되지 않습니다. 게시 기능은 향후 릴리스에서 활성화될 예정입니다.

### 4.7 `metadata` (optional)

```yaml
metadata:
  author: "Sisyphus-Junior"
  version: "1.0.0"
  tags: [weather, forecast]
  coverage:
    years: [2024, 2025]
```

임의 메타데이터입니다. 키는 문자열이고 값은 null, 문자열, 숫자,
불리언, 배열, 객체로 구성된 표준 JSON 호환 값이어야 합니다. NaN과 Infinity 및 순환
참조는 허용하지 않습니다.

현재 구현에서 산출물에 반영되는 항목은 제한적입니다. `version`은 dataset card의
버전 표기로, `license`는 최상위 `license` 필드 미지정 시 레거시 폴백으로 사용됩니다.
그 외 임의 메타데이터는 검증·스펙 저장 목적으로만 보관되며 산출물, exporter,
manifest로 전달되지 않습니다.

### 4.8 `splits` (optional)

```yaml
splits:
  mode: ratio
  ratios:
    train: 0.8
    val: 0.1
    test: 0.1
  seed: 42
```

데이터셋을 명명된 분할로 나누는 방법을 정의합니다.

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `mode` | string | 예 | 분할 방식 (`ratio`: 비율 기반, `key`: 컬럼 값 기반) |
| `ratios` | object | 아니오 | ratio 모드에서 분할 이름 → 비율 매핑 (합이 1.0이어야 함) |
| `key` | string | 아니오 | key 모드에서 분할 기준이 되는 컬럼 이름 |
| `seed` | integer | 아니오 | ratio 모드의 결정적 셔플 시드 (기본값: `0`) |

`ratio` 모드는 비율의 합이 1.0이어야 하며, `key` 모드는 비어 있지 않은 `key`가
필요합니다. 이 선언은 Gold 패키지의 분할 생성에 사용됩니다.

### 4.9 `pii` (optional)

```yaml
pii:
  mode: warn
  allow_columns: [contact_hint]
```

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `mode` | string | `block`(기본), `warn`, `allow` 중 하나 |
| `allow_columns` | array<string> | PII 스캔에서 제외할 컬럼 목록 |

`publish: true`와 `pii.mode: allow`의 조합은 허용하지 않습니다.

### 4.10 `license` (optional)

```yaml
license: CC-BY-4.0
```

SPDX 식별자 또는 자유 텍스트 라이선스 선언입니다. `publish: true`이면 반드시
지정해야 합니다.

### 4.11 `quality` (optional)

Silver 통계·테이블에 대한 품질 규칙 선언입니다. 각 규칙 위반은 `quality.evaluator`가
`QualityCheckResult`(PASS 포함)로 구조화해 manifest(`quality_results`)와
`GET /builds/{run_id}/quality`, `GET /datasets/{dataset_id}/quality/history`에
반영합니다(#486). Preview(`POST /preview`)와 Build는 동일한 evaluator를 공유하므로
같은 데이터/규칙에는 항상 같은 판정을 냅니다.

#### 기존 syntax (#446, 하위 호환)

```yaml
quality:
  max_duplicate_rate: 0.01
  max_null_ratio:
    temperature: 0.05
  min_rows: 100
```

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `max_duplicate_rate` | number | 허용할 최대 중복 행 비율. 초과 시 위반(초과가 아니면 위반 아님 — 경계값은 통과). |
| `max_null_ratio` | object<string, number> | 컬럼별 허용 최대 null 비율 |
| `min_rows` | integer | 요구하는 최소 행 수. 미만이면 위반. |

기존 syntax의 위반은 기본적으로 **WARN**입니다 — Build는 계속 진행하고 결과만
기록합니다(#446 시절 semantics 그대로 유지).

#### 명시적 WARN/FAIL severity (#486)

각 규칙에 대응하는 `*_severity` 필드로 명시적 심각도를 선언할 수 있습니다. 값은
`"warn"`(기본) 또는 `"fail"`이며, `max_null_ratio`는 컬럼별 override 맵을 씁니다.

```yaml
quality:
  max_duplicate_rate: 0.01
  max_duplicate_rate_severity: fail   # 명시적 FAIL
  max_null_ratio:
    temperature: 0.05
  max_null_ratio_severity:
    temperature: warn                 # 명시적 WARN(기본과 동일하지만 선언 가능)
  min_rows: 100
  min_rows_severity: fail
```

FAIL로 판정된 rule이 하나라도 있으면 해당 source는 Gold 진입 전에 실패 처리됩니다.
WARN은 계속 진행합니다.

#### `range` (#486)

숫자 컬럼의 최소/최대 범위를 typed rule로 선언합니다. 자유형 Python/eval은
허용하지 않습니다. `min`/`max`는 포함(inclusive) 경계이며 최소 하나는 있어야
합니다. null 값은 범위 위반으로 계산하지 않습니다(missing/null 여부는
`max_null_ratio`가 담당 — 역할 분리).

```yaml
quality:
  range:
    - column: price
      min: 0
      max: 1000000
      severity: fail
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `column` | string | 예 | 대상 컬럼명 |
| `min` | number | 아니오 | 허용 최소값(포함) |
| `max` | number | 아니오 | 허용 최대값(포함) |
| `severity` | string | 아니오 | `warn`(기본) \| `fail` |

평가 결과의 `threshold`는 `{"min": ..., "max": ...}` 구조로 원래 경계를
보존합니다. 컬럼 dtype 때문에 숫자 범위를 평가할 수 없으면 규칙을 생략하지 않고
선언된 severity의 WARN/FAIL 결과와 안전한 `detail`을 기록합니다.

#### `compare_columns` (#486)

두 컬럼 간 비교를 제한된 operator만으로 선언합니다. 자유형 expression/eval은
허용하지 않습니다. 두 컬럼 모두 null이 아닌 행만 평가하며, 비교 불가능한 행을
자동으로 통과 처리하지 않습니다.

```yaml
quality:
  compare_columns:
    - left: sale_price
      operator: gte
      right: list_price
      severity: warn
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `left` | string | 예 | 왼쪽 컬럼명 |
| `operator` | string | 예 | `eq`\|`ne`\|`gt`\|`gte`\|`lt`\|`lte` 중 하나 |
| `right` | string | 예 | 오른쪽 컬럼명 |
| `severity` | string | 아니오 | `warn`(기본) \| `fail` |

평가 결과의 `threshold`는 `{"operator": ..., "right_column": ...}` 구조로
연산자와 오른쪽 컬럼을 보존합니다. dtype이 호환되지 않는 경우에도 선언된 severity의
WARN/FAIL 결과를 남기며, WARN은 Build를 계속하고 FAIL은 Gold 진입을 막습니다.

유효하지 않은 `operator`는 BuildSpec 로드 단계에서 즉시 거부됩니다.

#### Schema 계약과의 관계

`sources[].schema.required`/`dtypes`(#437)도 같은 `QualityCheckResult` 모델로
구조화됩니다(`category: "schema"`, `rule: "required_column"` \| `"dtype"`). required
컬럼이 없어 dtype을 검사할 수 없는 경우 dtype PASS를 만들지 않습니다 — "required
FAIL + dtype PASS" 같은 모순을 피합니다. Schema 위반은 severity 설정과 무관하게
항상 hard failure입니다(#189의 기존 게이트 그대로).

### 4.12 `composition` (optional)

두 source의 검증된 Silver 테이블을 join해 하나의 결합 Gold dataset을
추가로 만드는 계약입니다(#506). `composition` 없는 기존 multi-source
BuildSpec은 그대로 동작합니다 — source별 독립 Gold가 회귀 없이 유지되고,
composition은 부가 산출물로 `gold/{composition.name}/`에 추가됩니다.

초기 범위는 **두 source, 단일 equi-join**으로 제한합니다. 자유형 SQL
transformation과 3개 이상의 join graph는 지원하지 않습니다.

```yaml
sources:
  - provider: datago
    dataset: apt_trade
    alias: sales
  - provider: datago
    dataset: region_meta
    alias: region

composition:
  name: sales_with_region
  join:
    left: sales
    right: region
    left_key: region_id
    right_key: id
    type: inner
    on_duplicate_key: warn
```

복합 key와 cardinality 선언(#698):

```yaml
composition:
  name: trade_with_population
  join:
    left: trade
    right: population
    keys:
      - left: region_id
        right: region_id
      - left: month
        right: month
    cardinality: many_to_one
    on_null_key: warn
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `name` | string | 예 | 결합 결과 Gold dataset 이름. 다른 source의 output key(alias 또는 `provider.dataset`)와 겹칠 수 없습니다. |
| `join` | object | 예 | 아래 join 계약 |
| `join.left` | string | 예 | 왼쪽 source의 alias (`sources[].alias` 참조) |
| `join.right` | string | 예 | 오른쪽 source의 alias |
| `join.keys` | list | `left_key`/`right_key`와 택일 | 복합 join key. `{left, right}` 컬럼 쌍의 목록이며, 모든 쌍이 같을 때 행이 매칭됩니다(#698). |
| `join.left_key` | string | `keys`와 택일 | 왼쪽 테이블의 join key 컬럼명 (단일 쌍 축약형) |
| `join.right_key` | string | `keys`와 택일 | 오른쪽 테이블의 join key 컬럼명 (단일 쌍 축약형) |
| `join.type` | string | 아니오 | `inner`(기본) \| `left` |
| `join.on_duplicate_key` | string | 아니오 | `warn`(기본) \| `fail` |
| `join.cardinality` | string | 아니오 | `one_to_one` \| `one_to_many` \| `many_to_one` \| `many_to_many`. 생략하면 선언 없음(검증하지 않음) (#698) |
| `join.on_null_key` | string | 아니오 | `warn`(기본) \| `fail`. key 컬럼 중 하나라도 null인 행의 처리 (#698) |

`keys`와 `left_key`/`right_key`는 **정확히 하나만** 써야 합니다. 둘 다 쓰거나
둘 다 없으면 스펙 로드가 실패합니다. `left_key`/`right_key`는 `keys`에 쌍이 하나인
경우의 축약형이며, 기존 스펙은 그대로 동작합니다.

**alias 필수/중복 규칙**: `composition.join.left`/`right`가 참조하는 두
source는 반드시 비어 있지 않은 `alias`를 선언해야 하며, `composition`을
쓰는 BuildSpec에서는 선언된 모든 `sources[].alias` 값이 서로 달라야
합니다(빈 alias는 이 유일성 검사 대상이 아닙니다). 이 규칙은 `composition`이
없는 BuildSpec에는 적용되지 않습니다.

**join key 검증 시점**: alias 참조·`type`/`on_duplicate_key` 어휘 같은
구조적 문제는 `validate_spec`(구조 검증)이 스펙 파싱 직후 잡습니다. 하지만
join key 컬럼의 **존재 여부와 dtype 호환성**은 두 source가 각각 Silver
단계를 통과해야만 알 수 있으므로, 여기서는 검증할 수 없고 빌드 파이프라인의
런타임 게이트(orchestrator, Silver 완료 직후)에서 확인해 실패시킵니다. dtype은
완전히 동일해야 join이 허용됩니다 — 자동 캐스팅은 하지 않으며, 타입을
맞추려면 `sources[].schema.casts`(§4.4, #437)로 Silver 단계에서 명시적으로
정규화하세요.

**교집합 key 규칙(#698)**: cardinality와 중복 key는 **양쪽에 모두 나타나는
key(교집합)**만으로 판정합니다. 한쪽에만 있는 key는 몇 번 반복되든 결과 행을
곱할 수 없기 때문입니다. 예를 들어 왼쪽 key가 `A, A`, 오른쪽이 `B, B`이면 양쪽
모두 non-unique이지만 교집합이 없으므로 위반도 경고도 아닙니다. 복합 key는
컬럼 튜플 단위로 판정합니다 — 각 컬럼만 보면 중복이어도 쌍이 유일하면 유일한
key입니다.

**cardinality 검증**: 교집합 key마다 양쪽 행 수를 세어, 어떤 key든 왼쪽에서
반복되면 왼쪽이 "many", 오른쪽에서 반복되면 오른쪽이 "many"인
`observed_cardinality`를 얻습니다(왼쪽→오른쪽으로 읽습니다: `many_to_one`은
왼쪽은 반복 가능, 오른쪽은 유일). 선언된 `cardinality`가 관측값을 허용하지
않으면 composition이 실패하고, 오류 메시지에 선언값·관측값과 가장 많은 행을
곱하는 key 하나의 좌우 행 수가 담깁니다. "many"는 반복을 허용할 뿐 요구하지
않습니다 — `many_to_many`는 모든 관측값을, `one_to_one`은 `one_to_one`만
허용합니다.

**duplicate-key row explosion guard**: 교집합 key 중 하나라도 **양쪽 모두에서**
반복되면(many-to-many) 결과 행이 곱셈으로 폭증하므로 `duplicate_key_warning`이
켜집니다. 기본값 `on_duplicate_key: warn`은 결과를 만들고 manifest에 경고와
함께 정확한 행 수/distinct key 수를 남기며, `fail`은 Gold 진입 전에
composition을 실패 처리합니다. #698 이전에는 양쪽이 각각 non-unique이기만
하면(교집합이 없어도) 경고했습니다.

**null key**: key 컬럼 중 하나라도 null인 행은 어떤 행과도 매칭되지 않으므로
inner join에서 조용히 빠집니다. 이 행 수를 `left_null_key_rows`/
`right_null_key_rows`로 manifest에 기록하고 경고 로그를 남기며(`on_null_key:
warn`), `on_null_key: fail`이면 composition을 실패시킵니다.

**참조된 source가 실패한 경우**: `composition.join`이 참조하는 source 중
하나라도 Bronze/Silver를 통과하지 못하면 join을 시도하지 않고
`skipped`로 기록합니다(다른 source의 독립 Gold는 영향받지 않습니다).

**provenance**: manifest의 `composition` 필드(`CompositionProvenance`)에
join 조건(source/key/type)과 좌우 원본 행 수·distinct key 수·결과 행 수가
그대로 기록되어 추적 가능합니다. #698부터는 `keys`, 선언된 `cardinality`,
`observed_cardinality`, `left_unmatched_ratio`/`right_unmatched_ratio`(상대편에
매칭되는 key가 없는 행 — null key 행 포함 — 의 비율, 빈 쪽은 0.0),
`expansion_ratio`(결과 행 수 / 왼쪽 행 수, 왼쪽이 비면 null),
`left_null_key_rows`/`right_null_key_rows`도 기록합니다. `POST /build` 응답에도 `outcomes`(소스별)와
별도로 `composition`(결합 결과) 키가 노출됩니다.

## 5. 검증 규칙

Builder는 최소한 다음 규칙을 검증해야 합니다.

1. `dataset_id`는 비어 있지 않은 문자열이어야 합니다.
2. `title`은 비어 있지 않은 문자열이어야 합니다.
3. `description`은 비어 있지 않은 문자열이어야 합니다.
4. `sources`는 1개 이상 정의되어야 합니다.
5. 각 `sources[].provider`는 비어 있지 않아야 합니다.
6. 각 `sources[].dataset`은 비어 있지 않아야 합니다.
7. `exports`는 1개 이상 정의되어야 합니다.
8. 각 `exports[].kind`는 비어 있지 않아야 합니다.
9. 각 `exports[].output_path`는 비어 있지 않아야 합니다.
10. 지원하지 않는 필드 조합은 검증 단계에서 실패해야 합니다.

## 6. 버전 호환성

현재 BuildSpec 계약은 단일 버전(`schema_version` 필드 없음)으로 운영됩니다.

호환성 원칙:

- minor 문서 확장은 가능한 한 backward compatible하게 추가합니다.
- breaking change는 신규 계약 버전으로만 도입합니다.
- Studio는 Builder가 지원하는 버전만 전송해야 하며 임의 해석을 추가하면 안 됩니다.

## 7. 계약 원칙 요약

- BuildSpec은 Builder가 소유합니다.
- BuildSpec은 실행 계획이지 UI 상태 저장 포맷이 아닙니다.
- exports/publish는 source 정의를 대체하지 않습니다.
- manifest는 BuildSpec의 결과 기록물이지 입력이 아닙니다.
- 검증된 실행 입력은 run workspace의 `buildspec.yaml`에 canonical YAML로 저장됩니다.
  이 snapshot은 결정적 직렬화와 `sha256:<hex>` digest의 기준이며, source pipeline이
  부분 실패해도 남습니다. 검증 실패 입력은 snapshot으로 기록하지 않습니다.
- `metadata`, `sources[].params`, `exports[].options` 안의 명시적 credential 키 값은
  snapshot에서 `<redacted>`로 대체됩니다. 따라서 inline credential이 있던 snapshot은
  감사·비교용이며, 재실행 시 credential을 환경 또는 서비스 설정에서 다시 공급해야 합니다.
- `spec_digest`는 **저장된 redaction 후 canonical `buildspec.yaml` bytes**의 SHA-256입니다.
  따라서 credential 값만 다르고 나머지 spec이 같으면 두 snapshot과 digest도 같을 수 있으며,
  이는 secret을 digest로 식별하거나 노출하지 않기 위한 의도된 보안 정책입니다.

## 8. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | BuildSpec 중심 설계 |
| [API_CONTRACT.md](./API_CONTRACT.md) | BuildSpec을 받는 서비스 계약 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
| [ALGORITHM.md](./ALGORITHM.md) | 전체 빌드 알고리즘 명세 |

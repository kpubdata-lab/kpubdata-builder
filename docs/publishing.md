# 데이터셋 퍼블리싱 가이드

이 문서는 `scripts/publish_to_hf.py` 스크립트를 사용하여 공공데이터를 HuggingFace Hub 및 Kaggle에 퍼블리싱하는 전체 과정을 설명합니다.

---

## 개요

`publish_to_hf.py`는 config-driven 방식의 end-to-end 퍼블리싱 스크립트입니다. 하나의 YAML config 파일로 **데이터 수집 → 변환 → Parquet 저장 → Dataset Card 생성 → HuggingFace 업로드**까지 처리합니다.

이 스크립트는 향후 Builder의 Medallion Architecture(Bronze/Silver/Gold/Exporter/Publisher) 모듈로 분해될 레퍼런스 구현입니다.

```text
[YAML Config] → fetch → transform → write_parquet → generate_card → upload_to_hf / upload_to_kaggle
                  │          │            │                │              │
               Bronze     Silver        Gold           Export         Publish (HF + Kaggle)
```

---

## 사전 준비

### 1. 환경변수 설정

`~/.zshrc` (또는 `~/.bashrc`)에 다음 환경변수를 추가합니다:

```bash
# 공공데이터포털 API 키 (https://www.data.go.kr 에서 발급)
export KPUBDATA_DATAGO_API_KEY="your-api-key"

# HuggingFace API 토큰 (https://huggingface.co/settings/tokens 에서 발급)
export HF_TOKEN="hf_..."

# Kaggle API 인증 (https://www.kaggle.com/settings → API → Create New Token)
export KAGGLE_USERNAME="your-username"
export KAGGLE_KEY="your-api-key"
```

설정 후 반영:

```bash
source ~/.zshrc
```

> **Kaggle 대체 인증 방법**: `~/.kaggle/kaggle.json` 파일에 `{"username": "...", "key": "..."}` 형태로 저장해도 됩니다. 환경변수가 우선합니다.

### 2. HuggingFace 토큰 발급

1. https://huggingface.co/settings/tokens 접속
2. **Create new Access Token** 클릭
3. 설정:
   - **Token type**: Fine-grained
   - **Token name**: `kpubdata-publish` (또는 원하는 이름)
   - **User permissions > Repositories**: ✅ Write access to contents/settings
   - **Org permissions**: 대상 org 선택 후 ✅ Write access to contents/settings
4. 나머지 권한은 체크 해제
5. 토큰 생성 후 `HF_TOKEN` 환경변수에 저장

### 3. HuggingFace Organization (선택)

org 단위로 데이터셋을 관리하려면:

1. https://huggingface.co/organizations/new 에서 org 생성
2. 토큰 발급 시 해당 org에 대한 write 권한 부여

### 4. Kaggle API 토큰 발급

1. https://www.kaggle.com/settings 접속
2. **API** 섹션에서 **Create New Token** 클릭
3. 다운로드된 `kaggle.json`에서 `username`과 `key`를 환경변수에 설정
4. 주의: 새 토큰을 발급하면 이전 토큰은 **즉시 폐기**됨

> Kaggle은 organization을 지원하지 않습니다. 모든 데이터셋은 **개인 계정** 소속입니다.

### 5. 의존성 설치

```bash
cd kpubdata-builder
uv sync --extra publish
```

`publish` extra에는 `polars`, `huggingface-hub`, `kaggle`이 포함됩니다.

---

## 사용법

### 기본 실행 (HuggingFace + Kaggle 모두 업로드)

```bash
uv run python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml
```

### HuggingFace만 업로드

```bash
uv run python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml --target hf
```

### Kaggle만 업로드

```bash
uv run python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml --target kaggle
```

### 로컬에서만 파일 생성 (업로드 안 함)

```bash
uv run python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml --local-only
```

### 드라이런 (업로드 시뮬레이션)

```bash
uv run python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml --dry-run
```

### 디버그 로깅

```bash
uv run python scripts/publish_to_hf.py scripts/configs/seoul_apartment_trades.yaml -v
```

### CLI 옵션 요약

| 옵션 | 설명 |
| :--- | :--- |
| `config` (필수) | YAML config 파일 경로 |
| `--target` | 업로드 대상: `hf` (HuggingFace), `kaggle`, `all` (기본값: `all`) |
| `--dry-run` | 업로드를 건너뛰고 로컬 파일만 생성 (업로드 시뮬레이션 로그 출력) |
| `--local-only` | 로컬 파일 생성까지만 실행 (업로드 로직 자체를 건너뜀) |
| `--verbose`, `-v` | DEBUG 레벨 로깅 활성화 |
| `--confirm-non-commercial` | 비상업 조건(KOGL 제2유형) 데이터셋을 비공개 Kaggle 로만 게시하겠다고 확인 (#688) |

### 재배포 게이트 (#688)

`--local-only` 가 아니면, 무엇이든 가져오기 **전에** config 의 `card.license`/`license_name` 으로
재배포 가능 여부를 판정하고 막을 것은 종료 코드 2 로 막는다.

| 판정 | 조건 | 게시 |
| :--- | :--- | :--- |
| `allowed` | `korea-public-data-unrestricted`, `kogl-type-1` | 허용 |
| `non_commercial` | `kogl-type-2` | 비공개 Kaggle 에만, `--confirm-non-commercial` 과 함께. HF 업로드는 항상 공개라 거부 |
| `forbidden` | `kogl-type-3`, `kogl-type-4` — 이 경로는 가공 데이터를 게시하므로 변경금지 조건과 충돌 | 거부 |
| `unknown` | 그 밖 전부 — 조건을 적지 않았거나(`license` 없음), 확인하지 않은 `cc-by-4.0` 등 | **거부** (모름은 허락이 아니다) |

이 게이트는 마지막 config 가 BuildSpec 으로 옮겨질 때 레거시 코드와 함께 사라진다(ADR 0018).

### BuildSpec 경로의 재배포 게이트 (#688)

BuildSpec 으로 만든 run 은 **kpubdata 가 데이터셋마다 선언한 조건**
(`DatasetRef.license.redistribution`, kpubdata 0.8)으로 판정한다. 아무것도 선언하지 않은 데이터셋,
카탈로그에 없는 데이터셋, file·url 소스는 모두 `unknown` 이다. 한 run 의 판정은 가장 제한적인 소스의
판정이다 (`forbidden` > `unknown` > `non_commercial` > `allowed`).

| 판정 | 게시 | 질의·미리보기·다운로드 |
| :--- | :--- | :--- |
| `allowed` | 허용 (BuildSpec `license` 선언은 여전히 필요, #443) | 허용 |
| `non_commercial` | `confirm_non_commercial: true` 필요. 공개 게시에는 데이터셋 licence 에 비영리 표시(`cc-by-nc-4.0` 등)도 필요 | 허용 |
| `unknown` | **비공개만** — 공개 게시는 `redistribution_unknown` 으로 거부 | 허용 |
| `forbidden` | 전부 거부 (`redistribution_forbidden`) | **거부** — `/query`, `/preview`, warehouse 프로파일·질의·rows·집계·export·export 다운로드, 분석 생성·실행, artifact 다운로드가 403, stage 상세는 sample 을 빼고 준다 |

- HTTP: `GET /builds/{run_id}/publish/readiness` 가 `redistribution`(판정과 소스별 이유)을 돌려주고, 막힌 `POST .../publish` 도 같은 값을 준다. 판정은 target 의 기본 옵션(비공개)으로 계산하고, POST 가 실제 옵션으로 다시 확인한다.
- **비공개로만 허용되는 게시**(`unknown`, `non_commercial`)는 게시 전에 대상의 현재 공개 여부를 호출자 자격증명으로 읽는다. 이미 있는 저장소에 게시해도 공개 여부는 바뀌지 않으므로, 이미 공개인 Hugging Face repo·Kaggle dataset 이면 `destination_public` 으로 막고, 공개 여부를 읽지 못하면 `destination_visibility_unknown` 으로 막는다(모르는 것은 허가가 아니다). 없는 대상(새로 만들 것)과 비공개 대상은 통과한다. CLI 도 같다.
- 성공한 게시는 응답과 receipt 의 `redistribution` 에 판정, 판정을 읽은 kpubdata 버전(`kpubdata_version`), `confirm_non_commercial` 을 남긴다. `confirm_non_commercial` 은 Builder 의 확인이라 publisher 로 넘기지 않는다.
- CLI: `kpubdata-builder publish` 에도 같은 게이트가 있다. 막히면 종료 코드 2, 비영리 확인은 `--confirm-non-commercial`.
- 2026-09-30 현재 kpubdata 카탈로그의 어떤 데이터셋도 `redistribution` 을 선언하지 않았으므로, BuildSpec 경로의 **공개 게시는 모두 막힌다**. 조건을 정하는 일은 kpubdata#524 다.
- 선언된 PII(#689, #900)는 같은 출구(`/query`, `/preview`, stage 상세, artifact 다운로드)에서 약관 판정 **다음에** 확인한다. 약관이 `forbidden` 이 아니면 Silver·Bronze 를 읽는 경로가 선언 컬럼을 Gold 처럼 가리거나(질의·미리보기·sample), 원본 파일이면 403 `declared_pii_withheld` 로 거부한다. kpubdata 선언을 읽지 못하면 가릴 컬럼을 모르므로 503 `pii_declaration_unavailable` 로 거부한다(모르는 것은 허가가 아니다). 결정과 이유는 [API_CONTRACT.md](API_CONTRACT.md#declared-pii-in-silver-and-bronze-reads-900) 의 "Declared PII in Silver and Bronze Reads".

### 데이터셋 카드 (#694)

BuildSpec 경로의 Gold 출력마다 카드(`README.md`)와 같은 내용의 `card.json` 이 생긴다. 카드는
기록된 것에서만 채운다.

| 섹션 | 출처 |
| :--- | :--- |
| 출처표시 (`Attribution:`) | BuildSpec `attribution`, 없으면 kpubdata 가 선언한 출처표시 문구(kpubdata#617). 기관명이 아니라 라이선스의 출처표시 문장일 수 있어 "Attribution" 으로 적는다 |
| 출처 URL | kpubdata 카탈로그의 `source_url` / url 소스의 endpoint(query 제거) / file 소스는 "uploaded file" (내부 업로드 id 는 적지 않는다) |
| 라이선스 | BuildSpec `license_name`·`license`(`license_link`) **원래 이름 그대로**. kpubdata 가 제공기관 조건을 선언했으면 옆에 적는다 |
| 수집 일시 | provenance 의 `fetched_at` |
| 가공 | 선언된 read_as·null_tokens·coalesce·rename·zfill·casts·derived·gold selection·splits. 없으면 "없음" 을 적는다 |
| 개인정보 처리 | Gold 가 선언된 PII 열을 어떻게 했는지(manifest `pii_masking`: 마스킹한 열과 방식, `publish_unmasked` 로 마스킹 없이 게시한 열, kpubdata 가 선언했지만 이 소스에 없는 필드) + `pii` 정책의 스캔 방식(mode 그대로 — `allow` 는 스캔하되 조치하지 않는다는 뜻이다). 정책이 없으면 "스캔하지 않음" 을 적는다 |

`card.json` 은 계약의 `DatasetCard` 스키마다(#955, API contract 1.75.0). 문장 안에만 있던 두 사실은 필드로도 적는다 — 출처별 `license_declared`·`license_provider`(모르면 null)·`license_mismatch`(둘 다 알고 제공기관 조건이 선언 라이선스와 다를 때 true), 그리고 `processing_declared`(선언된 가공이 없어 "No transformation declared" 문장만 있으면 false). 클라이언트는 문장이 아니라 이 필드를 읽는다.

필수 섹션이 비면 게시가 막힌다(`card_incomplete`, 빈 섹션 이름을 알려준다). 카드가 없던 이전 run 은
`card_missing` 으로 막히며 다시 빌드하면 된다. Kaggle 패키지와 Hugging Face layout 에도 같은 카드가 들어간다 — HF 게시는 layout 의 `README.md` 를 repo 루트에 올리므로, 그 파일도 exporter 의 front matter 아래에 같은 절을 갖는다.

---

## Config YAML 스키마

Config 파일은 4개의 최상위 섹션으로 구성됩니다.

### `source` — 데이터 수집 설정

```yaml
source:
  provider: datago          # kpubdata provider 키
  dataset: apt_trade        # dataset 키
  list_all: true            # true면 자동 페이지네이션
  fetch_params:             # 각 항목이 하나의 API 호출
    - LAWD_CD: "11680"
      DEAL_YMD: "202401"
    - LAWD_CD: "11650"
      DEAL_YMD: "202401"
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `provider` | str | ✅ | `kpubdata`의 provider 이름 |
| `dataset` | str | ✅ | `kpubdata`의 dataset 이름 |
| `list_all` | bool | | `true`면 `ds.list_all()` 사용 (자동 페이지네이션) |
| `fetch_params` | list[dict] | ✅ | API 호출 파라미터 목록. 각 항목이 별도 호출, 결과는 합산 |

### `transform` — 데이터 변환 설정

```yaml
transform:
  column_mapping:
    sggCd: district_code     # raw 필드명 → clean 컬럼명
    dealAmount: deal_amount_10k_krw

  dtypes:
    district_code: str
    deal_amount_10k_krw: int_comma    # "82,500" → 82500

  derived:
    - name: deal_date
      expr: "concat_date(deal_year, deal_month, deal_day)"
      dtype: str

  filters:
    - "deal_amount_10k_krw > 0"
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `column_mapping` | dict[str, str] | ✅ | raw API 필드 → clean 컬럼 이름 매핑. 매핑되지 않은 컬럼은 제거됨 |
| `dtypes` | dict[str, str] | | 타입 캐스팅. 지원: `int`, `float`, `str`, `int_comma` |
| `derived` | list[dict] | | 파생 컬럼 정의 |
| `filters` | list[str] | | 행 필터 표현식 |

#### 지원하는 타입 (`dtypes`)

| 타입 | 설명 | 예시 |
| :--- | :--- | :--- |
| `str` | 문자열 변환 | `"11680"` → `"11680"` |
| `int` | 정수 변환 (null-safe) | `"2024"` → `2024`, `"-"` → `null` |
| `float` | 실수 변환 (null-safe) | `"84.5"` → `84.5`, `""` → `null` |
| `int_comma` | 콤마 제거 후 정수 변환 | `"82,500"` → `82500` |

null-safe 처리: 빈 문자열(`""`), `"-"`, `"N/A"`, `"null"`, `"None"`은 자동으로 `null`로 변환됩니다.

#### 파생 컬럼 (`derived`)

| 표현식 | 설명 | 예시 |
| :--- | :--- | :--- |
| `concat_date(y, m, d)` | 날짜 문자열 조합 (zero-padded) | `concat_date(deal_year, deal_month, deal_day)` → `"2024-01-15"` |
| `format(fmt, col1, col2, ...)` | Polars `pl.format()` 사용 | `format("{}-{}", year, month)` → `"2024-01"` |

#### 필터 표현식 (`filters`)

`"컬럼명 연산자 값"` 형태의 문자열. 지원 연산자: `>`, `<`, `>=`, `<=`, `==`, `!=`

```yaml
filters:
  - "deal_amount_10k_krw > 0"
  - "floor >= 1"
```

모든 필터를 동시에 만족하는 행만 유지됩니다 (AND 조건).

### `output` — 출력 설정

```yaml
output:
  hf_repo: "kpubdata/seoul-apartment-trades"
  kaggle_slug: "yschoe/seoul-apartment-trades"
  parquet_filename: "data/train.parquet"
  staging_dir: "./staging/seoul-apartment-trades"
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `hf_repo` | str | ✅ | HuggingFace 레포 ID (`org/dataset-name`) |
| `kaggle_slug` | str | | Kaggle 데이터셋 슬러그 (`username/dataset-name`). 없으면 Kaggle 업로드 건너뜀 |
| `parquet_filename` | str | ✅ | staging 내 parquet 파일 경로 |
| `staging_dir` | str | ✅ | 로컬 staging 디렉토리 (스크립트 실행 위치 기준 상대경로) |

### `card` — Dataset Card 설정

```yaml
card:
  title: "Korean Apartment Trades (아파트매매 실거래가)"
  description: |
    Real transaction prices for apartment sales...
  license: "cc-by-4.0"
  language:
    - ko
  tags:
    - real-estate
    - tabular
  features:
    - name: district_code
      description: "시군구 코드 (5-digit administrative district code)"
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `title` | str | ✅ | 데이터셋 제목 |
| `description` | str | ✅ | 데이터셋 설명 |
| `license` | str | | 라이선스 (기본값: `cc-by-4.0`) |
| `language` | list[str] | | 언어 코드 (기본값: `["ko"]`) |
| `tags` | list[str] | | HuggingFace 태그 |
| `features` | list[dict] | | 피처별 이름과 설명. Dataset Card의 Features 테이블에 렌더링됨 |

---

## 스크립트 내부 구조

스크립트의 각 함수는 Builder의 Medallion Architecture stage에 1:1 대응됩니다.

```text
함수                          → Builder Stage      설명
──────────────────────────────────────────────────────────────
load_config()                 → (설정)            YAML config 로드 및 검증
fetch_records()               → Bronze            kpubdata Client로 raw 데이터 수집
transform_records()           → Silver            Polars 기반 컬럼 매핑, 타입 변환, 필터링
  ├── _cast_column()                              null-safe 타입 캐스팅
  ├── _nullify_tokens()                           빈값/"-"/"N/A" → null 변환
  ├── _add_derived_column()                       파생 컬럼 생성
  └── _apply_filter()                             비교 필터 적용
write_parquet()               → Gold              Parquet 패키징
generate_dataset_card()       → Export            HF Dataset Card (README.md) 생성
  ├── _build_features_table()                     피처 설명 마크다운 테이블
  ├── _build_sample_table()                       샘플 데이터 테이블
  └── _build_stats_section()                      수치 컬럼 통계
upload_to_hf()                → Publish           whitelist 기반 HF Hub 업로드
upload_to_kaggle()            → Publish           dataset-metadata.json 생성 + Kaggle API 업로드
  └── _map_kaggle_license()                       HF→Kaggle 라이선스 매핑
main()                        → CLI               argparse 기반 진입점 (--target hf|kaggle|all)
```

### 업로드 보안

`upload_to_hf()`는 staging 디렉토리 전체를 업로드하지 않습니다. 임시 `.hf_upload/` 디렉토리에 다음 파일만 복사 후 업로드합니다:

- `README.md` (dataset card)
- `data/*.parquet` (데이터 파일)

업로드 완료 후 `.hf_upload/` 디렉토리는 자동 삭제됩니다.

---

## 제공되는 Config 파일

### `seoul_apartment_trades.yaml`

국토교통부 아파트매매 실거래가 데이터. 서울 25개구 전체, 2020-01~2024-12 (60개월). 거래가격(`deal_amount_10k_krw`)을 타겟 변수로 하는 tabular regression / time-series 벤치마크.

- **소스**: data.go.kr MOLIT 아파트 실거래가 API
- **지역**: 서울 25개구 전체
- **기간**: 2020년 1월 ~ 2024년 12월 (60개월)
- **API 호출**: 1,500회 (25개구 × 60개월)
- **예상 레코드**: ~250,000건
- **HF 레포**: `kpubdata/seoul-apartment-trades`

### `korea_base_rate.yaml`

한국은행 기준금리 데이터. config-driven 재사용성 검증용.

- **소스**: BOK 기준금리 API
- **HF 레포**: `kpubdata/korea-base-rate`

---

## 새 데이터셋 추가하기

1. `scripts/configs/` 에 새 YAML config 파일 생성
2. 위 스키마에 맞춰 `source`, `transform`, `output`, `card` 섹션 작성
3. `--local-only`로 먼저 로컬 테스트:
   ```bash
   uv run python scripts/publish_to_hf.py scripts/configs/my_new_dataset.yaml --local-only -v
   ```
4. staging 디렉토리에서 parquet과 README.md 확인
5. 문제없으면 실행:
   ```bash
   uv run python scripts/publish_to_hf.py scripts/configs/my_new_dataset.yaml
   ```

### Config 작성 팁

- `column_mapping`에 포함되지 않은 raw 필드는 자동으로 제거됩니다.
- `fetch_params`의 각 항목은 별도의 API 호출이 됩니다. 여러 지역/기간을 조합하려면 항목을 추가하세요.
- `list_all: true`를 사용하면 페이지네이션을 자동으로 처리합니다.
- `int_comma` 타입은 `"82,500"` 같은 콤마가 포함된 숫자 문자열을 처리합니다.
- 파생 컬럼은 타입 캐스팅 이후에 계산되므로, 참조하는 컬럼이 올바른 타입인지 확인하세요.

---

## 트러블슈팅

### `No records fetched` 에러

- `KPUBDATA_DATAGO_API_KEY` 환경변수가 올바르게 설정되었는지 확인
- `fetch_params`의 파라미터 값이 API 스펙에 맞는지 확인 (예: `LAWD_CD`는 5자리 시군구 코드)

### `huggingface_hub not installed` 에러

```bash
uv sync --extra publish
```

### HF 업로드 401/403 에러

- `HF_TOKEN` 환경변수 확인
- 토큰에 대상 org/repo에 대한 write 권한이 있는지 확인
- Fine-grained 토큰의 경우 Org permissions에서 대상 org 선택 필요

### Polars 타입 캐스팅 실패

- 원본 데이터에 빈 문자열, `"-"`, `"N/A"` 등이 포함되어 있을 수 있습니다 → `int`/`float` 타입은 자동으로 null-safe 처리됩니다
- 콤마가 포함된 숫자 필드는 `int_comma` 타입을 사용하세요

### Kaggle 업로드 401 에러

- `KAGGLE_USERNAME`과 `KAGGLE_KEY` 환경변수 확인
- 또는 `~/.kaggle/kaggle.json` 파일 존재 여부 확인
- Kaggle 토큰을 재발급하면 **이전 토큰이 즉시 폐기**됨. 새 토큰으로 환경변수/파일 모두 갱신 필요

### Kaggle `dataset_view` AttributeError

- kaggle SDK 1.6+ 에서 `dataset_view()` 메서드가 제거됨
- 스크립트는 `dataset_list(mine=True, search=slug)` 방식으로 데이터셋 존재 여부를 판별

### Kaggle organization 미지원

- Kaggle은 HuggingFace와 달리 organization 계정을 지원하지 않음
- 모든 데이터셋은 개인 계정 소속 (`username/dataset-name`)
- `kaggle_slug`은 `hf_repo`와 다른 namespace를 가질 수 있음

---

## Builder 모듈 분해 가이드

이 스크립트는 학생들이 Builder의 Medallion Architecture 모듈로 분해하는 레퍼런스입니다.

| 스크립트 함수 | 분해 대상 모듈 | 디렉토리 |
| :--- | :--- | :--- |
| `fetch_records()` | Bronze stage | `src/kpubdata_builder/stages/bronze/` |
| `transform_records()` | Silver stage (Polars engine) | `src/kpubdata_builder/stages/silver/`, `tabular/` |
| `write_parquet()` | Gold stage | `src/kpubdata_builder/stages/gold/` |
| `generate_dataset_card()` | HF Layout Exporter | `src/kpubdata_builder/exporters/` |
| `upload_to_hf()` | HF Publisher | `src/kpubdata_builder/publishers/` |
| `upload_to_kaggle()` | Kaggle Publisher | `src/kpubdata_builder/publishers/` |
| Config YAML | BuildSpec model | `src/kpubdata_builder/spec.py` |

참고 이슈: #50, #8, #9, #10, #28, #37, #40

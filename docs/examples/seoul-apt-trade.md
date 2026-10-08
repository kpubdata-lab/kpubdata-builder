# 서울 아파트 실거래가 종단 간 예제

이 문서는 `kpubdata`의 `datago.apt_trade` 데이터를 사용해 서울 아파트 실거래가를 수집하고, Polars로 정제한 뒤, Hugging Face Dataset 형태의 로컬 산출물로 패키징하는 end-to-end 예제입니다. **레거시 publish 스크립트**([ADR 0018](../adrs/0018-legacy-publish-pipeline.md))를 쓰는 예제라 Polars 를 쓰며, Builder 본체는 DuckDB 만 쓴다([ADR 0021](../adrs/0021-duckdb-tabular-engine.md)).

현재 예제는 선행 PR #52에서 추가된 config-driven publishing script를 재사용합니다. 새 YAML을 만들지 않고 `scripts/configs/korean_apartment_trades.yaml`을 기준 config로 사용합니다.

## 예제 범위

`scripts/configs/korean_apartment_trades.yaml`은 다음 범위의 아파트매매 실거래가를 수집합니다.

| 항목 | 값 |
| :--- | :--- |
| Provider | `datago` |
| Dataset | `apt_trade` |
| 지역 | 강남구 `11680`, 서초구 `11650` |
| 기간 | `202401`부터 `202406`까지 |
| API 호출 수 | 12개 `fetch_params` |
| 출력 저장소명 | `kpubdata/korean-apartment-trades` |
| 로컬 출력 경로 | `./staging/korean-apartment-trades` |

전체 서울 25개 구와 장기 기간을 대상으로 하는 대용량 config가 필요하면 `scripts/configs/seoul_apartment_trades.yaml`을 참고할 수 있습니다. 다만 이 문서의 beginner 예제는 비용과 실행 시간을 줄이기 위해 더 작은 `korean_apartment_trades.yaml`을 기준으로 설명합니다.

## 설치

기본 개발 의존성과 publish 예제에 필요한 optional dependency를 설치합니다.

```bash
uv sync --extra dev --extra publish --extra docs
```

패키지로 설치해 실행하는 경우에는 다음 extra가 필요합니다.

```bash
pip install "kpubdata-builder[publish,legacy-publish]"
```

`publish` extra에는 `huggingface-hub`, `xmltodict`, `kaggle`이 포함됩니다. 이 예제의 로컬 파일 생성에는 Polars(`legacy-publish` extra)와 Parquet 작성 의존성이 필요하고, 실제 Hugging Face 업로드를 할 때만 Hugging Face 인증이 필요합니다. 한국어 건물명 로마자 변환이 필요하면 `kr-building-name-normalizer`를 별도로 설치하세요.

## 사전 준비

`datago.apt_trade`는 data.go.kr 공공데이터 API를 호출합니다. 실제 데이터를 가져오려면 data.go.kr API key가 환경 변수에 설정되어 있어야 합니다.

```bash
export KPUBDATA_DATAGO_API_KEY="your-data-go-kr-api-key"
```

이 문서와 PR에서는 Hugging Face 실제 업로드를 수행하지 않습니다. `HF_TOKEN`이나 API key 값을 문서, config, 코드에 저장하지 마세요.

외부 API 호출은 네트워크 상태, data.go.kr 응답 지연, API quota에 영향을 받을 수 있습니다. `korean_apartment_trades.yaml`은 12개 파라미터 세트라 비교적 작지만, `list_all: true`로 페이지를 모두 순회하므로 실행 시간이 달라질 수 있습니다.

## 실행 명령

로컬 산출물만 생성하려면 `--local-only`를 사용합니다. 이 모드는 Parquet 파일과 dataset card를 만들고 업로드 단계 전에 종료합니다.

```bash
uv run python scripts/publish_to_hf.py scripts/configs/korean_apartment_trades.yaml --local-only
```

업로드 직전 단계까지의 흐름을 확인하되 실제 업로드를 건너뛰려면 `--dry-run`을 사용합니다.

```bash
uv run python scripts/publish_to_hf.py scripts/configs/korean_apartment_trades.yaml --dry-run
```

`--dry-run`은 fetch, transform, package, dataset card 생성까지 수행한 뒤 Hugging Face와 Kaggle 업로드만 건너뜁니다. `--local-only`가 가장 안전한 beginner 실행 모드입니다.

실행 로그를 더 자세히 보려면 `--verbose`를 추가합니다.

```bash
uv run python scripts/publish_to_hf.py scripts/configs/korean_apartment_trades.yaml --local-only --verbose
```

## 파이프라인 매핑

현재 스크립트는 Builder의 장기 모듈 구조를 설명하기 위한 config-driven 레퍼런스입니다. Issue #50의 Bronze/Silver/Gold 흐름은 다음처럼 매핑됩니다.

| 단계 | 스크립트 동작 | 관련 파일 |
| :--- | :--- | :--- |
| Bronze | `kpubdata` client로 `datago.apt_trade`를 fetch하고 checkpoint를 남김 | `scripts/pipeline/fetch.py` |
| Silver | `column_mapping`, `dtypes`, `derived`, `filters`를 Polars DataFrame에 적용하고 schema를 검증 | `scripts/pipeline/transform.py` |
| Gold | 정제된 DataFrame을 `data/train.parquet`로 패키징 | `scripts/pipeline/package.py` |
| Export | Hugging Face Dataset card 형식의 `README.md` 생성 | `scripts/pipeline/package.py` |
| Publish | `--local-only`가 아니면 HF/Kaggle upload 함수 호출, `--dry-run`이면 업로드 생략 | `scripts/pipeline/publish.py` |

## Config 구조

`scripts/configs/korean_apartment_trades.yaml`은 크게 네 부분으로 나뉩니다.

| Key | 역할 |
| :--- | :--- |
| `source` | `provider`, `dataset`, `list_all`, `fetch_params`를 선언합니다. |
| `transform` | 원천 필드명을 영어 컬럼명으로 바꾸고, 타입 캐스팅과 파생 컬럼, row filter를 정의합니다. |
| `output` | HF repo 이름, Parquet 파일명, 로컬 staging directory를 정의합니다. |
| `card` | dataset card의 제목, 설명, license, language, tags, feature 설명을 정의합니다. |

대표 변환은 다음과 같습니다.

| 원천 필드 | 출력 컬럼 |
| :--- | :--- |
| `sggCd` | `district_code` |
| `umdNm` | `neighborhood` |
| `aptNm` | `apartment_name` |
| `excluUseAr` | `exclusive_area_m2` |
| `dealAmount` | `deal_amount_10k_krw` |

`deal_year`, `deal_month`, `deal_day`는 `concat_date(deal_year, deal_month, deal_day)`로 `deal_date` 파생 컬럼을 만듭니다. `deal_amount_10k_krw > 0` filter도 적용됩니다.

## 생성되는 파일 구조

`--local-only` 또는 `--dry-run` 실행이 성공하면 기본적으로 다음 구조가 생성됩니다.

```text
staging/korean-apartment-trades/
├── README.md
└── data/
    └── train.parquet
```

`README.md`는 Hugging Face dataset card입니다. config의 `card` 섹션과 실제 DataFrame의 record count, feature count, numeric statistics, sample rows를 사용해 생성됩니다.

실행 중 checkpoint가 필요한 경우에는 다음 파일이 임시로 생길 수 있습니다.

```text
staging/korean-apartment-trades/.checkpoints/fetch_checkpoint.json
```

성공적으로 publish 흐름이 끝나면 checkpoint는 삭제됩니다. `--local-only`로 중간에 종료하는 경우 checkpoint가 남을 수 있으며, 이어받기가 필요하면 `--resume`을 사용할 수 있습니다.

## 업로드하지 않는 검증

문서 예제 검증은 다음 순서로 진행하는 것을 권장합니다.

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run mkdocs build
uv run python scripts/publish_to_hf.py scripts/configs/korean_apartment_trades.yaml --local-only
```

API key가 없거나 외부 API 호출이 실패하면 마지막 명령은 실패할 수 있습니다. 이 경우에도 `--help`로 CLI 옵션을 확인하고, 문서 빌드와 정적 검사를 먼저 통과시켜 문서 자체의 안전성을 확인할 수 있습니다.

```bash
uv run python scripts/publish_to_hf.py --help
```

## 주의 사항

- 실제 Hugging Face 업로드를 하려면 `HF_TOKEN`이 필요하지만, 이 예제에서는 업로드를 수행하지 않습니다.
- API key나 token은 코드, YAML, 문서, commit에 넣지 않습니다.
- `--dry-run`은 업로드만 생략하며 데이터 fetch와 로컬 파일 생성은 수행합니다.
- 대용량 config인 `scripts/configs/seoul_apartment_trades.yaml`은 호출 수가 많아 더 오래 걸리고 API quota를 더 많이 사용할 수 있습니다.

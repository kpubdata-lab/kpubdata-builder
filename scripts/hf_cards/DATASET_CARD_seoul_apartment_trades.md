---
license: cc-by-4.0
language:
- ko
- en
tags:
- real-estate
- housing-prices
- korea
- seoul
- tabular
- regression
- time-series
- apartment
- property
- government-data
- public-data
size_categories:
- 100K<n<1M
task_categories:
- tabular-regression
---

# Seoul Apartment Trades (서울 아파트 매매 실거래가)

Real apartment sale transaction records in Seoul, South Korea,
covering all 25 districts from January 2020 to December 2024 (60 months, ~234,000 records).

Sourced from the Ministry of Land, Infrastructure and Transport (MOLIT, 국토교통부)
via the [data.go.kr](https://www.data.go.kr/data/15126468/openapi.do) public API.

## Quick Start

```python
from datasets import load_dataset

ds = load_dataset("kpubdata/seoul-apartment-trades")
df = ds["train"].to_pandas()
print(df.head())
```

```python
# With Polars
import polars as pl

ds = load_dataset("kpubdata/seoul-apartment-trades")
df = pl.from_pandas(ds["train"].to_pandas())
```

## Schema

| Column | Type | Description |
|---|---|---|
| `district_code` | str | 5-digit administrative district code (시군구코드). See mapping table below. |
| `neighborhood` | str | Neighborhood name **in Korean** (법정동명, e.g. 역삼동, 개포동) |
| `apartment_name` | str | Apartment complex name **in Korean** (e.g. 래미안대치팰리스) |
| `exclusive_area_m2` | float | Exclusive use area in m² (전용면적) |
| `floor` | int | Floor number of the traded unit |
| `build_year` | int | Year the building was constructed |
| `deal_year` | int | Transaction year |
| `deal_month` | int | Transaction month |
| `deal_day` | int | Transaction day |
| `deal_amount_10k_krw` | int | **Transaction price** in units of 10,000 KRW (만원). Target variable. |
| `lot_number` | str | Land lot number (지번) |
| `deal_date` | str | Transaction date as YYYY-MM-DD (derived) |
| `registration_date` | str | Registration date at registry office (등기일자). May be null for older records. |
| `apartment_seq` | str | Unique apartment complex identifier (아파트 일련번호) |

### Important Notes on Values

- **Text columns are in Korean.** `neighborhood` and `apartment_name` contain original Korean text.
  This is intentional — romanization is lossy and inconsistent. Column names are in English for accessibility.
- **Price unit**: `deal_amount_10k_krw` is in 만원 (10,000 KRW). To get KRW: multiply by 10,000.
  Example: `80000` = 800,000,000 KRW = ~$600,000 USD.
- **Nullable fields**: `registration_date` may be null for older transactions where registry data is unavailable.

## Korean Real Estate Context (for International Users)

### Administrative Geography
Seoul is divided into **25 구 (gu, districts)**, each subdivided into **동 (dong, neighborhoods)**.
The `district_code` is a standardized 5-digit code used across Korean government systems.

### District Code Mapping

| Code | District (Korean) | District (English) |
|---|---|---|
| 11110 | 종로구 | Jongno-gu |
| 11140 | 중구 | Jung-gu |
| 11170 | 용산구 | Yongsan-gu |
| 11200 | 성동구 | Seongdong-gu |
| 11215 | 광진구 | Gwangjin-gu |
| 11230 | 동대문구 | Dongdaemun-gu |
| 11260 | 중랑구 | Jungnang-gu |
| 11290 | 성북구 | Seongbuk-gu |
| 11305 | 강북구 | Gangbuk-gu |
| 11320 | 도봉구 | Dobong-gu |
| 11350 | 노원구 | Nowon-gu |
| 11380 | 은평구 | Eunpyeong-gu |
| 11410 | 서대문구 | Seodaemun-gu |
| 11440 | 마포구 | Mapo-gu |
| 11470 | 양천구 | Yangcheon-gu |
| 11500 | 강서구 | Gangseo-gu |
| 11530 | 구로구 | Guro-gu |
| 11545 | 금천구 | Geumcheon-gu |
| 11560 | 영등포구 | Yeongdeungpo-gu |
| 11590 | 동작구 | Dongjak-gu |
| 11620 | 관악구 | Gwanak-gu |
| 11650 | 서초구 | Seocho-gu |
| 11680 | 강남구 | Gangnam-gu |
| 11710 | 송파구 | Songpa-gu |
| 11740 | 강동구 | Gangdong-gu |

### Price Context
- Korean real estate prices are quoted in 만원 (man-won, 10,000 KRW).
- Colloquially, Koreans use 억 (eok, 100,000,000 KRW = 10,000 만원).
- Example: "9억" = 900,000,000 KRW = `90000` in this dataset.

### Key Terms
| Korean | English | Meaning |
|---|---|---|
| 전용면적 | Exclusive area | Net usable floor area (excluding hallways, elevators) |
| 실거래가 | Real transaction price | Actual sale price reported to government (not asking price) |
| 매매 | Sale/Trade | Ownership transfer transaction |

## Coverage

- **Geography**: All 25 districts of Seoul
- **Period**: January 2020 – December 2024 (60 months)
- **Records**: ~234,000 transactions
- **Source API**: [data.go.kr #15126468](https://www.data.go.kr/data/15126468/openapi.do)

## Use Cases

- Tabular regression benchmark (predict `deal_amount_10k_krw`)
- Seoul housing market time-series analysis (COVID boom → rate correction → recovery)
- Urban analytics and spatial analysis (combine with geocoding APIs)
- Practice dataset for EDA / data visualization

## Limitations

- Text values are in Korean. Non-Korean users can still use numeric features for ML tasks.
- No geographic coordinates included. Users can geocode using the Korean address API ([juso.go.kr](https://juso.go.kr)).
- `registration_date` is sparse for older records.
- This is raw government transaction data, not a feature-engineered ML benchmark.

## Legal & Attribution

This dataset uses "아파트매매 실거래 상세 자료" (Apartment Trade Detailed Data)
published by the Ministry of Land, Infrastructure and Transport (국토교통부)
under Korea Open Government License Type 1 (공공누리 제1유형).

**Korean attribution (한국어 출처표시):**

> 본 저작물은 '국토교통부'에서 작성하여 공공누리 제1유형으로 개방한
> '아파트매매 실거래 상세 자료'를 이용하였으며, 해당 저작물은
> '국토교통부, https://www.data.go.kr/data/15126468/openapi.do'에서
> 무료로 다운받으실 수 있습니다.

**License:** [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/)
**공공누리 정보:** https://www.kogl.or.kr/info/licenseType1.do

## Part of the kpubdata Series

This dataset is part of the [kpubdata](https://huggingface.co/kpubdata) collection —
Korean public data made globally accessible.

Built with [kpubdata](https://github.com/kpubdata-lab/kpubdata) SDK
and [kpubdata-builder](https://github.com/kpubdata-lab/kpubdata-builder) pipeline.

# 데이터셋 퍼블리싱 시행착오 기록

이 문서는 `publish_to_hf.py` 스크립트로 HuggingFace 및 Kaggle에 데이터셋을 퍼블리싱하면서 발견한 시행착오를 기록한다. Builder의 Bronze/Silver/Gold/Publish 모듈 구현 시 동일한 실수를 방지하기 위한 참고 자료다.

---

## 1. HuggingFace 라이선스 값은 소문자만 허용

### 증상

`upload_folder()` 호출 시 `_validate_yaml()` 단계에서 거부됨.

### 원인

HuggingFace Hub는 YAML front matter의 `license` 필드를 **소문자만** 허용한다.

```yaml
# ❌ 거부됨
license: CC-BY-4.0

# ✅ 통과
license: cc-by-4.0
```

### Builder 적용 지점

- **Gold/Export 단계**: Dataset Card 생성 시 라이선스 값을 자동으로 소문자 변환
- **BuildSpec 검증**: spec 로드 시점에 라이선스 값이 소문자인지 early validation

---

## 2. Polars mixed-type 추론 실패

### 증상

```
polars.exceptions.ComputeError: could not append value: "23.03.16" of type: str
to the builder; make sure that all rows have the same schema or consider
increasing `infer_schema_length`
```

### 원인

공공데이터 API 응답의 동일 필드가 호출마다 다른 타입을 반환한다. 예를 들어 `rgstDate`(등기일자)가 어떤 호출에서는 정수(`20230316`), 다른 호출에서는 문자열(`"23.03.16"`)로 반환된다. 23만건 이상의 레코드를 하나의 DataFrame으로 합칠 때 Polars의 스키마 추론이 실패한다.

### 해결

모든 컬럼을 `Utf8`로 강제한 뒤, `dtypes` 설정에 따라 명시적으로 캐스팅한다.

```python
schema = {col: pl.Utf8 for col in column_mapping.values()}
df = pl.DataFrame(mapped, schema=schema)
```

### Builder 적용 지점

- **Silver 단계**: 지금은 DuckDB(ADR 0021)다. 레코드를 표로 올리기 전에 한 컬럼에 섞인 타입을 찾아 조용히 바꾸지 않고 거부하며(`RecordTypeScan`, #187), source 가 `read_as: str` 로 선언한 컬럼은 텍스트로 읽고, 타입은 선언된 캐스트로만 확정한다. 이 절의 Polars 오류는 레거시 publish 스크립트와 그 이전 엔진의 기록이다
- **BuildSpec**: `dtypes` 필드를 필수로 요구하여 암묵적 추론에 의존하지 않도록 강제
- **검증**: 캐스팅 실패 시 null로 변환하되 실패 건수를 경고로 출력

---

## 3. xmltodict 의존성 누락

### 증상

`kpubdata`의 datago provider가 XML 응답을 파싱할 때 `ModuleNotFoundError: No module named 'xmltodict'` 발생.

### 원인

`kpubdata`는 `xmltodict`를 optional dependency로 관리하지만, builder의 `publish` extra에는 포함되지 않았다.

### 해결

`pyproject.toml`의 `publish` extra에 `xmltodict>=0.13,<1` 추가.

### Builder 적용 지점

- **의존성 관리**: Builder가 `kpubdata`를 사용할 때 필요한 모든 optional dependency를 builder 측에서도 명시
- XML 기반 provider(datago 등)를 사용하는 BuildSpec은 `xmltodict`가 필수

---

## 4. 데이터 규모 판단 실패

### 증상

첫 퍼블리싱 결과 3,221건 (강남구+서초구, 6개월). HuggingFace에서 의미 있는 데이터셋으로 보기에 너무 작음.

### 교훈

- California Housing 데이터셋: ~20,640건
- NYC Taxi 데이터셋: 수백만건
- 시계열 분석에는 최소 36개월 이상 데이터가 필요 (계절성, 시장 사이클)

### 최종 결정

서울 25개구 × 60개월 (2020-2024) = 1,500 API 호출, ~23만건

### Builder 적용 지점

- **BuildSpec 검증**: 예상 레코드 수를 사전 추정하는 `estimated_records` 필드 고려
- **품질 기준**: `hf-publishing-standards.md`에 최소 10,000건, 시계열 36개월 기준 명시
- **Preview 단계**: 소규모 샘플로 먼저 확인 후 전체 실행하는 2단계 워크플로우

---

## 5. 공공누리 출처표시 법적 요건

### 증상

법적 검토 없이 데이터를 퍼블리싱하려 함.

### 교훈

한국 공공데이터를 HuggingFace에 재배포하려면:

1. **공공누리 유형 확인** 필수 (제1~4유형에 따라 허용 범위 다름)
2. **출처표시 문구** 한국어 원문을 Dataset Card에 반드시 포함
3. **CC 라이선스 매핑**: 공공누리 제1유형 → `cc-by-4.0`

### Builder 적용 지점

- **BuildSpec**: `license_type` (공공누리 유형) 필드를 spec에 포함
- **Export 단계**: 공공누리 유형에 따라 출처표시 문구를 자동 생성
- **Publish 단계**: 라이선스/출처표시 누락 시 업로드 차단 (early validation)
- 상세 매핑 테이블: `hf-publishing-standards.md` §3 참조

---

## 6. Dataset Card 언어 — 글로벌 플랫폼은 영어

### 증상

첫 버전 Dataset Card를 한영 혼합으로 작성. 글로벌 사용자가 내용을 이해하기 어려움.

### 교훈

- Dataset Card 본문: **영어**
- 컬럼 설명: **영어 + (한국어)** 병기
- 출처표시: **한국어 원문 + 영어 번역** 병기

### Builder 적용 지점

- **Export 단계**: Dataset Card 템플릿을 영어로 기본 생성
- **BuildSpec**: `features` 설명에 `{영어} ({한국어})` 패턴 강제
- 상세 언어 규칙: `hf-publishing-standards.md` §4 참조

---

## 7. Kaggle SDK `dataset_view` API 없음

### 증상

```
AttributeError: 'KaggleApi' object has no attribute 'dataset_view'
```

### 원인

kaggle SDK 1.6+ 에서 `dataset_view()` 메서드가 제거됨. 공식 문서나 예제에는 여전히 언급되는 경우가 있어 혼동 유발.

### 해결

`dataset_list(mine=True, search=slug_name)` 으로 대체하여 존재 여부를 판별한다.

```python
results = api.dataset_list(mine=True, search=kaggle_slug.split("/")[-1])
dataset_exists = any(str(d) == kaggle_slug for d in results)
```

### Builder 적용 지점

- **Publisher 모듈**: Kaggle API 호출 시 SDK 버전별 API 가용성을 방어적으로 처리
- `dataset_view`는 사용하지 말 것. `dataset_list` + 필터링 패턴 사용

---

## 8. Kaggle API 401 인증 에러

### 증상

```
ApiException: (401) Reason: Unauthorized
```

### 원인

Kaggle에서 새 API 토큰을 발급하면 **이전 토큰이 즉시 폐기**됨. 환경변수(`KAGGLE_KEY`)와 `~/.kaggle/kaggle.json`에 저장된 값이 서로 다르거나, 둘 다 구 토큰인 경우 발생.

### 해결

1. https://www.kaggle.com/settings → API → **Create New Token** 으로 새 토큰 발급
2. 환경변수(`KAGGLE_USERNAME`, `KAGGLE_KEY`)와 `~/.kaggle/kaggle.json` **모두** 갱신
3. `source ~/.zshrc` 로 반영 확인

### Builder 적용 지점

- **인증 검증**: Publish 시작 전 `api.authenticate()` 후 간단한 API 호출(`dataset_list(mine=True)`)로 토큰 유효성을 사전 검증
- **에러 메시지**: 401 에러 발생 시 "토큰 재발급 필요" 안내 메시지 출력

---

## 9. Kaggle은 Organization 미지원

### 증상

HuggingFace에서 `kpubdata/seoul-apartment-trades` (org 네임스페이스)로 업로드한 것과 동일한 브랜딩을 Kaggle에서 사용 불가.

### 원인

Kaggle은 organization 계정을 지원하지 않음. 모든 데이터셋은 개인 계정 소속 (`username/dataset-name`).

### 해결

- Kaggle slug를 별도로 관리: `kaggle_slug: "yschoe/seoul-apartment-trades"`
- HF slug과 Kaggle slug을 config에서 분리하여 관리

### Builder 적용 지점

- **BuildSpec**: `hf_repo`와 `kaggle_slug`을 별도 필드로 유지 (네임스페이스가 다를 수 있음)
- **문서화**: Dataset Card에 양쪽 플랫폼 URL을 모두 기재하여 cross-reference 제공

---

## 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [hf-publishing-standards.md](./hf-publishing-standards.md) | 퍼블리싱 표준 규칙 (이 문서의 교훈이 반영된 규칙) |
| [publishing.md](./publishing.md) | 퍼블리싱 스크립트 사용법 |

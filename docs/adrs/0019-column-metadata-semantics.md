# ADR 0019 — 컬럼 메타: 저장 타입·의미·표시·단위·출처의 분리

- 상태: 제안됨(Proposed)
- 관련 이슈: #813, #702(식별자 판정), #735·#794(wire encoding), #814(클라이언트 호환 fixture)
- 관련 문서: `contract/builder-api.yaml`(1.41.0), `src/kpubdata_builder/tabular/semantics.py`, `src/kpubdata_builder/tabular/wire.py`

## 맥락

1.40.0 까지 컬럼 메타는 `dtype`·`logical_type`·`wire_encoding` 세 필드뿐이다. 이 셋은
**어떻게 저장되고 어떻게 전송되는가**만 말한다. 값이 **무엇인가**(코드·측정값·날짜·기간),
어떤 이름으로 보여 줄지, 어떤 단위·배율로 세는지, 그리고 그 말을 **누가 했는지**는 없다.
그래서 Studio 는 추정하거나, 이미 다른 뜻이 있는 필드에 의미를 겹쳐 싣게 된다.

Core(kpubdata)에는 이미 `FieldDescriptor.title`·`description`, `FieldConstraints.format`
이 있다. 매핑 없이 새 필드를 만들면 같은 의미가 두 곳에 산다.

## 결정

### 1. 개념 정의

| 개념 | 필드 | 뜻 | 누가 정하나 |
| :--- | :--- | :--- | :--- |
| 저장 타입 | `dtype` (Silver·preview), `logical_type` | Builder 가 실제로 들고 있는 Polars 타입. `logical_type` 은 매개변수를 뺀 이름 | 데이터 자체. **힌트로 바꿀 수 없다** |
| wire encoding | `wire_encoding` | 이 응답에서 값이 JSON 으로 어떻게 실리는가(#735, #794) | 응답마다 Builder 가 값으로 판정 |
| 의미 종류 | `semantic.kind` | `code`·`measure`·`date`·`period`. 열린 문자열 | 힌트(출처 포함) |
| 표시 | `display.label`·`description`·`format` | 사람에게 보여 줄 이름·설명·표시 형식 | 힌트(출처 포함) |
| 단위 | `unit.name`·`scale` | 측정값의 단위와 배율(1000 = 천 단위) | 힌트(출처 포함) |
| 출처 | 각 힌트의 `origin` | `user_annotation`·`core_spec`·`catalog`·`engine_inferred` | 힌트를 만든 쪽 |

> 2026-10 주: "Polars 타입"은 이 ADR 당시의 표현이다. [ADR 0021](0021-duckdb-tabular-engine.md) 이후 저장 타입은
> DuckDB 가 들고 있는 값을 Builder dtype 이름(`tabular/dtypes.py`)으로 부른 것이고, 이름 자체는 같다.

`semantic`·`display`·`unit` 은 모두 **선택**이다. 아무도 설명하지 않은 컬럼에는 키 자체가
없다 — 빈 객체나 `null` 을 보내지 않는다. 빈 객체는 "설명했는데 아무것도 없다"와 구분되지
않기 때문이다.

### 2. Core 매핑

| Core | 컬럼 메타 |
| :--- | :--- |
| `FieldDescriptor.title` | `display.label` |
| `FieldDescriptor.description` | `display.description` |
| `FieldConstraints.format` | `display.format`(원문 그대로). 날짜 형식(`date`, `YYYYMMDD`, `YYYY-MM-DD`)이면 `semantic.kind: date`, 기간 형식(`YYYYMM`, `YYYY-MM`, `YYYY`…)이면 `period`. 그 밖의 형식은 종류를 말하지 않는다 |
| `FieldDescriptor.type`, `nullable` | **쓰지 않는다.** 원천이 주장한 타입이지 Builder 가 가진 저장 타입이 아니다 |
| (Core 에 단위 필드 없음) | `unit` 은 Core 에서 오지 않는다 |

이 매핑은 `semantics.from_field_descriptor` 한 곳에 있다. Core 의 특정 릴리스에 묶이지
않도록 속성 이름으로 읽는다.

### 3. 우선순위

한 컬럼을 여러 출처가 설명하면 **힌트마다 따로** 가장 높은 출처의 것을 쓴다.

```text
user_annotation > core_spec > catalog > engine_inferred
```

라벨은 사용자 주석에서, 단위는 Core 명세에서 올 수 있다. 같은 출처끼리는 먼저 주어진 것을
쓴다(`semantics.resolve`). 계약의 `ColumnMetaOrigin` enum 순서가 이 우선순위이며, 테스트가
둘이 같음을 확인한다.

### 4. 힌트가 할 수 없는 것

- 저장 타입·`wire_encoding`·값을 바꾸지 않는다. `measure` 라고 적힌 문자열 코드 컬럼은
  여전히 `string` 으로, 선행 0 을 그대로 가진 채 전송된다.
- 캐스팅·집계·행 식별(키)을 자동으로 바꾸지 않는다. 식별자 판정은 #702 가 따로 한다.
- 라이선스, PII 판정, 원천 출처(provenance)를 덮어쓰지 않는다. 그것들은 힌트가 아니며 이
  우선순위의 대상이 아니다.
- `unit.scale` 은 표시용이다. 전송되는 값은 배율로 다시 계산되지 않는다.
- `engine_inferred` 는 추정이다. 클라이언트는 추정임을 표시한다.

### 5. 클라이언트 규칙

- 모르는 `semantic.kind` 는 파싱 오류가 아니다. 원문 값을 그대로 보여 준다. 그래서 계약에서
  `kind` 는 enum 이 아닌 열린 문자열이다.
- 모르는 `wire_encoding` 은 숫자로 추측하지 않는다. 원문 텍스트로 보여 준다.
- 응답에는 선택 필드가 추가될 수 있다(#814). 이 세 필드가 그 예다.

## 개정 (2026-09-30, #702, 계약 1.61.0)

소유자 결정에 따라 식별자 판정을 연결한다. kpubdata 0.8 의 `FieldDescriptor.semantic_kind`
(kpubdata ADR 0006)를 `semantic.kind`(`origin: core_spec`)로 옮기고 — 모르는 종류도 원문 그대로
— `semantic.kind` 가 `code` 이고 Builder 가 **텍스트로 저장한** 컬럼은 `logical_type: identifier`
로 보고한다(`wire.mark_identifiers`).

- 4 절의 예외는 이것 하나다. 저장 타입(`dtype`, 프로파일의 `storage_type`)·`wire_encoding`
  (`string`)·값은 그대로다. 텍스트가 아닌 컬럼(사용자가 정수로 캐스팅한 코드 등)은 저장 타입의
  `logical_type` 을 유지한다.
- 기각한 대안(`logical_type: "code"`)의 문제 — 저장 타입을 알 수 없게 됨 — 는 생기지 않는다.
  `identifier` 는 언제나 텍스트 저장이기 때문이다.
- 선언은 run 의 BuildSpec 스냅숏이 가리키는 kpubdata 명세에서 읽는다(`service/column_semantics.py`).
  `schema.rename` 은 따라가고, `coalesce` 로 합쳐진 후보와 두 소스가 다르게 설명한 컬럼은
  설명하지 않는다. 값으로 추정하지 않는다. 사용자 주석 저장소는 아직 없으므로 이 층은 `core_spec`
  하나다.
- 이 개정으로 `/query`·`/warehouse/*`·`/preview` 의 컬럼 메타에 `semantic`·`display` 가 실리기
  시작한다. Silver stage 상세(`SilverColumnInfo`)에는 아직 싣지 않는다.

## 이번 범위와 남은 일

이번 변경(계약 1.41.0)은 계약·매핑·우선순위·보존 테스트까지다. **현재 Builder 는 이 힌트를
아무 응답에도 싣지 않는다.** Core 스키마(`SchemaDescriptor`)를 받아 오는 경로, 카탈로그
힌트, 사용자 주석 저장소가 아직 Builder 에 없기 때문이다. 이것들이 생기면
`semantics.with_semantics` 로 컬럼 메타에 붙인다.

Studio 의 `silverColumnInfoSchema` 는 `.strict()` 라 Silver stage 상세에 이 필드가 실리면
파싱에 실패한다(#735 때 실제로 일어났다). Silver 에 힌트를 싣기 전에 Studio 가 추가 필드를
허용해야 한다(#814 짝 이슈). preview 와 query 의 컬럼 메타는 Studio 쪽이 strict 가 아니다.

## 검토한 대안

- **`logical_type` 에 의미를 싣는다** (`logical_type: "code"`): 저장 타입과 의미가 한 필드에
  섞여, 코드로 설명된 정수 컬럼이 무엇으로 저장됐는지 알 수 없게 된다. 기각.
- **`semantic_kind` 를 enum 으로 둔다**: 종류를 하나 늘릴 때마다 strict 클라이언트가
  깨진다. 기각.
- **출처를 컬럼당 하나만 둔다**: 라벨과 단위가 다른 곳에서 오는 흔한 경우를 표현하지 못한다.
  힌트마다 `origin` 을 둔다.

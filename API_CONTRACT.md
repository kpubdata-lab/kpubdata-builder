# API 계약 — KPubData Builder

## 1. 단일 소스

KPubData Builder(패키지 `kpubdata-builder`) HTTP wire 계약의 단일 소스는 [contract/builder-api.yaml](https://github.com/yeongseon/kpubdata-builder/blob/main/contract/builder-api.yaml)입니다.

- endpoint, request body, response body, status code, security scheme은 OpenAPI 문서를 기준으로 합니다.
- `contract/builder-api.yaml`의 `info.version`은 `kpubdata_builder.service.API_CONTRACT_VERSION`과 일치해야 합니다.
- `tests/unit/test_service_contract.py`가 버전 일치, 정적 route/status 일치, 실제 `dispatch()` 응답의 wire-level conformance를 검증합니다.
- Studio 같은 소비자는 이 문서가 아니라 OpenAPI SSOT와 `GET /version`을 기준으로 호환성을 판단합니다.
- 버전 상승·Studio 호환 범위·release freeze 절차는 [ADR 0013](https://github.com/yeongseon/kpubdata-builder/blob/main/docs/adrs/0013-api-contract-release-policy.md)을 따릅니다.

### 계약 버전 요약

- `main`의 계약 버전은 항상 stable SemVer이며 OpenAPI와 코드가 같은 값을 사용합니다.
- additive wire 변경은 minor, 기존 의미를 유지하는 계약 오류 수정은 patch, breaking 변경은 major입니다.
- example·설명·내부 refactor처럼 wire가 같으면 버전을 올리지 않습니다.
- 위 두 줄은 **CI 가 강제한다**(#693). PR 마다 `scripts/check_contract_compat.py` 가 base
  브랜치의 계약과 비교해, 규범적 변경(설명·summary·example·`x-*` 이외)에 `info.version` 이
  오르지 않았거나, operation·status code·media type·property·schema·enum 값 제거, 타입·`$ref`
  변경, 새 필수 parameter 같은 **breaking 변경이 major 상승 없이** 들어오면 실패한다. 의도한
  breaking 변경의 명시적 승인은 major 상승 자체다.
- Studio는 exact equality가 아니라 같은 major와 기능별 최소 SemVer를 확인합니다. 새 operation을
  실제로 소비할 때만 schema/client와 최소 기능 버전을 갱신합니다.
- Epic #484 완료는 개발 중 버전 변경을 미루는 지점이 아니라 최종 계약을 freeze하고 release
  manifest/tag에 기록하는 지점입니다.

### 클라이언트 호환 규칙 (#814)

응답을 읽는 쪽이 지켜야 하는 규칙입니다. 서버 쪽 규칙(위)과 짝을 이룹니다.

- **응답에는 minor 버전마다 선택 필드가 더해질 수 있습니다.** 모르는 필드는 무시합니다.
  계약의 `additionalProperties: false` 는 *이 버전의 Builder 가 보내는 것*을 서술할 뿐,
  클라이언트가 추가 필드를 거부하라는 뜻이 아닙니다. 응답 파서를 strict 하게 만들면 additive
  변경에 깨집니다 — 1.30.0 의 `logical_type`/`wire_encoding` 추가 때 Studio 의
  `silverColumnInfoSchema.strict()` 가 실제로 그랬습니다(#735).
- **필수 필드의 이름과 타입은 다음 major 전까지 바뀌지 않습니다.** 필수 필드가 없거나 타입이
  다르면 클라이언트는 여전히 거부합니다. 관대함은 모르는 필드에만 적용됩니다.
- 모르는 enum 값(예: 새 `wire_encoding`)은 원문으로 다루고, 숫자로 추측하지 않습니다.
- `logical_type: identifier`(1.61.0, #702)는 kpubdata 명세가 `semantic_kind: code` 로 선언한
  텍스트 컬럼입니다(법정동코드, PNU, 우편번호). `wire_encoding` 은 항상 `string` 이고, 클라이언트는
  이 값을 자동으로 `Number()` 로 바꾸지 않습니다 — 앞자리 0 이 사라지고 JOIN 이 어긋납니다.
  어떤 컬럼이 identifier 인지는 kpubdata 선언에서 오며, Builder 는 값을 보고 추정하지 않습니다.

이 규칙은 기계가 검사할 수 있게 fixture 로 내놓습니다. `contract/fixtures/responses.json` 은
계약의 모든 2xx named response example 마다 세 가지 본문을 담습니다.

| 키 | 뜻 | 클라이언트 기대 |
| :--- | :--- | :--- |
| `current` | 이 계약 버전이 보내는 그대로 | 통과 |
| `with_additive_fields` | 속성을 선언한 모든 객체에 `future_optional_field` 를 더한 것(`additive_paths` 가 위치) | 통과 |
| `required_type_broken` | 최상위 필수 필드 하나(`broken_path`)의 타입을 바꾼 것 | 거부 |

파일 머리의 `contract_version` 이 fixture 가 만들어진 계약 버전입니다. 이 파일은
`scripts/generate_response_fixtures.py` 가 계약에서 생성하며, 손으로 고치지 않습니다.
`tests/unit/test_response_fixtures.py` 가 커밋된 파일이 재생성 결과와 같은지, `current` 가 계약에
맞는지, 추가 필드 본문은 strict 파서가 거부하고 관대한 파서는 받아들이는지, 필수 필드 타입 오류는
관대한 파서도 거부하는지 확인합니다. 계약 example 을 바꾸면 스크립트를 다시 실행해 함께
커밋합니다. Studio 계약 테스트는 이 파일을 읽어 자기 응답 스키마를 검사합니다(짝 이슈).

이 문서는 사람이 읽는 운영 가이드입니다. wire 형태를 옮겨 적지 않습니다.

### Builder 가 소유하는 어휘 (#831)

계약의 enum 은 모두 Builder 의 것입니다 (Independence Rule 7). 값이 지금 kpubdata 와 같아도
소유자는 Builder 이고, kpubdata 의 enum 값을 그대로 wire 로 흘려보내지 않습니다.
`src/kpubdata_builder/service/vocabulary.py` 가 kpubdata 값마다 Builder 값을 명시적으로 매핑하고,
매핑에 없는 값은 선언된 대체값으로 바꿉니다.

| 계약 필드 | Builder 어휘 | 현재 값의 출처 | 매핑에 없는 kpubdata 값 |
| :--- | :--- | :--- | :--- |
| `DatasetStatusAxes.access` | `AccessStatus` | kpubdata probe 분류 + `unknown` | `unknown` |
| `CatalogDataset.representation` | `Representation` | kpubdata `Representation` | `other` |
| `CatalogDataset.operations[]` | `Operation` | kpubdata `Operation` | 목록에서 뺌 |
| `CatalogQuerySupport.pagination` | `PaginationMode` | kpubdata `PaginationMode` | `query_support` 전체를 `null` |

kpubdata 가 값을 더해도 wire 는 바뀌지 않습니다. 새 값을 어떻게 부를지는 Builder 가 이 매핑에서
정하고, wire 에 값이 늘면 계약 버전을 올립니다. `tests/unit/test_wire_vocabulary.py` 가 매핑이
계약 enum 과 같은지, 설치된 kpubdata 의 값을 모두 매핑하는지, 모르는 값이 대체값이 되는지 확인합니다.

## 2. 실행 모델

v0.4 Builder service는 동기식 실행 모델을 유지합니다.

| 범위 | 모델 | 기준 |
| :--- | :--- | :--- |
| `/validate`, `/preview`, `/build`, 조회 계열 | 요청-응답 동기 처리 | ADR 0002 |
| 비동기 job 모델 (`POST /builds`, `GET /builds/{run_id}`, `POST /builds/{run_id}/cancel`) | 접수 후 즉시 반환, 상태는 polling | ADR 0008 / #334 |

원칙:

- `POST /build`는 현재 요청 안에서 파이프라인을 실행하고 성공/실패 결과를 반환합니다.
- `POST /builds` / `GET /builds/{run_id}` / `POST /builds/{run_id}/cancel`은 ADR 0008(#334)의 상태 머신·멱등성·협력적 취소·부분 산출물 규약에 따라 도입되어 있습니다.
- Medallion stage별 artifact/preview 조회가 필요해지면 OpenAPI SSOT에 stage-specific endpoint를 먼저 추가합니다.

## 3. 응답 정책

자세한 schema와 status code는 OpenAPI SSOT를 따릅니다. 정책 수준의 의미는 다음과 같습니다.

| 상황 | 정책 |
| :--- | :--- |
| 정상 요청 | endpoint별 성공 응답을 `200`으로 반환 |
| BuildSpec 파싱/로드 실패 | 클라이언트가 수정할 수 있는 입력 오류로 처리 |
| BuildSpec 검증 실패 | 문제 목록을 포함한 입력 오류로 처리 |
| preview source 실패 | HTTP 요청 자체는 성공할 수 있으며, source별 `status`/`error`로 실패를 표현 |
| build source 실패 | upstream/source 의존 실패로 처리하고 가능한 경우 manifest를 남김 |
| run별 BuildSpec 조회 | `GET /builds/{run_id}/spec`으로 redaction된 canonical YAML과 그 bytes의 digest를 반환 |
| built dataset 조회 | `GET /datasets`, `GET /datasets/{dataset_id}`, `GET /datasets/{dataset_id}/runs`로 `BuildSpec.dataset_id` 단위 grouping/latest run/run history를 반환 |
| stage summary/preview | `GET /builds/{run_id}/stages`, `GET /builds/{run_id}/stages/{stage}`로 source별 Bronze/Silver/Gold 상태와 안전한 요약을 반환 |
| structured quality/drift | `GET /builds/{run_id}/quality`로 run의 source별 `quality_results`/`schema_drift`를, `GET /datasets/{dataset_id}/quality/history`로 dataset의 run별 PASS/WARN/FAIL 집계 이력을 반환 |
| read-only query | `POST /query`가 server-resolved Silver/Gold table을 logical `dataset`으로 등록하고 별도 capacity/timeout 안에서 실행 |
| composition(join) | `BuildSpec.composition`이 있으면 `POST /build` 응답에 source별 `outcomes`와 별도로 `composition` 키(결합 결과)가 노출됨 |
| 비동기 job 취소 | `POST /builds/{run_id}/cancel`이 `queued` job은 실행 전에 곧바로 `cancelled`로, `running` job은 `cancelling`을 거쳐 안전한 stage 경계에서 `cancelled`로 종결. 종단 job이거나 정상 종료로 확정된 job은 `409` |
| 취소된 run의 부분 산출물 | 삭제하지 않고 partial manifest(`status: cancelled`, `partial: true`)와 함께 보존. 실행되지 않은 stage는 성공으로 기록하지 않으며, 취소를 실패로도 실패를 취소로도 표기하지 않음 |
| 인증 실패 | `401`은 재인증 대상, `403`은 권한 요청 대상, `503`은 JWKS 일시 장애 대상. 같은 클라이언트의 `401`이 반복되면 `429`(`code: "auth_throttled"`, `retry_after_seconds`)로 전환되고, 이때는 인증을 시도하지 않고 즉시 거부한다 |

정책과 구현이 다르면 구현 각주를 늘리지 말고 다음 순서로 정리합니다.

1. 실제 의도한 계약이면 `contract/builder-api.yaml`을 수정합니다.
2. 구현 버그이면 service 코드와 conformance test를 수정합니다.
3. Studio 영향이 있으면 Studio 클라이언트/문서 PR을 별도로 엽니다.

### BuildSpec과 preview 계약 해석

- HTTP 요청의 `spec` 필드는 현재와 같이 YAML 문자열입니다. OpenAPI의 `BuildSpec`
  컴포넌트는 그 YAML이 표현하는 canonical 도메인 구조를 타입 생성기가 읽을 수 있게
  정의합니다.
- `metadata`, `sources[].params`, `exports[].options` 값은 표준 JSON 호환 범위입니다.
- source preview는 성공과 실패 모두 `source_key`, `status`, `error`, `schema`,
  `sample`, `total_rows`, `statistics`를 반환합니다. source 실패는 HTTP 200 안에서
  `status: failed`, 비어 있는 schema/sample, 0 기반 statistics, 문자열 `error`로
  표현합니다.
- 제거된 `transforms`, top-level `normalization_mode`,
  `sources[].normalization_mode`는 계약 필드가 아니며 파서가 명시적으로 거부합니다.
- 검증을 통과해 실행을 시작한 spec은 pipeline 진입 전에
  `{output_root}/{run_id}/buildspec.yaml`에 원자 저장됩니다. legacy run처럼 snapshot이
  없는 경우 API는 manifest에서 추측·복원하지 않고 unavailable `404`를 반환합니다.
- snapshot redaction은 범용 매핑의 명시적인 credential 키에만 적용됩니다. inline secret은
  `<redacted>`로 대체되므로 해당 snapshot만으로 credential이 필요한 실행을 그대로 재실행할
  수는 없으며, credential은 환경/서비스 설정에서 다시 공급해야 합니다.
- `spec_digest`는 원본 객체가 아니라 **실제로 저장된 redaction 후 canonical snapshot bytes**의
  SHA-256입니다. credential 값만 다른 두 spec은 의도적으로 같은 snapshot/digest가 될 수 있습니다.

### Built Dataset과 Stage Summary/Preview (#488)

- **identity**: built dataset의 identity는 오직 `BuildSpec.dataset_id`입니다. 디렉터리
  이름이나 source catalog 이름으로 추측하지 않습니다. `buildspec.yaml` snapshot(#487)이
  없는 legacy run의 dataset_id는 추측하지 않으며, `GET /datasets*` grouping에서
  조용히 제외됩니다(`GET /builds`에는 계속 나타남).
- **latest run**: 각 dataset은 principal이 접근 가능한 run 중 `finished_at` 기준
  최신 run으로 요약됩니다. 동일 `finished_at`은 `run_id` 내림차순으로 결정적으로
  타이브레이크합니다. ownership 필터링은 latest 선정보다 먼저 적용되므로, 동일
  dataset_id를 가진 타 사용자의 run이 latest 후보나 metadata에 섞이지 않습니다.
- **row_count**: multi-source dataset의 row_count는 단일 스칼라로 축약하지 않습니다.
  `row_counts`(source_key별 맵)와 `total_row_count`(합계)를 함께 제공합니다.
- **quality**: `#486`(구조화된 quality gate)이 선반영되지 않았으므로 `quality` 필드는
  항상 `null`입니다. 현재의 log-only 품질 경고를 임의로 PASS/WARN/FAIL로 변환하지
  않습니다 — 미평가는 PASS가 아닙니다.
- **stage status**: `completed`/`failed`/`not_run`/`unavailable` 네 가지로 구분합니다.
  파일시스템 존재만으로 성공을 추측하지 않고, manifest의 실패 기록과 sidecar
  완전성을 함께 봐서 partial/failed run에서도 "Bronze 성공 → Silver 실패 → Gold
  미실행" 같은 상태를 구분합니다.
- **secret/path 비노출**: Bronze의 `fetch_params`/`provenance.fetch_params`, Gold
  export의 `options`/`output_path`, 그리고 어떤 응답에서도 절대 filesystem 경로는
  노출되지 않습니다. Silver `sample`은 build 시점에 이미 persist된 preview 상한
  (기본 5행) 안에서만 반환되며 parquet 전체를 읽지 않습니다.
- `dataset_id`는 slash/space 등 경로에 그대로 쓸 수 없는 문자를 포함할 수 있으므로,
  `GET /datasets/{dataset_id}` 경로의 `dataset_id`는 클라이언트가 percent-encoding해야
  합니다. `BuildSpec.dataset_id` 자체에는 이 API 때문에 새로운 제약을 추가하지
  않았습니다.

### 구조화된 Quality/Schema Drift와 History (#486)

- **정본은 manifest**: `quality_results`(source_key별 `QualityCheckResult` 목록)와
  `schema_drift`(source_key별 `SchemaDriftFinding` 목록)는 build 시점에 manifest.json에
  기록되며, `GET /builds/{run_id}/quality`는 이를 그대로 노출합니다. 별도로 다시
  계산하지 않습니다.
- **`availability`로 "0건 평가"와 "계산된 적 없음"을 구분(#514)**: 빈
  `quality_results: {}`만으로는 rule이 없어서 0건인지, quality 단계 자체가 돌지
  않았는지 구분할 수 없었습니다. `GET /builds/{run_id}/quality`는 이제
  `availability`(`available`/`partial`/`unavailable`)와 `evaluated_checks`(정수)를
  함께 반환합니다. `available`은 이 run이 시도한 모든 source(`manifest.inputs`)의
  quality 결과가 있음을 뜻하며 `evaluated_checks`가 0일 수 있습니다(rule 미설정 등).
  `partial`은 일부 source만 결과가 있는 경우(예: multi-source run에서 한 source의
  Silver가 실패)입니다. `unavailable`은 결과가 전혀 없는 경우로, `quality_results`
  필드 자체가 없는 legacy run(#486 이전)뿐 아니라, 필드는 있지만(`{}`) 시도한
  source 중 하나도 결과가 없는 새 run도 포함합니다(예: 모든 source가 quality
  단계 진입 전에 실패 — manifest writer는 quality가 하나도 계산되지 않았어도
  빈 `{}`를 항상 기록하므로 실제로 발생할 수 있습니다). 이 필드는 additive이며
  기존 `quality_results`/`schema_drift`
  형태는 바뀌지 않습니다.
- **PASS 포함 전체 보존**: `QualityCheckResult`는 실제로 평가된 check만 담되 PASS도
  포함합니다. rule 미설정/평가 불가(컬럼 없음, denominator 0 등)는 결과에서 아예
  제외됩니다 — PASS로 가장하지 않습니다.
- **확장 규칙 조건 보존**: `range` 결과의 `threshold`는 `min`/`max`를,
  `compare_columns` 결과는 `operator`/`right_column`을 구조화해 보존합니다. 컬럼은
  존재하지만 dtype이 호환되지 않아 평가할 수 없는 경우에는 규칙을 생략하지 않고
  선언된 severity의 WARN/FAIL과 안전한 `detail`을 기록합니다.
- **WARN/FAIL gate**: WARN은 Build를 계속 진행시키고 결과만 기록합니다. FAIL은 해당
  source를 Gold 진입 전에 실패시키며, 실패해도 이미 계산된 `quality_results`는
  manifest에 보존됩니다.
- **Preview/Build 동일 판정**: `POST /preview`의 각 `SourcePreview.quality_results`는
  Build와 동일한 evaluator(`quality.evaluate_quality`) 결과입니다. Preview는 drift는
  포함하지 않습니다(워크스페이스에 아무것도 쓰지 않으므로 이전 run과 비교할 대상이 없음).
- **Schema drift 비교 범위**: `detect_drift`는 "직전 아무 run"이 아니라 **동일
  dataset_id·source_key의 직전 "성공" run**과만 비교합니다. 다른 dataset/source의
  silver와 비교해 가짜 drift를 만들지 않습니다.
- **Quality History 집계**: `GET /datasets/{dataset_id}/quality/history`는 #488의
  dataset→run 조회 helper를 재사용해 접근 가능한 run별로 pass/warn/fail count,
  `evaluated_checks`, `rule_pass_rate`(`pass_count / evaluated_checks`,
  `evaluated_checks == 0`이면 `null`), `validated_rows`를 반환합니다. `validated_rows`는
  `QualityCheckResult.evaluated_rows`를 rule 수만큼 합산하지 않고, `#488`이 이미
  정의한 `row_counts` 합계(소스별 Silver row_count) semantics를 재사용합니다.
- **Legacy/partial/failed run**: manifest에 `quality_results`가 없는 legacy run은
  `evaluated_checks=0, rule_pass_rate=null`로 표현됩니다 — 미평가를 "전부 PASS"로
  해석하지 않습니다. partial/failed run도 structured 결과가 있으면 history에
  포함됩니다(정책적으로 제외하지 않음).
- **Ownership**: History/detail 모두 `/datasets/{dataset_id}`·`/datasets/{dataset_id}/runs`와
  동일한 ownership semantics를 공유합니다. 동일 `dataset_id`라도 타 사용자의 run은
  섞이지 않습니다.
- **AI 해석은 gate에 영향 없음**: drift의 원인 해석(#448, advisory)이 존재하더라도
  PASS/WARN/FAIL 판정이나 dataset quality 요약에는 관여하지 않습니다.
- `GET /datasets`, `GET /datasets/{dataset_id}` 응답의 `quality` 필드는 여전히 항상
  `null`입니다. 임의 종합 quality score를 만들지 않기 위한 의도적 설계이며, 구조화된
  결과는 위 두 전용 엔드포인트로 조회합니다.

### Read-only Query (#504)

- `/query`는 `dataset` physical relation을 최소 한 번 참조하는 단일 SELECT/CTE만
  허용합니다. CTE의 `dataset` shadowing, recursive CTE, 외부 table/table function,
  filesystem/network 접근, DML/DDL은 거부합니다.
- query는 HTTP worker pool과 별도인 bounded capacity를 사용합니다. Query timeout은 child
  process를 실제 종료하며 429/504 오류는 안정적인 `code`로 구분됩니다.

### Composition/Join (#506)

- `BuildSpec.composition`은 두 source의 검증된 Silver를 join해 별도 결합 Gold
  dataset(`gold/{composition.name}/`)을 부가로 만듭니다. source별 독립 Gold는
  그대로 유지되며, `composition`이 없는 기존 multi-source BuildSpec은 영향받지
  않습니다.
- `composition.join.left`/`right`가 참조하는 source는 `alias`를 반드시 선언해야
  하고, `composition`을 쓰는 BuildSpec 안에서는 선언된 `alias` 값이 서로
  달라야 합니다 — 이 규칙은 `composition`이 없는 BuildSpec에는 적용되지 않습니다.
- alias 참조·`join.type`/`join.on_duplicate_key` 어휘 같은 구조 문제는
  `validate_spec`이 스펙 파싱 직후 거부합니다. join key 컬럼의 존재 여부와 dtype
  일치(완전 동일만 허용, 자동 캐스팅 없음)는 두 source가 각각 Silver를 통과해야
  알 수 있으므로 빌드 파이프라인의 런타임 게이트가 담당합니다.
- 양쪽 join key가 모두 중복 값을 가지면(many-to-many) 결과 행이 곱셈으로
  폭증할 수 있습니다 — 기본 `on_duplicate_key: warn`은 결과를 만들고 경고를
  남기며, `fail`은 composition을 실패 처리합니다.
- `POST /build` 응답의 `composition` 키는 `{name, status, error}`이며
  `status`는 `ok`/`failed`(join 실행 자체 실패)/`skipped`(참조한 source가
  실패해 join을 시도조차 못함) 중 하나입니다. `composition`만 실패하고 모든
  source가 성공해도 최상위 `error` 요약은 composition의 error에서 파생됩니다.
- `manifest.json`의 `composition`(`CompositionProvenance`, additive)에는 join
  조건과 좌우 원본 행 수·distinct key 수·결과 행 수가 그대로 기록되어 원천과
  join 조건을 추적할 수 있습니다. legacy manifest는 이 필드가 없거나 null이며
  "composition 미사용 run"으로 해석해야 합니다.

### Stable Owner Identity (#505)

- display identity(사람이 읽는 라벨)와 persistent resource ownership identity를 분리합니다.
  `manifest.json`의 `created_by`는 기존(#388) display/legacy 라벨 그대로 유지되며, 신규
  `owner_id`(additive)가 ownership 판정에 쓰이는 canonical stable identity입니다.
- `owner_id`는 principal 종류별로 domain-separated SHA-256 해시입니다. OIDC는
  `sha256(kind + "\0" + issuer + "\0" + subject)`로 issuer/subject 전체(트렁케이션 없이)를
  해시해 사용합니다 — 이메일/표시 이름이 바뀌어도 값이 바뀌지 않고, subject 앞부분이
  우연히 겹치는 다른 사용자와도 절대 같아지지 않습니다. raw `sub`/이메일 등 민감한
  claim은 owner_id에도, 로그에도 직접 남기지 않습니다.
- ownership 판정(`_check_ownership`, `/query`, dataset/quality/stage 목록)은 레코드와
  principal 양쪽에 `owner_id`가 있으면 이를 우선 비교합니다. 어느 한쪽이라도 없으면(예:
  #505 이전에 생성된 legacy run) 기존 `created_by`/label 비교로 폴백합니다 — 기존 리소스가
  즉시 접근 불가가 되지 않습니다. `owner_id`도 `created_by`도 없는 레코드는 "누구나 접근
  가능"으로 취급되지 않고 거부됩니다(fail-closed).
- `owner_id`는 내부 ownership 판정 전용입니다. 디스크의 persisted `manifest.json`과
  파생 `BuildIndex`에는 저장하지만 `/builds` 목록과
  `GET /builds/{run_id}/manifest`를 포함한 HTTP 응답에서는 제거합니다. 따라서 OpenAPI
  `BuildManifest`의 공개 property가 아니며 wire 계약과 API 계약 버전은 바뀌지 않습니다.
- subject prefix 충돌 방지는 `owner_id`가 기록되는 #505 이후 신규 resource에 적용됩니다.
  `owner_id`가 없는 pre-#505 legacy resource는 호환성을 위해 기존 `created_by` 라벨로
  폴백하므로, 이미 저장된 truncated subject prefix 충돌을 소급해서 해소할 수 없습니다.
- 특정 신규 IdP 채택이나 email/password 로그인은 이 절의 범위가 아닙니다 — 향후 IdP
  결정(#515)과 무관하게 stable owner identity 계산 방식이 먼저 확정된 것입니다.

### System Resource·Build Statistics API (#516)

`GET /monitoring/summary`, `GET /monitoring/builds`는 Studio Monitoring 화면에
필요한 시스템/집계 observability를 제공합니다. Run 단위 이벤트(#496)는 별도
범위입니다.

- **"모른다"와 "0"을 구분합니다.** 측정된 적 없는 값은 `0`/`healthy`로 위장하지 않고
  `null`과 `available`/`partial`/`unavailable`(quality.py가 이미 정의한 어휘를
  재사용)로 표현합니다.
- **Aggregate status**(`MonitoringSummaryResponse.status`): required subsystem
  (`api`/`queue`/`workers`/`artifact_store`)의 `availability`로부터 계산되는
  deterministic 판정입니다. 넷 모두 `available`이면 `healthy`, 하나라도
  `partial`/`unavailable`이면 `degraded`입니다. latency SLA threshold(예:
  p95 100ms/500ms/1s)는 근거(ADR/config)가 없어 사용하지 않습니다 —
  `sample_count=0`/`p95_latency_ms=null` 자체나 `queue`/`workers`의 실제 0건은
  degraded 근거가 아닙니다(availability가 실제로 `unavailable`/`partial`일 때만
  degraded). Provider 상태(#492)는 optional이라 이 판정에 포함되지 않습니다.
- **Builder API 상태**: `dispatch()` 실행 시간을 최근 최대 1000개 요청의 bounded
  ring buffer로 기록하고 nearest-rank(보간 없음) 방식으로 p95를 계산합니다.
  `sample_count=0`이면 `p95_latency_ms=null`입니다. collector 자체가 손상되어
  표본을 읽을 수 없으면(#527) `availability=unavailable` +
  `sample_count=null` + `p95_latency_ms=null`입니다 — "정상 무표본"과 "측정
  불가"를 같은 값으로 뭉개지 않으며, 이 subsystem 실패가 원 요청이나
  monitoring 응답 자체를 실패시키지 않습니다. Healthy/Degraded 같은 latency
  임계값 판정은 근거(ADR/config)가 없어 발명하지 않았습니다 — raw
  `sample_count`/`p95_latency_ms`만 제공합니다.
- **Queue/Worker**: async build 실행 모델은 `AsyncBuildExecutor`/
  `AsyncBuildJobRegistry`(#511/#513)로 구현되어 있고 `BuilderService`가 항상
  생성해 `POST /builds`(비동기) 제출에 사용합니다. `queue`/`workers`는 이
  실행기의 read-only snapshot(`AsyncBuildExecutor.stats()`)을 그대로 반영하므로
  정상 runtime에서는 항상 `availability: available`입니다. `waiting`은
  status=`queued`, `running`은 status=`running`인 active job 수이고
  `total = waiting + running`입니다 — registry가 계속 보존하는 terminal
  (`succeeded`/`failed`/`cancelled`) job 이력은 섞지 않습니다. `workers.active`는
  `running`과 같고(worker 하나가 job 하나를 실행), `workers.capacity`는 실행기
  생성 시 보존한 `max_workers`이며 `ThreadPoolExecutor`의 private field를 직접
  읽지 않습니다. `workers.utilization`은 `active / capacity`(0.0~1.0)입니다.
  `availability: unavailable`은 (현재는 도달하지 않는) 진짜 async 미지원 구성을
  위한 fallback으로만 남겨두며, 그때만 나머지 필드가 `null`입니다 — "0건"과
  "확인 불가"를 구분합니다. `BoundedThreadingHTTPServer`의
  `ThreadPoolExecutor`(#253)는 이것과 무관한 HTTP 연결 동시성 상한입니다.
- **Artifact Store**: `output_root` 폴더가 존재한다는 사실만으로 `available`로
  간주하지 않습니다 — BuildIndex 쿼리도 성공해야 `available`입니다. `last_write_at`은
  BuildIndex에 기록된 가장 최근 성공(`ok`) 빌드의 `finished_at`에서만 얻으며, 성공
  기록이 없으면 `available`이되 `null`입니다(0건과 확인 불가를 구분). 절대 파일시스템
  경로는 노출하지 않습니다.
- **Build 통계**(`/monitoring/builds`): timezone은 UTC, bucket 경계는
  `[start, end)` 반열린, bucket 기준 timestamp는 `finished_at`(BuildIndex는 완료된
  빌드만 기록, ADR 0003)입니다. 현재 `window=24h`/`bucket=hour`만 지원하며 다른
  값은 400입니다. malformed timestamp(파싱 실패, NULL 포함)는 집계에서 제외되고
  `excluded_count`에 반영되며 전체 `availability`는 `partial`이 됩니다(BuildIndex
  쿼리 자체 실패는 `unavailable`, 정상 집계 결과 0건은 `available`). bucket 카운트
  wire 필드는 `total`/`success`/`failed`/`cancelled`입니다 — 내부 BuildIndex
  status 값 `ok`는 그대로 두고(변경 없음) 외부 Monitoring API 필드 이름만
  `success`로 매핑합니다(#527).
- **Provider 상태**는 요청마다 실제 네트워크 프로브를 유발하므로(#492) 이번 버전의
  Monitoring 응답에는 포함되지 않습니다.
- **Ownership**: 시스템 aggregate(`api`/`queue`/`workers`/`artifact_store`)는 개별
  run의 dataset/owner/credential 정보를 포함하지 않아 필터링이 필요 없습니다.
  `/monitoring/builds`의 버킷 집계와 recent runs는 `ENFORCE_OWNERSHIP`+oidc
  principal일 때 `principal_owns()`(#505)와 동일한 정책으로 필터링해 다른
  사용자의 run이 섞이지 않습니다. bucket 집계는 window 전체를 먼저 가져온 뒤
  필터링해도 손실이 없지만, `recent_runs`는 고정 10건 LIMIT이 걸린 조회라 필터를
  LIMIT **이전에** SQL에서 적용합니다(`BuildIndex.list_recent_owned`, #527) —
  그렇지 않으면 다른 principal의 최신 run들이 LIMIT을 다 채워 요청자 본인의
  recent run이 빠질 수 있습니다.

### File·URL Source Ingestion (#498)

Public API/File/URL source가 동일한 canonical source contract와
Bronze→Silver→Gold pipeline을 공유합니다. `BuildSpec.sources[].kind`가
`public_api`(기본)/`file`/`url`을 구분하며, `kind`를 생략한 source는 항상
`public_api`로 해석되어 기존 동작이 그대로 유지됩니다.

- **File 업로드**: `POST /uploads`는 JSON이 아니라 raw binary body를 받습니다
  (multipart 대신 동등한 binary upload). `format`/`encoding`/`filename`은
  query parameter로 전달하며, 서버가 즉시 파싱 가능성을 검증해(손상·빈 파일은
  400) fail-fast합니다. 저장된 content는 요청 principal의 `owner_id`로
  격리되며, 다시 원문을 내려주는 API는 없습니다 — `GET /uploads/{upload_id}`는
  메타데이터만 반환합니다. `BuildSpec.sources[].upload_id`가 참조하는 업로드는
  build/preview를 요청한 principal과 소유자가 같아야 하며, 다르면 존재 여부를
  구분하지 않고 동일하게 not found로 처리합니다(fail-closed, #505의 ownership
  패턴과 동일). `sources[].format`/`encoding`은 업로드 시점에 검증된 값과
  정확히 일치해야 합니다. 업로드 content는 로컬 파일시스템 경로가 아니라
  SQLite에 저장되므로 path traversal 표면이 없고, `upload_id`는 서버가
  발급하는 불투명한 식별자(`upl_<hex32>`)입니다 — 사용자가 filename/path를
  직접 참조할 수 없습니다.
- **URL fetch(P0)**: `kind="url"` source는 GET, Auth=None인 안전한 HTTP(S)
  fetch만 지원합니다(Bearer credential 연동은 #492 이후 P1). SSRF 방어로
  `https` 외 scheme과 userinfo가 포함된 URL을 거부하고, hostname을 DNS로 직접
  resolve해 loopback/private/link-local/reserved 등 비공인(non-global) 주소로의
  접속을 차단합니다(하나라도 비공인이면 전체 거부). 실제 TCP 연결은 검증한
  IP에 직접 여는 방식으로 검증 시점과 연결 시점 사이의 DNS rebinding을
  방지하며, redirect마다 동일 검증을 반복합니다(최대 5회). 응답 크기와
  connect/read timeout에 상한을 둡니다. BuildSpec 계약에 header/POST/PUT/PATCH
  필드가 아예 없어 임의 header나 다른 HTTP method를 표현할 수 없습니다.
- **Provenance/manifest 비노출**: file source의 provenance는 로컬 파일시스템
  경로 대신 `upload_id`만 담습니다. url source의 provenance/manifest는 query
  string이 제거된 endpoint만 담아 우연히 섞인 secret이 남지 않게 합니다. 두
  kind 모두 기존 `SourceProvenance`(provider/dataset 필드) 모양을 그대로
  재사용합니다 — file은 `provider="file", dataset=upload_id`, url은
  `provider="url", dataset=<host+path 기반 경로-안전 slug>`로 채웁니다(사람이
  읽는 원본 endpoint는 `fetch_params.endpoint`에 별도로 남습니다).
- **Preview/Build 동일 경로**: `POST /preview`와 `POST /build` 모두 같은
  source resolver를 공유하므로, file/url source도 Public API source와 동일한
  스키마/샘플/quality 판정 흐름을 거칩니다.

## 4. 인증과 CORS

브라우저 클라이언트(Studio 등)와의 연동을 위해 CORS는 default-deny입니다.

- `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 미설정 시 cross-origin 요청을 거부합니다.
- Same-origin 요청은 허용합니다.
- preflight는 허용 origin에 대해 `GET, POST, OPTIONS`와 `Content-Type, X-API-Key, Authorization`을 허용합니다.

인증은 두 경로를 지원합니다.

| 방식 | 헤더 | 용도 | 환경변수 |
| :--- | :--- | :--- | :--- |
| API Key | `X-API-Key: <secret>` | 서비스 계정, 스케줄 워크플로 | `KPUBDATA_BUILDER_API_KEY` |
| Bearer (OIDC) | `Authorization: Bearer <jwt>` | 사람 사용자, Studio | `OIDC_ISSUER` + `OIDC_AUDIENCE` |

OIDC는 `OIDC_ISSUER`/`OIDC_AUDIENCE`와 허용 목록(`OIDC_ALLOWED_HD`, `OIDC_ALLOWED_SUBJECTS`, `OIDC_ALLOWED_EMAILS`)이 설정된 경우에만 활성화됩니다.

## 5. CLI 대응 관계

CLI와 HTTP service mode는 같은 도메인 계약을 공유합니다.

| CLI | 대응 API operation |
| :--- | :--- |
| `kpubdata-builder validate spec.yaml` | `validateSpec` |
| `kpubdata-builder preview spec.yaml` | `previewBuild` |
| `kpubdata-builder build spec.yaml` | `createBuild` |

CLI와 HTTP 응답 의미가 갈라지면 OpenAPI와 service conformance test를 먼저 확인합니다.

## 6. Python API — BuilderService

Python 코드에서 직접 사용하는 경우 `BuilderService`를 통해 HTTP 없이 같은 service 로직을 호출할 수 있습니다.

```python
from pathlib import Path

from kpubdata_builder.service import BuilderService

service = BuilderService(
    output_root=Path("./dist"),
    client_factory=lambda: my_kpubdata_client,
)

validate_response = service.validate(spec_yaml_str)
build_response = service.build(spec_yaml_str, run_id="my-run-001")
```

반환 객체의 body shape도 OpenAPI SSOT와 conformance test 대상입니다.

## 7. 관련 문서

| 문서 | 역할 |
| :--- | :--- |
| [contract/builder-api.yaml](https://github.com/yeongseon/kpubdata-builder/blob/main/contract/builder-api.yaml) | HTTP wire 계약 SSOT |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 입력 계약 |
| [BUILD_STATE.md](./BUILD_STATE.md) | build 상태 모델 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
| [docs/adrs/0002-build-execution-model.md](https://github.com/yeongseon/kpubdata-builder/blob/main/docs/adrs/0002-build-execution-model.md) | v0.4 동기 build 모델 결정 |
| [docs/adrs/0005-api-contract-single-source.md](https://github.com/yeongseon/kpubdata-builder/blob/main/docs/adrs/0005-api-contract-single-source.md) | OpenAPI SSOT 결정 |

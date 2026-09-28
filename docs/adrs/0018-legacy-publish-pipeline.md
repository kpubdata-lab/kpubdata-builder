# ADR 0018 — 레거시 publish 파이프라인과 BuildSpec 경로의 공존

- 상태: 제안됨(Proposed) — **결정 대기**
- 관련 문서: [ADR 0003 — persistent build store](./0003-persistent-build-store.md), [BUILD_SPEC.md](../BUILD_SPEC.md), [DATA_FRESHNESS.md](https://github.com/yeongseon/kpubdata-builder/blob/main/DATA_FRESHNESS.md)

## 맥락

이 저장소에는 데이터셋을 게시하는 경로가 **두 개** 있다.

| | 정식 경로 | 레거시 경로 |
|---|---|---|
| 입력 | `specs/*.yaml` (BuildSpec) | `scripts/configs/*.yaml` |
| 실행 | `kpubdata_builder.pipeline.run_build` | `scripts/publish_to_hf.py` + `scripts/pipeline/` |
| 진입 | CLI `build`/`publish`, HTTP `POST /builds` | `.github/workflows/publish-dataset.yml` |
| 개수 | **3** | **20** (그중 18개가 스케줄 게시) |

**실제 프로덕션 게시는 전부 레거시 경로로 돈다.** 스케줄 워크플로 5개
(`scheduled-air-quality`, `scheduled-dur`, `scheduled-weather`,
`scheduled-real-estate`, `scheduled-tourism`)가 `publish-dataset.yml` 을 호출하고,
그 워크플로는 `scripts/publish_to_hf.py <config> --target hf` 를 실행한다.

정식 경로는 Studio·HTTP API·CLI 가 쓰지만, 지금 HF 에 올라가 있는 데이터셋을
만드는 것은 레거시 쪽이다.

## 문제

두 경로가 있다는 사실이 **어디에도 적혀 있지 않다.** 그래서:

- 정식 경로에 넣은 수정(예: Kaggle 라이선스 기본값 제거, HF layout publish 차단
  해제)이 실제 게시되는 데이터셋에는 적용되지 않는다.
- 레거시 쪽에서 고친 것(예: Kaggle `public=False` 기본값)이 정식 경로에 반영되지
  않는다. 실제로 그런 적이 있다.
- 새 기여자는 `specs/` 를 보고 그게 전부라고 읽는다.

## 마이그레이션을 막는 것 — 측정값

레거시 config 를 BuildSpec 으로 옮기려면 BuildSpec 에 없는 개념 셋이 필요하다.
`scripts/configs/seoul_apartment_trades.yaml` 기준:

| 레거시 필드 | 예시 | BuildSpec 대응 |
|---|---|---|
| `transform.column_mapping` (13개) | `dealAmount` → `deal_amount_10k_krw` | ✅ `sources[].schema.rename` |
| `transform.derived` | 파생 컬럼 | ✅ `sources[].schema.casts` 일부 |
| `transform.filters` | `deal_amount_10k_krw > 0` | ❌ **없음** |
| `variants` | `en` / `ko` 두 벌의 컬럼명 | ❌ **없음** |
| `card.attribution` | 공공누리 출처표시 613자 | ❌ **없음** |

즉 지금 마이그레이션하면 **게시되는 데이터의 행과 컬럼이 달라진다.** 이미
공개된 HF 데이터셋의 스키마가 바뀌는 일이므로 코드 정리로 처리할 문제가 아니다.

## 공공누리 문제가 여기에 걸려 있다

`card.attribution` 은 단순한 누락이 아니다. 20개 config 중 **3개만** attribution
을 갖고 있고, 나머지 17개는 `license: cc-by-4.0` 만 붙은 채 게시되고 있다.

공공누리 제1유형은 출처표시가 **의무**다. 따라서 이건 마이그레이션 블로커이면서
동시에 현재 게시물의 준수 문제이기도 하다 — BuildSpec 에 attribution 개념을
만드는 일은 두 문제의 공통 선행 작업이다.

## 선택지

**A. 레거시 삭제.** 18개 스케줄 게시가 즉시 멈춘다. 위 세 가지를 BuildSpec 에
먼저 구현하지 않으면 불가능하다.

**B. 레거시 동결 + 신규는 BuildSpec.** 지금 상태를 명시적으로 만든다. 두 경로가
계속 남으므로 수정이 한쪽에만 들어가는 문제도 남는다.

**C. BuildSpec 을 채운 뒤 config 단위로 이관.** `filters`·`variants`·
`attribution` 을 BuildSpec 에 추가하고, 데이터셋 하나씩 출력이 바이트 단위로
같은지 확인하며 옮긴다. 스키마가 바뀌는 데이터셋은 그 사실을 공지하고 옮긴다.

## 권고

**C.** 다만 순서가 중요하다 — `attribution` 부터다. 그것만은 마이그레이션과
무관하게 지금 필요하고(공공누리 준수), 나머지 둘의 설계에도 영향을 주지 않는다.

이 ADR 은 어느 쪽도 확정하지 않는다. 결정 전에 레거시를 지우지 말 것.

## 결정되지 않은 것

- 스키마가 달라지는 데이터셋을 어떻게 공지할지
- `variants`(en/ko)를 BuildSpec 의 개념으로 만들지, 아니면 export 두 벌로 볼지
- 17개 데이터셋의 공공누리 유형 확인 주체 — 유형을 틀리게 적는 것은 적지 않는
  것보다 나쁘다

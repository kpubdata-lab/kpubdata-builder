# 데이터 업데이트 주기 전략 (Data Freshness Policy)

본 문서는 kpubdata-builder가 HuggingFace에 퍼블리시하는 데이터셋의 업데이트 주기와 전략을 정의한다.

## 원칙

1. **소스 발행 주기 기준**: 원천 데이터의 실제 갱신 주기에 맞춰 업데이트한다 (과도한 polling 금지).
2. **Atomic publish**: 로컬에서 파티션 완성 → row count/checksum 검증 → 단일 HF commit. 부분 업데이트 노출 금지.
3. **API 예산 준수**: data.go.kr 일 1,000회 제한을 초과하지 않도록 shard/rotation 적용.
4. **Reconciliation**: Incremental 데이터는 월 1회 전체 정합성 검증 (trailing window re-sync).

## 전략 분류

| 전략 | 설명 | 적용 대상 |
|---|---|---|
| **Append-only** | 이력 자체가 가치. 과거 데이터 불변, 새 파티션만 추가 | 시계열/이력 데이터 |
| **Incremental** | 레코드가 변동 가능. 최근 window만 upsert, 전체 rebuild 불필요 | 운영 데이터 |
| **Full refresh** | 작고 반정적인 데이터. 매번 전체 교체가 가장 단순 | 참조 데이터 |

## 데이터셋별 주기

> Cron은 **UTC** 기준. `KST = UTC + 9`.

| 데이터셋 | 주기 | 전략 | Cron (UTC) | KST 참고 | 비고 |
|---|---|---|---|---|---|
| 부동산 실거래가 (6종) | 매월 1~10일 daily | Incremental | `30 0 1-10 * *` | 09:30 KST | trailing 2-3개월 re-sync |
| DUR 의약품 안전사용 (8종) | 주 1회 (토) | Full refresh | `0 1 * * 6` | 10:00 KST | 소량·반정적 |
| 기상청 단기예보 | 3시간마다 | Append-only | `20 */3 * * *` | 매 3시간 | 발표회차별 파티션 |
| 기상청 중기예보 | 1일 2회 | Append-only | `20 0,12 * * *` | 09:20, 21:20 KST | |
| 관광정보 POI (명소 등) | 주 1회 (월) | Full refresh | `0 2 * * 1` | 11:00 KST | |
| 관광 이벤트/행사 | 매일 | Incremental | `0 3 * * *` | 12:00 KST | 현재~미래 window |
| 대기오염정보 | **게시 중지** (#759) | Append-only | `15 */2 * * *` — 워크플로 disabled | — | 공공누리 제3유형(출처표시·변경금지) 조건 때문에 멈췄다. 다시 켜는 것은 소유자 결정 |
| localdata 인허가 (100+종) | 매일 shard 로테이션 | Incremental | `0 19 * * *` | 04:00 KST | 타입별 주 1회, 월 1회 reconcile |
| 서울 따릉이 월별 이용 | 매월 5일 | Append-only | `0 1 5 * *` | 10:00 KST | |

## Backfill vs Ongoing

| 구분 | 방식 |
|---|---|
| **Ongoing (scheduled)** | 위 cron에 따라 자동 실행. hot window만 처리. |
| **Backfill (manual)** | `workflow_dispatch`로 `dataset`, `start_date`, `end_date`, `shard` 파라미터 지정하여 수동 실행. |

## Escalation 기준

- 대기오염/날씨가 sub-hour 수준의 freshness를 요구하면 → GitHub Actions 대신 외부 스케줄러 고려.
- localdata daily shard가 API 일일 한도 초과 시 → 타입 그룹 분할, per-type 주기 축소.
- 업스트림이 과거 이력을 소급 수정하는 경우 → reconcile window 확대.

## 모니터링

각 workflow run에서 다음을 기록:
- API 호출 수 (일일 한도 대비 %)
- Push된 파일 크기 / row count
- 실패 시 자동 retry (최대 2회), 이후 Slack/issue 알림

## GitHub Actions 운영 가드레일

- 모든 `scheduled-*.yml`은 `permissions: contents: read`를 기본값으로 두고, 실패 알림 job에만 `issues: write`를 부여한다.
- append-only 데이터셋(대기오염/날씨)은 `concurrency.cancel-in-progress: false`로 이전 실행을 취소하지 않고 직렬화해 중복 append와 부분 publish를 피한다.
- 데이터셋 publish job은 reusable workflow의 `timeout_minutes` 입력으로 45~60분 제한을 둔다. 긴 backfill은 scheduled workflow가 아니라 `workflow_dispatch`로 별도 실행한다.
- scheduled workflow가 실패하면 `fix(freshness): <workflow> failed` 이슈가 생성되며, run URL을 기준으로 원인 확인 후 backfill 여부를 결정한다. 같은 workflow 의 이슈가 열려 있으면 새로 만들지 않고 그 이슈에 run URL 을 댓글로 단다. 알림을 남기지 못하면 `notify-failure` 작업이 실패로 표시된다(`.github/workflows/freshness-incident.yml`, #1143).
- GitHub는 저장소 활동이 60일 이상 없으면 scheduled workflow를 비활성화할 수 있다. 월 1회 이상 수동 `workflow_dispatch` 또는 운영 변경 PR로 schedule 활성 상태를 확인한다.
- `KPUBDATA_DATAGO_API_KEY`, `HF_TOKEN`, Kaggle credentials는 GitHub Environment 보호 규칙 아래에서 관리하고, 분기별 로테이션 결과를 운영 이슈에 기록한다.
- Kaggle scheduled publish는 현재 비활성이다. dataset config에 `kaggle_slug`가 있고 Environment에 `KAGGLE_USERNAME`/`KAGGLE_KEY`가 등록된 데이터셋만 별도 scheduled workflow에서 활성화한다.

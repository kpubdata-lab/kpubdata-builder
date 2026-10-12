# ADR 0024 — 파생 snapshot: 입력 snapshot 을 고정하고 계보를 카탈로그에 남긴다

- 상태: 제안됨(Proposed) — 소유자 검토 대기. 구현은 이 ADR 이 승인된 뒤 별도 PR 로 한다
- 관련 이슈: #821 (이 ADR), #822 (행 키로 셀 수정), kpubdata-studio#502 (수정 검토 화면),
  #704 (다중 테이블 SQL), #611 (Silver 변환 규칙), #705 (hold·백업), #1099·#1276 (run 보관기한),
  #1095·#1275 (전체 상태 백업), #1102·#1279 (사용자 삭제)
- 관련 문서: [ADR 0021 — DuckDB 단일 tabular 엔진](./0021-duckdb-tabular-engine.md),
  [ADR 0014 — Source 통합 경계](./0014-source-ingestion-file-url-boundary.md),
  `src/kpubdata_builder/warehouse/` (`catalog.py`, `materialize.py`, `gc.py`, `backup.py`, `layout.py`)

이 문서의 "지금" 은 `main` 의 `cf314ae` (API 계약 1.121.0) 이다. 아래에서 인용하는 함수와 숫자는 모두
그 커밋의 코드에서 읽은 것이다.

## 맥락

### 무엇이 없나

사용자가 이미 커밋된 테이블을 입력으로 새 테이블(파생 테이블)을 만들 방법이 없다.

- `spec/models.py` 의 `SOURCE_KINDS` 는 `("public_api", "file", "url")` 이다. BuildSpec 의 source 는
  원천 API·업로드 파일·URL 중 하나뿐이고, 커밋된 snapshot 을 읽는 source 는 없다.
- 카탈로그 스키마(`warehouse/catalog.py`, `SCHEMA_VERSION = 5`)의 네 테이블 — `tables`,
  `table_snapshots`, `snapshot_leases`, `snapshot_holds` — 어디에도 "이 snapshot 은 어느 snapshot 에서
  만들어졌다" 를 담는 자리가 없다. `table_snapshots.run_id` 는 그 snapshot 을 만든 **run** 을 가리킬 뿐이다.

### 지금 snapshot 을 지키는 장치

| 장치 | 코드 | 수명 | 무엇이 푸나 |
| :--- | :--- | :--- | :--- |
| current 포인터 | `tables.current_snapshot_id`, `begin_retiring` 이 current 를 거부 | 다음 커밋까지 | 다른 snapshot 의 커밋(CAS) |
| lease | `resolve_current` / `pin`, `snapshot_leases` | `DEFAULT_LEASE_SECONDS = 3600` 뒤 만료 | `release`, 만료, `purge_expired_leases` |
| hold | `place_hold`, `snapshot_holds`, 종류 `saved_analysis`·`retention`·`audit` | 풀 때까지 또는 `expires_at` | `release_hold` |

GC 는 세 가지를 **한 트랜잭션 안에서** 확인한다. `gc.delete_snapshot` → `TableCatalog.begin_retiring` 이
`BEGIN IMMEDIATE` 안에서 current·live lease·live hold 를 보고, 하나라도 있으면 `ImmutableSnapshot`·
`SnapshotInUse`·`SnapshotHeld` 를 던지며, 없으면 상태를 `retiring` 으로 바꾼다. `retiring` 에는 새 lease 도
hold 도 붙지 않는다(`pin`, `place_hold` 가 거부). 이 "검사와 전이가 한 트랜잭션" 이라는 성질이 이 ADR 이
기대는 바닥이다.

### 그대로 쓰면 깨지는 곳

파생 run 이 입력을 지금의 장치로만 지키려고 하면 다음이 깨진다.

1. **lease 는 build 보다 짧다.** lease 기본 수명은 1시간(`DEFAULT_LEASE_SECONDS`)이고, build 시간 한도의
   기본값은 6시간(`service/build_limits.py`, `DEFAULT_BUILD_TIME_LIMIT_SECONDS`)이다. 1시간이 넘는 파생
   run 의 입력은 run 도중에 GC 대상이 된다.
2. **hold 는 커밋 전 snapshot 에 붙지 않지만, 입력에 붙이면 누가 풀지 모른다.** `place_hold` 는
   committed·quarantined 만 받는다(입력은 그 조건을 채운다). 그러나 run 이 crash 하면 hold 를 풀 주체가
   없다. 저장된 분석은 분석 행과 hold 를 함께 지우는 코드(`analyses_api.delete`,
   `user_deletion._step_analyses`)가 있어서 버티지만, 파생 run 에는 그런 짝이 없다.
3. **계보 없이는 게시 판단이 틀린다.** 게시·내보내기의 이용 조건 판정(`service/redistribution.py`)은
   run 의 BuildSpec 에 적힌 source 의 조건을 본다(`read_snapshot_spec` → `build_verdict`). 입력이
   snapshot 인 source 는 그 자체로 조건이 없으므로, 계보를 따라가지 않으면 `forbidden` 데이터에서 만든
   파생 테이블이 `unknown` 으로 읽힌다.

## 결정

### 1. 입력은 snapshot id 로 고정한다

- 새 source 종류 `snapshot` 을 둔다. 사용자는 테이블 이름(같은 workspace 의 `logical_name`)과
  `current` 또는 snapshot id 를 적는다.
- **run 이 시작할 때 한 번** 해석한다. `current` 는 포인터를 읽어 snapshot id 로 바꾸고, 이후 그 run 은
  그 id 만 본다. snapshot id 를 직접 적었으면 그 id 가 그 테이블의 것인지 확인한다 —
  `service/warehouse_api._pin` 이 이미 하는 검사(`known_snapshot_ids`)와 같다.
- 해석, 파생 snapshot 행 등록(`staging`), 입력 간선 기록은 **한 `BEGIN IMMEDIATE` 트랜잭션** 이다
  (§2 의 `begin_snapshot(..., inputs=...)`). 그래서 "current 를 읽은 뒤 GC 가 그것을 지우는" 틈이 없다.
- 해석한 id 와 입력의 `artifact_digest` 는 run 의 이벤트와 manifest 에 남는다. **재시도(`retry_of`)는
  원래 run 이 해석한 id 를 그대로 쓰고 `current` 를 다시 해석하지 않는다.** 그 사이 입력이 GC 되었으면
  재시도는 `input_unavailable` 로 실패한다 — 다른 입력으로 조용히 바꾸지 않는다.
- 같은 입력 id 와 같은 레시피는 같은 결과를 만들어야 한다(결정성, §5). 그 판정 기준이
  `content_digest` 의 일치다.

### 2. 계보는 카탈로그의 간선 테이블에 둔다

카탈로그가 canonical 이므로(`catalog.py` 모듈 docstring) 계보도 카탈로그에 둔다. 스키마 버전 6 의
마이그레이션으로 테이블 하나를 더한다(`_MIGRATIONS[5]`, 기존 행은 건드리지 않음).

```text
snapshot_inputs
  snapshot_id        TEXT  파생 snapshot          REFERENCES table_snapshots(id)
  ordinal            INT   입력 순서(0부터)
  input_snapshot_id  TEXT  입력 snapshot          REFERENCES table_snapshots(id)
  input_table_id     TEXT  입력 테이블
  input_digest       TEXT  등록 시점의 입력 artifact_digest
  PRIMARY KEY (snapshot_id, ordinal)
  INDEX (input_snapshot_id)
```

- 간선은 파생 snapshot 이 `staging` 으로 등록되는 그 트랜잭션에서 쓴다. 입력이 committed·quarantined 가
  아니거나 `retiring` 이면 등록 자체를 거부한다(`SnapshotStateError`, 서비스 코드 `input_unavailable`).
- **레시피와 엔진은 간선이 아니라 파생 snapshot 쪽에 둔다.** `table_snapshots` 에 `recipe_digest`
  (정규화한 레시피 JSON 의 SHA-256)와 `engine`(`duckdb` 와 버전) 열을 더한다. 레시피 본문은 run 의
  BuildSpec snapshot(`_read_snapshot_yaml` 이 읽는 파일)에 이미 있으므로 카탈로그에 두 번 쓰지 않는다.
- `_snapshot.json`(`SnapshotManifest`)에 `inputs` 와 `recipe_digest` 를 **선택 필드** 로 더한다. 카탈로그를
  디스크에서 다시 세울 때 계보도 함께 서도록 하기 위해서다. 지금 `from_json` 은 모든 필드를 `data[field]`
  로 읽으므로, 새 필드는 기본값을 두고 없으면 기본값으로 읽어야 기존 manifest 가 깨지지 않는다.
- 열 단위 출처(§6 의 Studio 요구)도 manifest 에 둔다: 출력 열마다 `{"from": "<입력 열>"}` 또는
  `{"derived": "<op>"}`. 카탈로그에는 두지 않는다 — 질의하지 않고 읽기만 하는 값이다.

### 3. 간선이 곧 보호다 — hold 행을 따로 만들지 않는다

**파생 snapshot 이 살아 있는 동안, 그 입력은 GC 되지 않는다.** "살아 있다" 는 파생 snapshot 의 상태가
`staging`·`validated`·`committed`·`quarantined` 중 하나라는 뜻이다. `abandoned`·`retiring` 인 파생
snapshot 의 간선은 보호하지 않는다.

- `begin_retiring`·`assert_deletable`·`detach_current` 의 같은 트랜잭션 안에 검사 하나를 더한다:
  `snapshot_inputs` 에서 이 snapshot 을 입력으로 삼는 살아 있는 파생 snapshot 이 있으면 거부한다.
  예외는 새 `SnapshotReferenced(WarehouseError)` 로, 메시지는 파생 snapshot id 와 그 테이블 이름을 댄다
  (hold 의 `reason` 처럼, 사람이 무엇을 지워야 풀리는지 알 수 있게).
- `forget_snapshot` 은 그 snapshot 이 **파생 쪽** 인 간선을 함께 지운다. 입력 쪽 간선이 남은 snapshot 은
  `begin_retiring` 이 이미 거부했으므로 여기 오지 않는다.
- 이것으로 issue 가 말한 세 가지가 따로 코드 없이 나온다.
  - **실행 중 보호**: 간선은 run 시작에 `staging` 행과 함께 생기므로 lease 수명(1시간)과 무관하다.
  - **실패·취소 시 정리**: 실패·취소 경로는 지금처럼 파생 snapshot 을 `abandon` 한다
    (`materialize` 의 `except (SnapshotConflict, SnapshotStateError)` 와 같은 자리). `abandoned` 간선은
    보호하지 않으므로 그 순간 입력이 풀린다. crash 로 아무도 `abandon` 하지 못한 경우는
    `gc.abandon_stale` 이 `--stale-hours`(기본 24시간) 뒤에 표시한다.
  - **체인 보호**: C 가 B 에서, B 가 A 에서 파생되면 C→B 간선이 B 를 지키고, B 가 살아 있으므로 B→A
    간선이 A 를 지킨다. 체인 전체를 따라가는 질의는 필요 없다.
- 저장된 분석이 파생 snapshot 을 읽으면 지금처럼 그 snapshot 에 `saved_analysis` hold 가 붙고
  (`analyses_api.create`), 그 snapshot 이 살아 있으므로 간선이 입력을 지킨다. 분석 쪽 코드는 바뀌지 않는다.

hold 행으로 표현하는 대안(§대안 B)을 버린 이유: 같은 사실(이 snapshot 이 저 snapshot 에서 왔다)이 간선과
hold 두 곳에 살게 되고, 둘 중 하나만 지워진 상태가 생긴다. 분석 hold 가 그 문제를 막으려고
"hold 먼저 풀고 행을 지운다" 는 순서 규칙을 두 군데(`analyses_api`, `user_deletion`)에 갖고 있는 것이 근거다.

**불변식 하나**: `--stale-hours` 는 build 시간 한도보다 길어야 한다. 지금 기본값은 24시간 > 6시간으로
성립한다. 짧게 설정하면 느린 파생 run 의 `staging` 행이 실행 도중 `abandoned` 가 되고, 커밋은
`mark_validated` 의 상태 검사에서 `SnapshotStateError` 로 실패한다 — 데이터가 틀리지는 않지만 run 은
헛돈다. 구현은 `warehouse-gc` 가 이 두 값을 비교해 경고하도록 한다.

### 4. 기존 정리·백업·삭제와의 관계

**`warehouse-gc` 와 build 안의 회수(`orchestrator._reclaim`).** `gc.collect` 는 `committed[keep:]` 를
돌며 `delete_snapshot` 을 부르고 예외 종류로 보고서를 채운다. `SnapshotReferenced` 를 받으면 새 목록
`GCReport.kept_referenced` 에 넣고, `warehouse-gc` 출력에 `kept N referenced` 를 더한다.
결과적으로 입력 테이블은 `keep` 보다 많은 snapshot 을 들고 있을 수 있다. 그 상한은 "입력 테이블의
keep + 그 테이블을 입력으로 쓰는 파생 테이블들이 아직 가진 snapshot 이 가리키는 서로 다른 입력 수" 이고,
파생 테이블도 `keep` 으로 회수되므로 유한하다. **단, 테이블이 자기 자신의 이전 snapshot 을 입력으로 삼으면
이 상한이 사라진다**(각 snapshot 이 바로 앞 것을 지켜 사슬이 끝없이 남는다). 그래서 v1 은 입력 테이블과
출력 테이블이 같은 것을 거부한다(`self_input_forbidden`). #822 는 이 규칙과 부딪힌다(§6, 미결 4).

**`prune-runs` (#1276, `retention.py`).** run 은 그 run 이 만든 committed snapshot 이 있는 동안
`snapshot` 이유로 남는다(`_References.reason` 이 `committed_by_run` 을 본다). 간선이 입력 snapshot 을
지키므로 입력을 만든 run 도 따라서 남는다. 파생 run 자신도 파생 snapshot 이 있는 동안 남는다 — 내보내기가
파생 run 의 BuildSpec(레시피)을 읽어야 하므로 필요한 성질이다. 실패한 파생 run 의 재시도 원본은 지금처럼
`retry_source` 로 남는다. 새로 필요한 `KeepReason` 은 없다.

**백업과 복구 (#705 `warehouse/backup.py`, #1275 `store/state_backup.py`).**

- `backup` 은 카탈로그를 SQLite 온라인 backup 으로 통째로 복사하므로 `snapshot_inputs` 도 따라온다.
  읽을 수 없는 snapshot(`staging`·`validated`·`abandoned`·`retiring`)의 행과 hold 를 지우는 자리에서
  **그 snapshot 이 파생 쪽인 간선도 지운다.** 입력 쪽 간선은 생길 수 없다(입력은 언제나 committed·quarantined).
- `verify_backup` 에 검사 셋을 더한다: 간선의 양 끝이 백업 안에 있다 / 입력이 읽을 수 있는 상태다 /
  `input_digest` 가 입력 snapshot 의 `artifact_digest` 와 같다. 하나라도 어긋나면 지금처럼
  `BackupInvalid` 로 전체를 거부한다.
- 전체 상태 백업은 배타 상태 잠금(`store/state_lock.py`)을 쥐므로 실행 중인 파생 run 은 백업에 들어오지
  않는다. `warehouse-backup` 단독 명령은 잠금 없이 돌 수 있지만, 실행 중인 파생 snapshot 은 `staging` 이라
  위 규칙대로 빠진다.
- 복구 뒤 lease 는 없고 간선은 있으므로, 복구된 웨어하우스에서도 보호가 그대로 성립한다. 이것이 보호를
  lease 가 아니라 간선에 둔 또 하나의 이유다(백업은 lease 를 버린다).

**사용자 삭제 (#1279, `service/user_deletion.py`).**

- `_step_warehouse` 는 사용자의 테이블을 `sorted(own_tables)` 순서로 지운다. 파생 테이블이 같은 사용자의
  다른 테이블을 입력으로 쓰면, 입력 테이블 차례에 파생 snapshot 이 아직 남아 `SnapshotReferenced` 로 막힌다.
  그래서 지우는 순서를 **간선의 역 위상 순서**(파생 먼저, 입력 나중)로 바꾼다. 지우는 집합 안의 간선은
  `_own_holds` 처럼 "곧 함께 사라질 것" 으로 셈해 dry run 의 막힘 이유에서 뺀다.
- 다른 사용자의 파생 snapshot 이 이 사용자의 snapshot 을 입력으로 삼고 있으면 막고 이유를 적는다 — 지금
  "공유 테이블의 current snapshot" 을 막는 것과 같은 방식이다. v1 은 입력을 같은 workspace 로 제한하므로
  다중 사용자 배포에서는 이 경우가 생기지 않지만, 소유권을 강제하지 않던 시절의 데이터에는 생길 수 있다.
- 복구 뒤 삭제 재실행(`state_backup` 의 deleted-users 경로)은 같은 단계 함수를 부르므로 따로 바꿀 것이 없다.

**게시와 이용 조건.** 파생 run 의 이용 조건 판정은 `source.kind == "snapshot"` 을 만나면 그 입력 snapshot 의
run 을 따라가 그 run 의 판정을 쓰고, 입력들 중 가장 제한적인 것을 택한다(`forbidden` > `unknown` >
`non_commercial` > `allowed`, `redistribution.py` 의 기존 순서). 입력 run 의 BuildSpec 을 읽을 수 없으면
`unknown` 이다 — 모르는 것을 허락으로 읽지 않는다는 기존 원칙 그대로. **v1 은 사용자 파생 결과의 게시를
막는다.** 판단은 `source.kind` 가 아니라 "이 snapshot 에 입력 간선이 있다" 로 한다. 내보내기(`exports_api`)
manifest 에는 snapshot id 옆에 입력 id 와 digest 를 함께 적는다.

**개인정보.** 입력은 Gold 이므로 선언된 PII 열은 이미 가려져 있다. 파생 source 의 `publish_unmasked` 는
뜻이 없으므로 거부한다 — 가려진 값을 파생으로 다시 드러낼 수 없다.

### 5. API 모양 (계약 수준의 스케치 — 구현 아님)

**BuildSpec.** 기존 BuildSpec·검증·`POST /builds`(비동기 job, ADR 0008)·`materialize` 를 그대로 쓴다.
원천 수집(Bronze 의 API·파일·URL 읽기)은 없다. Bronze 는 고정된 입력 snapshot 의 `table.parquet` 을 읽는
것으로 대신하고, Silver 의 `SchemaContract` 검증과 Gold 의 `GoldSelection` 은 지금처럼 돈다.

```yaml
sources:
  - kind: snapshot
    alias: rentals_2026
    table: seoul_bike.rentals        # 같은 workspace 의 logical_name
    snapshot: current                # 또는 snap_xxxxxxxxxxxxxxxx; run 시작에 id 로 고정
    recipe:                          # 순서대로 적용, 타입이 있는 연산만
      - op: filter                   # GoldFilter 와 같은 연산자 집합(eq, ne, gt, ge, lt, le, in, not_null)
        column: rent_date
        operator: ge
        value: "2026-01-01"
      - op: select
        columns: [station_id, rent_date, count]
      - op: derive                   # 허용된 AST 만. eval 없음
        name: count_per_hour
        expr: {div: [{col: count}, {lit: 24}]}
      - op: dedupe
        by: [station_id, rent_date]
        order_by: [{column: count, direction: desc}]
        keep: first
        nulls: last
```

v1 의 연산은 issue 대로 `filter`, `select`, `drop`, `derive`, `map_values`, `dedupe`, `pivot`, `unpivot`
이다. v1 은 입력 하나만 받는다(다중 입력과 JOIN 은 #704). 이름 바꾸기와 형 변환은 레시피에 두지 않고
기존 `SchemaContract.rename`·`casts` 를 쓴다 — 같은 일을 두 곳에서 하게 하지 않기 위해서다(미결 3).

**결정성.** 같은 입력 id, 같은 `recipe_digest`, 같은 엔진 버전이면 같은 `artifact_digest` 가 나와야 한다.
DuckDB 의 병렬 실행은 행 순서를 보장하지 않으므로, 파생 run 은 출력을 결정적인 순서로 쓴다(레시피에 정렬이
없으면 출력의 모든 열로 정렬). 테스트는 같은 run 을 두 번 돌려 `artifact_digest` 가 같음을 확인한다.

**카탈로그 (Python, `warehouse` 패키지).**

```text
SnapshotInput(input_snapshot_id, input_table_id, input_digest, ordinal)   frozen dataclass

TableCatalog.begin_snapshot(..., inputs: Sequence[SnapshotInput] = (),
                            recipe_digest: str | None = None,
                            engine: str | None = None) -> SnapshotRow
    입력이 읽을 수 없거나 retiring 이면 SnapshotStateError. 간선은 같은 트랜잭션.
TableCatalog.begin_derivation(table_id, *, input_table_id, input_snapshot="current", ...)
    -> tuple[SnapshotRow, SnapshotInput]
    current 해석 + begin_snapshot(inputs=...) 를 한 트랜잭션으로.
TableCatalog.inputs_of(snapshot_id) -> list[SnapshotInput]
TableCatalog.dependents_of(snapshot_id, *, live_only=True) -> list[SnapshotRow]
SnapshotReferenced(WarehouseError)
GCReport.kept_referenced: list[str]
materialize(..., snapshot=<이미 등록한 staging 행>)   등록을 run 시작으로 옮기기 위한 인자
```

**HTTP (추가만, 계약 minor 증가).**

```text
GET /warehouse/tables/{name}/snapshots/{snapshot_id}/lineage
200 {
  "snapshot_id": "...", "run_id": "...",
  "recipe_digest": "sha256:...", "engine": "duckdb 1.x",
  "inputs":     [{"table": "...", "snapshot_id": "...", "digest": "sha256:...", "available": true}],
  "dependents": [{"table": "...", "snapshot_id": "...", "state": "committed"}],
  "columns":    {"count_per_hour": {"derived": "derive"}, "station_id": {"from": "station_id"}}
}
```

`dependents` 는 "왜 이 snapshot 이 지워지지 않는가" 에 답한다. 새 오류 코드:

| 코드 | 언제 |
| :--- | :--- |
| `input_not_found` | 입력 테이블이나 snapshot 이 그 workspace 에 없다 |
| `input_unavailable` | 입력이 retiring·abandoned 이거나, 재시도 때 이미 회수되었다 |
| `self_input_forbidden` | 입력 테이블과 출력 테이블이 같다 |
| `recipe_invalid` | 레시피 검증 실패. 위치(`sources[i].recipe[j]`)를 함께 준다 |
| `derived_publish_forbidden` | v1 에서 입력 간선이 있는 snapshot 을 게시하려 했다 |

### 6. 실패 방식

| 상황 | 결과 | 입력 보호 |
| :--- | :--- | :--- |
| 입력 해석 전에 GC 가 입력을 `retiring` 으로 만듦 | 등록 거부, `input_unavailable` | 생기지 않음 |
| 등록 뒤 GC 가 입력을 지우려 함 | `begin_retiring` 이 `SnapshotReferenced` 로 거부 | 유지 |
| 레시피 실행 실패, 검증 실패, 빈 결과 거부(#1186) | run 실패, 파생 snapshot `abandon` | 그 순간 풀림 |
| 사용자 취소, 시간 한도(#1229) | 위와 같음 | 풀림 |
| 프로세스 crash | `staging` 행이 남음 → `abandon_stale` 이 24시간 뒤 표시 | 표시될 때까지 유지 |
| 출력 테이블의 CAS 패배(#787) | `materialize` 가 `abandon` 후 `SnapshotConflict`, 재시도 안 함 | 풀림 |
| 결과 digest 가 같은 입력·레시피의 이전 결과와 다름 | 결정성 위반. v1 은 기록만 하고 실패시키지 않음(미결 5) | 영향 없음 |
| 입력 manifest 의 digest 와 파일이 다름 | 읽기 전에 `content_digest` 로 확인, run 실패 | 풀림 |

### 7. #822 와 Studio #502 가 이 ADR 에 바라는 것

#822(행 키로 셀 수정)는 "기준 snapshot 에 수정 집합을 적용해 새 snapshot 을 만든다" 이므로 파생 run 의
한 종류다. 필요한 것:

1. **기준 snapshot 의 고정과 보존.** 수정 항목의 `expect` 는 기준 snapshot 의 값과 비교된다. 기준이 GC 되면
   `already_satisfied` 와 `conflict` 를 가를 수 없다. §3 의 간선이 이것을 준다.
2. **snapshot 이 아닌 입력.** 수정 집합은 `revisions` 저장소(#820, `service/revisions.py`)의 불변 리비전이다.
   간선 테이블은 snapshot 끼리만 잇는다. 수정 집합은 `(kind, doc_id, revision)` 을 파생 snapshot 의 열
   (또는 manifest)로 적는 것으로 충분하다 — 리비전은 고쳐 쓰지 않으므로 보호가 필요 없다. 다만 사용자 삭제가
   리비전 문서를 지우면 계보의 그 부분은 "삭제됨" 으로 읽힌다.
3. **확정 전 대기.** #822 는 해결되지 않은 conflict·orphan 이 있으면 결과를 `current` 로 확정하지 않는다.
   지금 `materialize` 는 promote 직후 곧바로 커밋한다. "validated 로 멈추고 사용자의 확정을 기다리는" 단계가
   필요하고, 그 동안 `abandon_stale` 이 그것을 crash 로 오인하지 않아야 한다(미결 4).
4. **같은 테이블에 대한 파생.** 셀 수정은 보통 "같은 테이블의 새 버전" 으로 기대된다. §4 의
   `self_input_forbidden` 과 정면으로 부딪힌다(미결 4).

Studio #502 가 이 ADR 의 API 에서 읽을 것:

- `lineage.inputs` — 어느 기준 snapshot 위에서 계산했는지, 그것이 아직 있는지(`available`).
- `lineage.columns` — 편집 가능한 셀을 원천 열에 대응하는 셀로 제한하는 근거. `derived` 인 열은 편집 불가.
  키 열과 집계 셀은 #822 의 키 계약과 질의 결과가 정한다.
- `dependents` — 삭제·철회가 막힐 때 그 이유.
- 확정 호출은 기존 커밋과 같은 `expected_revision`(#787)을 받아, 검토하는 동안 다른 커밋이 들어왔으면
  조용히 덮지 않고 거부한다.

## 검토한 대안

**A. lease 를 갱신(heartbeat)하며 실행 중 입력을 지킨다.** 갱신을 놓치면(GC 지연, 프로세스 멈춤) 보호가
조용히 사라지고, 백업이 lease 를 버리므로 복구 뒤에는 보호가 없다. 커밋 뒤의 지속 보호는 따로 또 필요하다.
버림.

**B. 입력에 `lineage` 종류의 hold 를 붙인다.** `HOLD_KINDS` 에 하나를 더하면 GC·`prune-runs`·사용자 삭제가
이미 hold 를 이해하므로 코드가 적다. 그러나 hold 를 풀 책임이 파생 snapshot 의 삭제·포기·crash 경로마다
생기고, 하나라도 빠지면 입력이 영원히 남는다. 간선에서 보호를 계산하면 풀 것이 없다. 버림. 다만
"보고서에서 hold 처럼 보이게" 는 `kept_referenced` 로 따로 보여 준다.

**C. 입력 파일을 파생 snapshot 안에 복사해 두고 보호하지 않는다.** 저장량이 입력 크기만큼 늘고, #822 가
요구하는 "기준 snapshot 과의 비교" 를 같은 id 로 할 수 없다. 버림.

**D. 계보를 run manifest 에만 둔다.** `prune-runs` 가 run 을 지우면 계보가 사라지고, GC 는 run manifest 를
읽지 않는다. 카탈로그가 canonical 이라는 원칙과도 어긋난다. 버림.

## 미결 사항 — 소유자 결정이 필요한 것

1. **커밋 뒤에도 입력을 지킬 것인가.** 이 ADR 은 "파생 snapshot 이 살아 있는 동안" 지킨다(issue 의 제안).
   대안은 "실행 중에만 지키고, 커밋 뒤에는 계보만 남겨 `available: false` 로 보인다" 이다. 앞쪽은 재현성과
   #822 의 비교를 주고, 뒤쪽은 입력 테이블의 디스크를 `keep` 안에 묶는다.
2. **`current` 를 언제 해석할 것인가.** 이 ADR 은 run 시작이다(틈 없음, 구현 단순). 제출 시점으로 하면 사용자가
   본 것이 곧 결과가 되지만, 대기 중인 job 의 입력을 따로 지켜야 하고 대기 job 취소가 그것을 풀어야 한다.
3. **레시피와 `SchemaContract` 의 경계.** 이 ADR 은 rename·cast 를 `SchemaContract` 에만 둔다. 레시피의
   `derive` 와 `SchemaContract.derived`(지금 `date_parts`·`join_key` 두 종류)를 하나로 합칠지, `derive` 가
   허용할 AST 노드 목록(산술, 비교, 문자열 결합, 날짜 부분, `coalesce` 등)을 어디까지로 할지.
4. **같은 테이블 파생과 확정 대기.** `self_input_forbidden` 을 유지하고 #822 의 결과를 별도 테이블로 낼지,
   같은 테이블을 허용하되 보호를 "직전 하나" 로 제한할지. 그리고 확정을 기다리는 결과의 상태 — 새 상태
   (`pending_confirmation`)를 더할지, `validated` 에 만료가 있는 hold 를 붙일지, 대기 기한은 얼마인지.
5. **결정성 위반의 처리.** 같은 입력·레시피·엔진에서 digest 가 다르면 실패시킬지, 기록만 할지(이 ADR 의 v1).
   엔진 버전이 바뀐 경우를 같은 레시피로 볼지.
6. **게시 금지를 언제 풀 것인가.** v1 은 입력 간선이 있는 snapshot 의 게시를 막는다. 풀 때의 조건(입력의
   이용 조건이 모두 `allowed` 이고 사람이 확인했을 때 등).
7. **workspace 를 넘는 입력.** v1 은 같은 workspace 만. 공유 테이블이 생기면 다른 사람의 파생이 내 snapshot 을
   지키게 되고, 사용자 삭제가 그것에 막힌다. 그때의 우선순위(삭제가 이기는가, 파생이 이기는가).

## 영향

- 카탈로그 스키마 6(마이그레이션 하나, 기존 행 보존), `SnapshotManifest` 의 선택 필드 셋, `SOURCE_KINDS` 에
  `snapshot` 하나. Builder API 계약은 추가만 하므로 minor 증가.
- GC·백업·사용자 삭제는 새 예외 하나와 검사 몇 줄을 받는다. `prune-runs` 와 저장된 분석은 바뀌지 않는다.
- 입력 테이블의 디스크 사용량은 그것을 쓰는 파생 테이블 수만큼 `keep` 을 넘을 수 있다.

## 검증 (구현 PR 이 보일 것)

- 결정성: 같은 입력 id·레시피로 두 번 돌린 결과의 `artifact_digest` 가 같다.
- 경쟁: GC 의 `begin_retiring` 과 파생 등록을 여러 스레드로 엇갈려 돌려, "입력이 지워졌는데 파생이 커밋됨" 이
  한 번도 없다. 두 순서 각각의 결과(`input_unavailable` / `SnapshotReferenced`)가 나온다.
- 정리: 실패·취소·CAS 패배·crash(`abandon_stale`) 네 경로 뒤에 입력이 다음 GC 에서 회수된다.
- 체인: A→B→C 에서 C 가 있는 동안 A 가 남고, C 와 B 를 회수한 뒤 A 가 회수된다.
- 백업: 간선이 있는 웨어하우스의 왕복 복구, 그리고 간선의 `input_digest` 를 바꾼 백업이 거부된다.
- 사용자 삭제: 같은 사용자의 입력·파생 테이블이 순서 문제 없이 지워진다.
- 이용 조건: `forbidden` 입력에서 만든 파생 snapshot 의 내보내기·게시가 거부된다.

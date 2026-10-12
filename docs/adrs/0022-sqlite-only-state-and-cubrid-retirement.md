# ADR 0022 — 상태는 SQLite 와 로컬 파일에만 둔다: CUBRID 백엔드를 잃는 것 없이 퇴역시킨다

- 상태: 승인됨(Accepted) — 2026-10-12 소유자 승인(#1093). [ADR 0016](./0016-cubrid-state-backend.md) 을 대체한다
- 관련 이슈: #1093 (이 ADR), #1094 (CUBRID 백엔드 제거 — 이 ADR 이 정한 계약을 구현한다),
  #1095 (한 번의 백업으로 전체 상태 복구), kpubdata-lab/kpubdata#812 (메인테이너 결정 모음)
- 관련 문서: [ADR 0003](./0003-persistent-build-store.md)(manifest 가 정본),
  [ADR 0010](./0010-artifactstore-state-backend.md)(저장소 추상화),
  [ADR 0012](./0012-provider-credential-boundary.md)(자격 증명 경계),
  [ADR 0016](./0016-cubrid-state-backend.md)(CUBRID 상태 백엔드),
  [ADR 0017](./0017-fullstack-oci-deployment.md)(단일 VM 배포), [deploy.md §6](../deploy.md)

## 맥락

### ADR 0016 의 전제

ADR 0016 은 "조직 요구사항으로" Builder 의 영속 상태를 CUBRID 로 옮기는 백엔드를 들였다. 스케일링이
목적이 아니었고, CUBRID 를 쓰는 것 자체가 요구사항이었다. 그 백엔드를 고르면(`KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid`)
CUBRID 에는 세 가지가 들어간다.

| CUBRID 표 | 내용 | SQLite 백엔드에서의 자리 |
|---|---|---|
| `manifests` | run 마다 manifest 문서 한 행. **ADR 0016 은 이 행을 정본으로, `manifest.json` 파일을 미러로 정했다** | `<output>/<run_id>/manifest.json` — 정본 |
| `builds`(+ 버전 표) | 빌드 인덱스. manifest 에서 뽑은 조회용 표 | `<output>/_builds.sqlite` — 파생물 |
| `provider_credentials` | 사용자가 저장한 provider 키의 암호문 | `<output>/.service/provider-credentials.sqlite3` |

나머지 상태(run 이벤트와 종료 기록, 게시 영수증, 업로드, revision, 가입 원장, 저장한 분석,
테이블 카탈로그, 연결 테스트 결과)와 산출물 바이트는 **어느 백엔드에서나** 로컬 파일과 SQLite 에 있다
(`src/kpubdata_builder/store/inventory.py`, deploy.md §6.0.1).

### 그 전제가 없어졌다는 근거

- **소유자의 결정(2026-10-08).** 소유자가 2026-10-08 에 "Builder 는 CUBRID 를 쓰지 않는다. 운영 상태는
  SQLite 와 로컬 파일이다. 다른 데이터베이스는 나중에 검토할 수 있다"고 밝혔다. 이것은 소유자의 진술이며
  저장소에 남은 문서가 아니다. **이 ADR 의 승인이 그 결정을 저장소에 기록하는 절차다** — 승인 기록은 아래
  "승인 기록" 절에 남긴다.
- **참조 배포는 이미 SQLite 로만 돈다.** 단일 VM 배포(ADR 0017)의 `docker-compose.prod.app.yml` 은
  `KPUBDATA_BUILDER_STORAGE_BACKEND` 와 `KPUBDATA_BUILDER_CUBRID_URL` 을 컨테이너에 넘기지 않는다.
  이 사실은 `tests/unit/test_prod_compose_settings.py` 의 `NOT_PASSED` 목록이 고정하고 있고(#1168),
  deploy.md 의 같은 목록이 "CUBRID 백엔드는 퇴역 예정"이라고 적는다.

### 그냥 지우면 잃는 것

ADR 0016 의 `CubridArtifactStore.put_manifest` 는 행을 먼저 커밋하고 파일을 나중에 쓴다. 조회
(`get_manifest`)는 행을 먼저 본다. 그래서 다음 경우에 **가장 최신의 읽을 수 있는 manifest 가 CUBRID 에만**
남고, CUBRID 로 도는 동안은 행이 그것을 가리기 때문에 아무도 알아채지 못한다.

- 미러 쓰기가 실패한 경우. 파일 쓰기(`write_text`)는 원자적이지 않아서, 디스크가 차는 등으로 중간에 실패하면
  이미 있던 `manifest.json` 이 잘리거나 비어 있게 된다.
- `/data` 의 run 디렉터리를 운영 중에 잃거나 이전 백업으로 되돌린 경우. 행은 남고 파일은 없거나 예전 것이다.

따라서 CUBRID 를 떠날 때는 **별도의 이관(대조와 복구)이 필요하다.** 이 ADR 은 "별도의 이관은 필요 없다"고
보지 않는다.

## 결정

### 1. 상태는 SQLite 와 로컬 파일에만 둔다

| 무엇 | 어디 | 성격 |
|---|---|---|
| manifest | `<output>/<run_id>/manifest.json` | **정본.** ADR 0003 의 규칙으로 돌아간다 — ADR 0016 이 cubrid 백엔드에 한해 뒤집었던 것을 되돌린다 |
| 산출물 바이트 | `<output>/<run_id>/…` | 정본(지금도 두 백엔드 모두 여기) |
| 빌드 인덱스 | `<output>/_builds.sqlite` | **파생물.** manifest 와, manifest 없이 끝난 run 은 run 이벤트 저장소의 종료 기록(`run_endings`, #1252)에서 다시 만든다. `serve` 기동 때와 `kpubdata-builder rebuild-index` 가 만든다. 실패 code·stage 열(#1267)도 같은 원천에서 다시 채워진다 |
| 그 밖의 SQLite 저장소 | `<output>/…`, `<warehouse>/_warehouse.sqlite` | 각 저장소의 성격과 잃었을 때의 비용은 `store/inventory.py` 가 하나씩 적고, deploy.md §6.0.1 이 그 표를 옮긴다. 이 ADR 은 그 목록을 바꾸지 않는다 |

경계는 하나다: **manifest 가 정본이고 인덱스는 거기서 다시 만든다.** 인덱스에만 있고 manifest 나 종료 기록에
없는 값은 두지 않는다. 인덱스를 지워도 잃는 것이 없어야 한다.

배포 조건은 deploy.md §6 그대로다 — 프로세스 하나, 롤링 배포 없음, `/data` 는 로컬 블록 볼륨,
네트워크 파일시스템 위에 SQLite 를 두지 않는다.

스키마 버전은 #1251 이 정한 방식(각 저장소의 `schema_version` 표, 더 새로운 버전의 파일은 열지 않음)을 따른다.
**`schema_version` 표를 SQLite 의 `PRAGMA user_version` 으로 일괄 교체하는 일은 이 결정의 조건이 아니다.**

다른 데이터베이스는 나중에 검토할 수 있다(소유자). 그때를 위해 저장소 추상화 — `BuildIndex`,
`CredentialRepository`, `ArtifactStore` Protocol(ADR 0010) — 는 남긴다(#1094 의 완료 조건이기도 하다).
새 데이터베이스를 들이는 것은 새 ADR 의 일이며, 이 ADR 을 근거로 삼지 않는다.

### 2. 남아 있는 CUBRID 설정의 실패 방식 (#1094 가 구현할 계약)

CUBRID 코드가 제거된 릴리스에서:

1. `KPUBDATA_BUILDER_STORAGE_BACKEND` 가 `cubrid` 이면 **기동을 거부한다.** `serve` 뿐 아니라 상태를 여는
   모든 명령(`build`, `rebuild-index`, `prune-cancelled` 등)이 0 이 아닌 종료 코드로 끝난다.
2. `KPUBDATA_BUILDER_CUBRID_URL` 이 비어 있지 않으면, 백엔드 설정이 무엇이든 **같이 거부한다.** 그 값은 남은
   CUBRID 배포의 흔적이고, 무시하고 뜨면 운영자는 CUBRID 에 쓰고 있다고 믿게 된다.
3. 오류는 한 줄이며 다음을 담는다: 어느 설정이 문제인지, CUBRID 백엔드가 제거되었다는 것, 이 ADR, 그리고 할 일
   두 가지 — (a) 아직 대조하지 않았다면 CUBRID 를 지원하는 마지막 릴리스에서 §3 의 절차를 먼저 밟는다, (b) 대조를
   마쳤다면 두 설정을 지운다. **URL 의 값은 오류에 다시 적지 않는다**(비밀번호가 들어 있다, #1108).
4. **알림 없이 SQLite 로 넘어가는 것(fallback)은 금지한다.** 거부 대신 경고만 남기고 뜨는 것도 금지한다.
5. `KPUBDATA_BUILDER_STORAGE_BACKEND` 가 비어 있거나 `sqlite` 이면 지금처럼 뜬다. 그 밖의 값은 지금처럼 거부한다.
6. 거부되어야 할 설정이 실제로 거부되는지 확인하는 부정 테스트를 같은 변경에 둔다.
7. 설정 목록(`settings_catalog.py`)에서 두 설정을 빼면, `tests/unit/test_prod_compose_settings.py` 의
   `NOT_PASSED` 와 deploy.md 의 같은 목록에서도 같은 변경으로 뺀다 — 둘이 설정 목록과 대조되므로, 하나만 빼면
   테스트가 실패한다(#1168, #1093 의 댓글). 1~3 의 거부는 설정 목록이 아니라 "제거된 설정" 목록으로 한다.

### 3. CUBRID 에서 파일로 옮기는 절차

CUBRID 를 쓰는 배포는 **CUBRID 백엔드가 아직 있는 릴리스**에서 이 절차를 마친 뒤에 제거 릴리스로 올린다.

**0. 백업.** Builder 를 내린다(프로세스 하나라 이 동안 쓰는 것이 없다). CUBRID 데이터베이스의 백업과 `/data`
볼륨의 스냅샷을 같은 시점에 남긴다(#1095). 이후 단계는 이 둘을 바꾸지 않거나, 바꾸더라도 파일을 더하기만 한다.

**1. 대조(dry run).** 같은 이미지에서, CUBRID URL 을 준 채로:

```bash
python -m kpubdata_builder.store.cubrid_reconcile --output-dir /data          # 사람이 읽는 보고
python -m kpubdata_builder.store.cubrid_reconcile --output-dir /data --json   # 기록용
```

이 도구는 읽기만 한다 — CUBRID 의 표도, 파일도 바꾸지 않고, 표가 없으면 비어 있는 것으로 본다. `manifests` 의
행을 하나씩 `<output>/<run_id>/manifest.json` 과 비교한다. 행과 파일은 직렬화가 달라서(파일은 들여쓰기한다)
글자가 아니라 읽은 JSON 문서로 비교한다.

| 분류 | 뜻 | 할 일 |
|---|---|---|
| `missing_on_disk` | 행은 있는데 `manifest.json` 이 없다 | 복구(2·3단계) |
| `unreadable_on_disk` | 파일은 있지만 JSON 객체로 읽히지 않는다(잘린 미러) | 복구 |
| `differs` | 둘 다 읽히는데 내용이 다르다 | 복구 — 행을 쓴다(아래 규칙) |
| `unreadable_in_db` | 행이 JSON 객체가 아니다 | 없음 — Builder 는 지금도 이때 파일을 쓴다 |
| `unsafe_run_ids` | run ID 가 출력 디렉터리 밖을 가리킨다 | 읽지도 쓰지도 않는다. 기록하고 사람이 판단한다 |
| `matching` | 같다 | 없음 |
| `disk_only` | 파일만 있다(승격된 적 없는 run) | 없음 — 이미 파일이 정본이다 |
| `credentials` | CUBRID 에 키가 저장된 `owner_id/provider` 쌍. 암호문은 읽지 않는다 | §4 |

종료 코드: 복구할 run 이 없으면 0, 있으면 1, CUBRID 를 읽지 못하면 2.

`differs` 에서 행을 쓰는 이유: CUBRID 로 도는 동안 사용자가 본 manifest 는 행이었다(`get_manifest` 가 행을
먼저 본다). 행으로 맞추면 사용자에게 보이던 것이 그대로 남는다. 파일 쪽 내용은 2단계에서 따로 남기므로 잃지 않는다.

**2. 내보내기(export).** 1단계가 복구할 run 으로 꼽은 것마다, 행의 문서와 지금 있는 파일(있다면)을 `/data` 의
run 디렉터리가 아닌 별도 디렉터리에 run ID 이름으로 저장한다. 원래 파일을 덮어쓰기 전에 남기는 것이
이 단계의 목적이다.

**3. 복구.** 행의 문서를 `<output>/<run_id>/manifest.json` 으로 쓴다. 직렬화는 `LocalArtifactStore.put_manifest`
와 같다(키 정렬, UTF-8, 들여쓰기 2, 끝 줄바꿈). 산출물 바이트는 원래 파일에만 있었으므로, 복구한 run 의 산출물이
없으면 그 run 을 보고에 남긴다 — manifest 는 되살아나지만 바이트는 백업에서만 돌아온다.

**4. 인덱스 재구축.** `KPUBDATA_BUILDER_STORAGE_BACKEND` 를 비우고 `kpubdata-builder rebuild-index --output-dir /data`
를 실행한다. SQLite 인덱스가 manifest 와 종료 기록에서 새로 만들어진다.

**5. 검증.**

- 1단계를 다시 실행해 종료 코드 0(`needs restore: no`)을 확인한다.
- CUBRID `manifests` 의 run ID 가 모두 `manifest.json` 을 가진 run 디렉터리로 있는지 — 1단계의 보고가 그것이다.
- 새 SQLite 인덱스의 run 수를 CUBRID `builds` 의 행 수와 비교하고, 차이가 있으면 run ID 로 설명한다(인덱스는
  파생물이라 CUBRID 쪽이 이전 실패로 모자랄 수는 있어도, SQLite 쪽이 모자라면 안 된다).
- SQLite 백엔드로 `serve` 를 띄워 기동이 성공하고, 복구한 run 몇 개를 API 로 조회해 manifest 가 행과 같은지 본다.

**6. 전환.** 설정에서 `KPUBDATA_BUILDER_STORAGE_BACKEND` 와 `KPUBDATA_BUILDER_CUBRID_URL` 을 지우고 같은 릴리스로
띄운다. 정상 운영을 확인한 뒤에 제거 릴리스(#1094)로 올린다. CUBRID 데이터베이스는 **지우지 않고 둔다** — 지우는
시점과 보관 기간은 소유자가 정한다.

이 저장소에 있는 것은 1단계의 dry run 도구(`store/cubrid_reconcile.py`)다. 2·3단계를 하는 도구(내보내기와 복구)는
#1094 가 같은 모듈에 더한다. 그 전까지는 위 규칙대로 사람이 한다.

### 4. 자격 증명 저장소

**기본은 사용자가 키를 다시 등록하는 것이다.**

자동 이관이 암호 방식 때문에 불가능한 것은 아니다. 두 저장소는 같은 암호화를 쓴다 — AES-GCM, 12바이트 nonce
뒤에 암호문과 태그, 키는 `KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`, AAD 는 `owner_id\0provider`
(`credentials/store.py` 의 `associated_data`, 두 저장소가 같은 함수를 쓴다). AAD 에 백엔드가 들어가지 않으므로,
**같은 master key 를 쓰는 한** CUBRID 의 암호문(base64 문자열)을 디코드해 SQLite 의 BLOB 으로 옮기면 복호화
없이 그대로 읽힌다. ADR 0016 의 "master key + AAD 바인딩이라 자동 이관되지 않는다"는 이 점에서 정확하지 않다.

그런데도 다시 등록을 기본으로 하는 이유:

- 옮기는 도구는 한 번 쓰고 버릴 코드인데, 모든 사용자의 비밀을 한꺼번에 다룬다. 다시 등록하면 비밀을 다루는
  새 코드가 생기지 않는다.
- 저장된 키의 수는 작고, 1단계의 보고가 누구의 어느 provider 키인지(비밀 없이) 알려 주므로 운영자가 사용자에게
  알릴 수 있다.
- 다시 등록하면 연결 테스트를 거치므로, 그사이 만료되거나 폐기된 키가 걸러진다.
- master key 를 바꿨거나 배포마다 달랐다면 옮겨도 읽히지 않는다.

소유자가 암호문 복사를 원하면 #1094 나 후속 이슈에서 한다. 조건은 같은 master key, 같은 `owner_id`(ADR 0012 의
안정된 사용자 ID), 그리고 옮긴 행마다 복호화가 성공하는지(값은 출력하지 않고) 확인하는 것이다.

전환 뒤에도 CUBRID 의 `provider_credentials` 와 그 백업에는 암호문이 남는다. master key 와 함께 새면 키가
드러나므로, 데이터베이스를 지우기 전까지 그 백업은 비밀처럼 다룬다.

### 5. 롤백

- **전환 전(0~5단계 중 실패).** CUBRID 는 바뀐 것이 없다. 3단계는 파일을 더하거나 행과 같은 내용으로 고칠
  뿐이고, cubrid 백엔드에게 파일은 미러이므로 해가 없다. 원래 설정(`STORAGE_BACKEND=cubrid`)으로 다시 띄운다.
- **전환 뒤, 같은 릴리스에서.** 전환 뒤에 생긴 run 은 파일에만 있다. CUBRID 로 돌아가면 `get_manifest` 는
  행이 없는 run 을 파일에서 읽고, `rebuild-index` 의 cubrid 경로는 파일에서 `builds` 를 다시 채운다. 전환 뒤에
  SQLite 에 등록한 키는 CUBRID 에 없으므로 다시 등록해야 한다.
- **제거 릴리스(#1094) 뒤.** CUBRID 를 지원하는 마지막 릴리스로 되돌린다 — #1094 가 CHANGELOG 에 그 버전을
  적는다. 제거 릴리스가 SQLite 저장소의 스키마 버전을 올렸다면 이전 릴리스는 그 파일을 열지 않는다(#1251,
  deploy.md §6.0). 그래서 제거 릴리스로 올리기 **직전**의 `/data` 백업을 남겨 두고, 되돌릴 때는 그것을 쓴다.
- CUBRID 데이터베이스와 0단계의 백업은 소유자가 지워도 된다고 할 때까지 남긴다.

## 검토한 대안

| | 내용 | 얻는 것 | 잃는 것 |
|---|---|---|---|
| A. 대조 없이 바로 제거 | 코드와 설정을 지운다 | 가장 빠르다 | 행에만 있던 manifest 를 잃을 수 있다(위 "그냥 지우면 잃는 것") |
| B. CUBRID 를 선택 백엔드로 유지 | 지금처럼 둔다 | 할 일이 없다 | 쓰는 배포가 없는 백엔드의 드라이버 의존성, 전용 CI 워크플로, 이미지 변형, 의존성 갱신을 계속 떠안는다. 두 정본 규칙이 문서와 코드에 남는다 |
| C. 다른 데이터베이스로 바로 전환 | PostgreSQL 등 | 다중 인스턴스의 길이 열린다 | 지금 필요가 없다. 소유자는 "나중에 검토"로 정했다 |
| **D. SQLite 와 파일만, 대조한 뒤 제거** | 이 ADR | 정본 규칙이 하나로 돌아오고, 남은 데이터를 잃지 않는다 | 절차와 dry run 도구를 한 번 만들어야 한다 |

**D 를 택한다.**

## 영향

- 승인되면 ADR 0016 의 상태를 `대체됨(0022)` 로 바꾸고, ADR 0003 의 "manifest.json 이 정본" 이 예외 없이 다시
  적용된다. ADR 0010 의 Protocol 은 남는다.
- #1094 가 코드, `cubrid` extra, lock 파일, 이미지 변형, 워크플로, `infra/cubrid` 프로필, 문서(deploy.md §6.1 등)와
  baseline 의 CUBRID 참조를 지우고 §2 의 실패 방식과 §3 의 내보내기·복구 도구를 구현한다. 이 ADR 이 승인되고
  CUBRID 를 쓰는 배포를 조사하고 대조를 마치기 전에는 제거를 병합하지 않는다.
- 백업과 복구는 #1095 가 다룬다.

## 검증

- **정적 검사:** ruff, mypy, 영어 주석 검사, ADR 목록 검사(`scripts/check_adr_index.py`), `mkdocs build --strict`.
- **실제 CUBRID 와의 리허설:** `tests/cubrid/test_cubrid_reconcile.py` 가 CUBRID 워크플로(`.github/workflows/cubrid.yml`)
  에서 실제 CUBRID 11.3 서버를 상대로 돈다. 행을 커밋한 뒤 미러 쓰기를 실패시키고, 잘린 파일과 내용이 다른 파일을
  만들고, dry run 이 그 run 들을 정확히 꼽는지, 행과 파일을 하나도 바꾸지 않는지, 복구한 뒤 다시 돌리면 남는 것이
  없는지, 키 목록에 비밀이 없는지 확인한다. 비교 규칙 자체는 `tests/unit/test_cubrid_reconcile_compare.py` 가
  기본 테스트에서 확인한다.
- **남은 것:** 실제 운영 데이터로 하는 리허설. CUBRID 로 운영 중인 배포가 있는지는 소유자가 확인한다(#1094).
  없으면 위 리허설로 충분하고, 있으면 그 배포의 백업 사본에 §3 을 먼저 밟는다.

## 승인 기록

- 2026-10-08 — 소유자가 Builder 는 CUBRID 를 쓰지 않는다고 밝힘(소유자의 진술).
- 2026-10-11 — 이 ADR 을 제안함(#1093).
- 2026-10-12 — **소유자(yeongseon, 저장소 소유자)가 승인함.** 같은 결정에서 소유자가 확인한 것:
  - **CUBRID 로 운영 중인 배포는 없다.** 그래서 §3 의 대조·내보내기·복구를 밟을 배포가 없고, "검증" 절의
    "남은 것"(실제 운영 데이터로 하는 리허설)은 할 대상이 없다 — 실제 CUBRID 서버를 상대로 한 리허설
    (`tests/cubrid/test_cubrid_reconcile.py`)로 충분하다.
  - CUBRID 백엔드 제거(#1094)를 진행한다. ADR 0016 의 상태는 `대체됨(0022)` 로 바꾼다.

> **2026-10-12 보충** 쓰는 배포가 없으므로 #1094 는 §3 의 2·3단계 도구(내보내기와 복구)를 만들지 않고,
> 1단계의 dry run 도구(`store/cubrid_reconcile.py`)도 CUBRID 코드와 함께 지운다. 그 도구는 CUBRID 드라이버와
> CUBRID URL 이 있어야 돌므로, 제거 릴리스에서는 §2 의 거부 때문에 실행될 수 없다 — 제거 뒤에 남겨 둘 이유가 없다.
> 혹시 나중에 CUBRID 로 돌던 배포가 나타나면, 이 ADR 이 제안된 커밋(#1269, `08f5ff5`)의 소스를 CUBRID extra 와
> 함께 설치해 그 도구로 §3 을 밟는다(v0.4.0 이미지에는 그 도구가 없다). §5 의 롤백 대상 릴리스도 v0.4.0 이다.

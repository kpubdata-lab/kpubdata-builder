"""Every environment variable Builder reads, written once (#1108).

The settings were read wherever they were needed — `cli.py`, the service modules, the
container entrypoint — and described by hand in `docs/deployment.md`, in two tables that
had drifted apart and missed eight of the variables the code reads. This is the list.
`scripts/generate_settings_doc.py` writes the tables in `docs/deployment.md` from it, and
`tests/unit/test_settings_catalog.py` fails when the document is stale, when the code
reads a variable that is not here, and when an entry here is read by nothing.

This module describes the settings; it does not read them. Each is still read where it
is used. The descriptions are the operator documentation and are written in Korean, as
the deployment guide is.
"""

# One setting per entry; the descriptions are prose and run as long as they need to.
# ruff: noqa: E501

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Group = Literal["access", "credentials", "builds", "ingestion", "storage", "publish", "container"]

#: The groups in the order the document lists them, with their headings.
GROUPS: dict[Group, str] = {
    "access": "인증과 접근",
    "credentials": "provider 키와 자격 증명",
    "builds": "빌드와 동시성",
    "ingestion": "업로드와 URL 소스",
    "storage": "상태 저장·웨어하우스·쿼리",
    "publish": "게시",
    "container": "컨테이너 진입점",
}


@dataclass(frozen=True)
class Setting:
    """One environment variable an operator may set."""

    name: str
    group: Group
    #: What the variable does, as the deployment guide says it.
    description: str
    #: The default, as the document shows it — prose, not a value to parse.
    default: str
    #: Whether it must be set, as the document shows it — prose, since most are conditional.
    required: str


@dataclass(frozen=True)
class InternalVariable:
    """A variable an operator does not set: Builder sets it for itself, a child process
    or kpubdata, or a test sets it. Not a setting, but it is in the environment and has
    to be known."""

    name: str
    description: str


SETTINGS: tuple[Setting, ...] = (
    Setting(
        name="KPUBDATA_BUILDER_API_KEY",
        group="access",
        description="API 인증 키 (`X-API-Key` 헤더). 미설정 시 모든 요청 401 (fail-closed)",
        default="없음",
        required="**필수** (프로덕션)",
    ),
    Setting(
        name="KPUBDATA_BUILDER_DEV_MODE",
        group="access",
        description="`true`/`1`이면 인증 생략 (**로컬 개발 전용**, ADR 0006). 기동 시 경고 로그를 남기고, `OIDC_ISSUER` 나 `ENFORCE_OWNERSHIP` 과 함께 설정되면 기동 거부(#1072)",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_ALLOWED_ORIGINS",
        group="access",
        description="CORS 허용 오리진 (콤마 구분, default-deny). 응답에는 항상 `Vary: Origin`이 붙는다",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT",
        group="access",
        description="윈도당 허용할 인증 실패 횟수(클라이언트 IP별). 초과분은 `429 auth_throttled`. `0` 이하면 비활성",
        default="`60`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS",
        group="access",
        description="인증 실패 카운트 윈도(초)",
        default="`60`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_TRUSTED_PROXIES",
        group="access",
        description='리버스 프록시의 주소나 CIDR 블록(콤마 구분). TCP peer 가 그중 하나일 때에만 `X-Forwarded-For` 로 인증 실패 스로틀의 클라이언트를 가린다([deploy.md](deploy.md) "리버스 프록시 뒤에서")',
        default="미설정 (헤더를 읽지 않음)",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_ADMIN_SUBJECTS",
        group="access",
        description="관리자로 대우할 `<issuer>\\|<sub>` 목록(쉼표 구분, #679). 관리 엔드포인트(`GET /admin/runs`, `GET /admin/config`)를 열지만 남의 run 산출물은 열지 않는다. **issuer 를 반드시 함께 적는다** — `sub` 는 issuer 안에서만 유일하고 `OIDC_ISSUER` 는 복수를 허용한다. issuer 없는 항목은 경고와 함께 무시된다. OIDC 배포에서는 이 변수를 컨테이너까지 전달해야 한다",
        default="미설정",
        required="다중 사용자 배포 시 선택",
    ),
    Setting(
        name="OIDC_JWKS_URL",
        group="access",
        description="JWKS 엔드포인트를 직접 지정한다. 미설정 시 issuer의 discovery 문서에서 찾는다",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="OIDC_JWKS_TTL",
        group="access",
        description="JWKS 캐시 수명(초). 만료되면 다음 Bearer 인증이 다시 가져온다",
        default="`3600`",
        required="선택",
    ),
    Setting(
        name="OIDC_ISSUER",
        group="access",
        description="OIDC 발급자 (설정 시 Bearer 활성, ADR 0015 — Keycloak realm). 쉼표로 여럿을 줄 수 있고, 그때는 아래 허용 목록의 모든 항목이 `<issuer>\\|<값>` 이어야 한다(#1074)",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="OIDC_AUDIENCE",
        group="access",
        description="OIDC audience (OIDC_ISSUER 설정 시 필수)",
        default="미설정",
        required="OIDC 시 필수",
    ),
    Setting(
        name="OIDC_ALLOWED_HD",
        group="access",
        description="허용 Workspace 도메인. OIDC 배포는 이 셋 중 하나 이상이 필수 — 없으면 기동 거부(#635). 세 목록의 항목은 `<값>` 또는 `<issuer>\\|<값>` 이다. issuer 를 적은 항목은 그 issuer 가 말한 값만 들인다 — sub·이메일·도메인은 issuer 안에서만 뜻이 있다. issuer 가 하나면 `<값>` 은 그 issuer 의 것이고, 둘 이상인데 `<값>` 만 적은 항목이 있으면 기동을 거부한다(#1074). issuer 는 `OIDC_ISSUER` 에 적은 그대로 쓴다",
        default="미설정",
        required="OIDC 시 셋 중 하나 필수",
    ),
    Setting(
        name="OIDC_ALLOWED_SUBJECTS",
        group="access",
        description="허용 sub 목록 (콤마 구분). 항목 형식은 `OIDC_ALLOWED_HD` 와 같다",
        default="미설정",
        required="OIDC 시 셋 중 하나 필수",
    ),
    Setting(
        name="OIDC_ALLOWED_EMAILS",
        group="access",
        description="허용 이메일 목록 (콤마 구분). 항목 형식은 `OIDC_ALLOWED_HD` 와 같다",
        default="미설정",
        required="OIDC 시 셋 중 하나 필수",
    ),
    Setting(
        name="ENFORCE_OWNERSHIP",
        group="access",
        description="`true`/`1`이면 run 소유권 강제 (C2, #389). `OIDC_ISSUER`가 있으면 값과 무관하게 켜진다(#635). `KPUBDATA_BUILDER_DEV_MODE` 와 함께 켜면 `serve` 가 기동을 거절한다 — dev principal 은 인증 없이 모든 사용자의 run 을 읽는다(#1072)",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY",
        group="credentials",
        description="사용자별 Provider credential AES-GCM master key (URL-safe base64 32 bytes)",
        default="미설정",
        required="credential CRUD 사용 시 필수",
    ),
    Setting(
        name="KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT",
        group="credentials",
        description="Provider connection test 전송 timeout(초)",
        default="`10`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL",
        group="credentials",
        description="`true`/`1`/`yes`/`on` 이면 **데이터 조회**에도 요청자 자신의 provider 키만 쓰고 운영자 키로 내려가지 않는다(F-07). 폴백을 두면 공유 배포에서 한 사람의 질의가 운영자 쿼터를 쓰고 운영자 신원으로 제공기관에 찍힌다. 미설정이면 폴백 허용(단일 사용자 배포 기본 동작). **다중 사용자 배포(OIDC 또는 `ENFORCE_OWNERSHIP`)에서는 값과 무관하게 켜지고**, 키는 요청의 `X-Provider-Key` 헤더로만 받아 요청·작업 동안만 메모리에 둔다(#683). **`env_keys` 를 지원하는 kpubdata 가 필요하다**(0.8.0 에는 없다): 없으면 `serve` 가 기동을 거부하고(종료 코드 1), 그 kpubdata 로 키 없는 클라이언트를 만들려는 시도도 오류로 끝난다 — 환경변수 키로 조용히 내려가지 않는다(#990).",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS",
        group="credentials",
        description="다중 사용자 배포에서 비동기 작업에 묶인 provider 키를 워커가 가져가기 전까지 메모리에 두는 최대 시간(#683). 제출한 때부터 세고, 지나면 타이머가 그때 키를 지운다 — 큐에 시간 상한이 없어도 키는 이 시간을 넘겨 남지 않는다(#1070). 그 작업은 차례가 왔을 때 `credentials_required` 로 끝난다. 워커가 가져간 뒤에는 이 시간과 무관하게 빌드가 끝날 때까지 쓰고 버린다. 작업에는 그 spec 이 쓰는 provider 의 키만 묶인다",
        default="`3600`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL",
        group="credentials",
        description="`true`면 게시 시 요청자에게 저장된 publish credential 만 쓰고 서버 환경변수(`HF_TOKEN` 등)로 내려가지 않는다(#635). 미설정이면 폴백 허용(단일 사용자 배포 기본 동작). **다중 사용자 배포(OIDC 또는 `ENFORCE_OWNERSHIP`)에서는 값과 무관하게 폴백이 없고 저장된 publish credential 도 읽지 않는다** — 토큰은 요청의 `X-Publish-Credential` 헤더(`HF_TOKEN=...`, `KAGGLE_USERNAME=...`, `KAGGLE_KEY=...`)로만 받아 그 요청 동안만 메모리에 둔다(#925)",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS",
        group="credentials",
        description='한 사용자가 같은 provider 를 다시 probe(`POST /providers/{provider}/probe`)할 수 있을 때까지의 간격(초, #1059). 기본 60. `0` 이면 간격을 두지 않고 "사용자당 동시 1건"만 남는다. 간격 안의 요청은 provider 를 호출하지 않고 429 `probe_rate_limited` 로 답한다. 프로세스 메모리에만 있어 재시작하면 잊는다.',
        default="`60`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS",
        group="builds",
        description="`prune-cancelled --apply`가 cancelled partial run을 정리하기까지의 보존 시간(시간). 미설정이면 정리 대상 없음(#549)",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_MAX_WORKERS",
        group="builds",
        description="동시 요청 스레드 상한",
        default="`10`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_MAX_BUILDS",
        group="builds",
        description="동시에 도는 build 수의 상한 — 동기 `POST /build` 와 비동기 job 을 합쳐서(`serve --max-builds`, #1028). 한도에 닿으면 동기 build 는 기다리고 비동기 job 은 큐에 남는다",
        default="`KPUBDATA_BUILDER_MAX_WORKERS` 값",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_BUILD_TIME_LIMIT_SECONDS",
        group="builds",
        description="비동기 build 한 건이 실행을 시작한 뒤 돌 수 있는 시간(초, #1119). 큐에서 기다린 시간은 세지 않는다. 넘으면 취소를 요청하고, build 는 다음 안전한 단계 경계에서 멈춰 `cancelled` 로 끝나며 그 `run_cancelled` 이벤트가 이유를 말한다. 측정 없이 정한 출발값이다(kpubdata#812). `0` 이면 끔. 동기 `POST /build` 에는 적용하지 않는다",
        default="`21600` (6시간)",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_MAX_ACTIVE_BUILDS_PER_OWNER",
        group="builds",
        description="다중 사용자 배포에서 사용자 한 명이 동시에 대기·실행 중으로 둘 수 있는 비동기 build 수(#1189). 넘으면 `POST /builds` 가 429 `build_owner_limit` 로 답하고, 큐는 다른 사용자에게 열려 있다. 측정 없이 정한 출발값이다(kpubdata#812). `0` 이면 끔. 단일 사용자 배포에는 적용하지 않는다",
        default="`2`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_MAX_PREVIEWS",
        group="builds",
        description="동시에 도는 preview 수의 상한(`serve --max-previews`, #1028). 넘는 preview 는 `KPUBDATA_BUILDER_BUILD_WAIT_SECONDS` 까지 기다리고, 그래도 자리가 없으면 429 `preview_queue_full` 로 답한다(#1068)",
        default="미설정 (무제한)",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS",
        group="builds",
        description="SIGTERM 뒤 실행 중인 비동기 빌드가 끝나기를 기다리는 시간(초, #1118). 지나면 남은 빌드에 멈추라고 하고 10초를 더 기다린다. `0` 이면 바로 멈추라고 한다. 유한한 0 이상의 수가 아니면(`inf`, `nan`, 음수, 숫자가 아닌 값) `serve` 가 기동을 거부한다. 대기 중이던 작업은 이 값과 무관하게 시작하지 않고 끝난다. 컨테이너의 종료 대기 시간(compose `stop_grace_period`, 기본 설정 120초)을 이 값 + 10초보다 길게 둔다",
        default="`90`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_BUILD_WAIT_SECONDS",
        group="builds",
        description="동기 `POST /build` 가 build 자리를 기다리는 상한(초, #1040). 넘으면 아무것도 가져오지 않은 채 429 `build_queue_full` 로 답한다. `0` 이면 자리가 없을 때 바로 거절한다. 비동기 `POST /builds` 는 기다리지 않고 큐에 넣는다. 유한한 0 이상의 수여야 한다 — `nan`, `inf`, 음수, 숫자가 아닌 값이면 `serve` 가 기동을 거절하고, 빈 값이면 기본값을 쓴다(#1068). preview 자리 대기에도 같은 상한이 쓰인다",
        default="`30`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_CHECKPOINT_MAX_AGE_SECONDS",
        group="builds",
        description="`retry_of` 로 재시도하는 build 가 원본 run 의 `param_grid` checkpoint 를 이어받을 수 있는 최대 나이(초, #1103). checkpoint 의 나이는 그 안의 가장 오래된 기록이 수집된 때부터 세며, 이어받아도 새로워지지 않는다. 넘으면 그 source 를 처음부터 다시 수집하고 manifest 의 `checkpoints_not_reused` 에 `expired` 로 남긴다. `0` 이면 재시도가 checkpoint 를 이어받지 않는다. 읽을 수 없는 값은 기본값으로 읽는다",
        default="`86400`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_MAX_UPLOAD_BYTES",
        group="ingestion",
        description="`POST /uploads`가 받는 최대 본문 크기(바이트). 초과분은 413",
        default="코드 기본값",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_UPLOAD_MAX_FILES",
        group="ingestion",
        description="다중 사용자 배포에서 사용자 한 명이 가질 수 있는 업로드 수(#1045). 넘으면 409 `upload_quota_exceeded`. `0` 이면 끔. 단일 사용자 배포에는 적용하지 않는다",
        default="`50`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_UPLOAD_MAX_TOTAL_BYTES",
        group="ingestion",
        description="다중 사용자 배포에서 사용자 한 명의 업로드 합계 크기(바이트, #1045). 넘으면 409 `upload_quota_exceeded`. `0` 이면 끔",
        default="`1073741824` (1 GiB)",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_UPLOAD_RETENTION_DAYS",
        group="ingestion",
        description="다중 사용자 배포에서 업로드를 보관하는 일수(#1045). 지난 업로드는 서비스가 뜰 때, 그 사용자가 다음에 업로드할 때, 그리고 그 사용자가 자기 업로드를 **읽을 때**(조회·목록·preview·build, #1067) **삭제된다**. 그래서 이 값을 **줄이면** 그 순간부터 사용자가 목록을 여는 것만으로 새 기한을 넘긴 업로드가 지워진다 — 그것을 가리키는 저장 스펙의 다음 빌드는 업로드를 찾지 못한다. `0` 이면 끔",
        default="`30`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES",
        group="ingestion",
        description="`kind: url` source가 가져오는 최대 응답 크기(바이트). SSRF 방어의 일부(#498)",
        default="코드 기본값",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_STORAGE_BACKEND",
        group="storage",
        description="상태 백엔드 (`sqlite`=로컬 기본, `cubrid`=CUBRID, ADR 0016)",
        default="`sqlite`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_CUBRID_URL",
        group="storage",
        description="CUBRID SQLAlchemy URL (예: `cubrid+pycubrid://user:pass@host:33000/db?charset=utf8`)",
        default="미설정",
        required="`STORAGE_BACKEND=cubrid` 시 필수",
    ),
    Setting(
        name="KPUBDATA_BUILDER_WAREHOUSE",
        group="storage",
        description="테이블 카탈로그 루트(`serve --warehouse` 와 같다). 설정하면 `POST /build` 가 source 별 Gold 를 커밋된 table snapshot 으로 남기고 응답 `materialized` 에 보고한다 — publish 자격증명이 필요 없다(#703). 미설정이면 카탈로그를 쓰지 않고 응답에 `materialized` 키가 없다",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_QUERY_MAX_CONCURRENCY",
        group="storage",
        description="동시 query child process 상한",
        default="`2`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_QUERY_MAX_MEMORY_MB",
        group="storage",
        description="query child 하나의 address space 상한(MB). 넘은 질의만 실패한다",
        default="무제한",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_QUERY_MEMORY_BUDGET_MB",
        group="storage",
        description="동시에 도는 query child 들의 메모리 합(MB). 질의마다 `MAX_MEMORY_MB` 만큼 예약하고 모자라면 `429 query_busy`",
        default="없음",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_DUCKDB_THREADS",
        group="storage",
        description="DuckDB 연결 하나의 thread 수(build 는 실행 중인 source 마다, query child 는 하나)",
        default="`2`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_DUCKDB_MEMORY_LIMIT",
        group="storage",
        description="DuckDB 연결 하나의 buffer 메모리(`1GB`, `512MB` …). 넘으면 임시 디스크로 spill 한다",
        default="`1GB`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_DUCKDB_MAX_TEMP_SIZE",
        group="storage",
        description="DuckDB 연결 하나의 spill(임시 디스크) 상한. 넘은 source·질의만 실패한다. build 의 spill 은 run 디렉터리의 `_duckdb_tmp`, query 의 spill 은 질의마다 새로 만들고 지우는 임시 디렉터리에 쓴다",
        default="`10GB`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT",
        group="publish",
        description="HTTP `local` publish target의 루트 디렉터리(절대 경로). destination은 이 안의 상대 `owner/name`로 한정된다(#550). 미설정이면 local target blocker",
        default="미설정",
        required="local publish 사용 시 필수",
    ),
    Setting(
        name="HF_TOKEN",
        group="publish",
        description="Hugging Face 게시 토큰. **단일 사용자 배포에서만** 읽는다 — 저장된 publish credential 이 없을 때의 서버 쪽 값이다. 다중 사용자 배포에서는 이 변수를 읽지 않고, 요청의 `X-Publish-Credential` 헤더만 쓴다(#925). CLI `publish` 도 이 값을 쓴다",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KAGGLE_USERNAME",
        group="publish",
        description="Kaggle 게시 계정. `KAGGLE_KEY` 와 함께 있어야 쓴다. 읽는 조건은 `HF_TOKEN` 과 같다 — 단일 사용자 배포에서만, 다중 사용자 배포에서는 읽지 않는다(#925)",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KAGGLE_KEY",
        group="publish",
        description="Kaggle API 키. `KAGGLE_USERNAME` 과 한 쌍이다",
        default="미설정",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_PORT",
        group="container",
        description="바인딩 포트",
        default="`8000`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_OUTPUT_DIR",
        group="container",
        description="실행 워크스페이스 루트",
        default="`/data`",
        required="선택",
    ),
    Setting(
        name="KPUBDATA_BUILDER_HOST",
        group="container",
        description="바인딩 호스트",
        default="`0.0.0.0`",
        required="선택",
    ),
)


INTERNAL_VARIABLES: tuple[InternalVariable, ...] = (
    InternalVariable(
        name="KAGGLE_CONFIG_DIR",
        description="Kaggle 게시를 실행하는 동안 Builder 가 빈 임시 디렉터리로 설정한다 — 호스트의 `~/.kaggle/kaggle.json` 을 읽지 않게 하기 위해서다. 게시가 끝나면 원래 값으로 되돌린다",
    ),
    InternalVariable(
        name="KPUBDATA_QUERY_TEMP_DIR",
        description="쿼리 엔진이 쿼리마다 만든 임시 디렉터리를 자식 프로세스에 넘길 때 설정한다. DuckDB 의 spill 파일이 여기에 생기고, 쿼리가 끝나거나 죽으면 부모가 지운다. 운영자가 정해 두는 값이 아니다 — spill 크기의 상한은 `KPUBDATA_DUCKDB_MAX_TEMP_SIZE` 다",
    ),
    InternalVariable(
        name="KPUBDATA_BUILDER_REPLAY_DIR",
        description="테스트·CI 가 녹화된 provider 응답 디렉터리를 가리킬 때 설정한다. 설정돼 있으면 Builder 가 아래 두 변수를 kpubdata 에 넘겨 실제 provider 를 부르지 않게 한다. 운영 배포에서는 설정하지 않는다",
    ),
    InternalVariable(
        name="KPUBDATA_MODE",
        description="`KPUBDATA_BUILDER_REPLAY_DIR` 가 있을 때 Builder 가 `replay` 로 설정한다. kpubdata 가 읽는 값이다",
    ),
    InternalVariable(
        name="KPUBDATA_REPLAY_DIR",
        description="`KPUBDATA_BUILDER_REPLAY_DIR` 가 있을 때 Builder 가 그 디렉터리의 절대 경로로 설정한다. kpubdata 가 읽는 값이다",
    ),
)


def setting_names() -> frozenset[str]:
    """The names an operator may set."""
    return frozenset(setting.name for setting in SETTINGS)


def internal_names() -> frozenset[str]:
    """The names Builder sets for itself."""
    return frozenset(variable.name for variable in INTERNAL_VARIABLES)


__all__ = [
    "GROUPS",
    "INTERNAL_VARIABLES",
    "SETTINGS",
    "InternalVariable",
    "Setting",
    "internal_names",
    "setting_names",
]

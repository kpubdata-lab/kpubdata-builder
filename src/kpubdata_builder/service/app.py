"""Builder 서비스 로직 (#36).

Studio 같은 외부 UI가 Builder를 호출할 수 있도록 validate/preview/build/artifacts
연산을 HTTP 전송과 분리된 순수 로직으로 제공한다. 각 메서드는 ServiceResponse
(상태 코드 + JSON 직렬화 가능한 body)를 반환하며, dispatch가 (method, path)를
해당 연산으로 라우팅한다.

주요 구성:
    - ServiceResponse: 상태 코드 + body
    - BuilderService: validate/preview/build/artifacts 연산
    - dispatch: 경로 라우팅
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast, runtime_checkable
from urllib.parse import urlsplit

import yaml
from kpubdata.core.models import DatasetRef
from typing_extensions import assert_never

from ..credentials import (
    AesGcmCredentialCipher,
    CredentialRepository,
    SQLiteCredentialRepository,
)
from ..errors import SpecLoadError, ValidationError
from ..events import BuildEvent, BuildEventStore
from ..manifest import status_from_manifest
from ..pipeline import (
    DEFAULT_PREVIEW_SEED,
    CancellationProbe,
    SampleMode,
    preview_build,
    run_build,
)
from ..quality import QualityCheckResult
from ..query.service import QueryService
from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.validator import validate_spec
from ..stages._path_safety import validate_path_segment
from ..stages.bronze.build import SourceClient
from ..store import make_build_index
from ..store.artifacts import make_artifact_store
from ..store.backend import storage_backend
from ..tabular import DEFAULT_PREVIEW_LIMIT
from ..uploads import (
    SQLiteUploadRepository,
    UploadRepository,
    resolve_max_upload_bytes,
)
from . import datasets as datasets_service
from . import monitoring as monitoring_service
from . import ownership as ownership_module
from . import publish as publish_service
from .auth import AuthError, Principal, authenticate
from .auth_throttle import AuthFailureThrottle
from .builds_api import BuildArtifactsApiService
from .datasets_api import DatasetsApiService
from .jobs import AsyncBuildExecutor, generate_run_id
from .monitoring_api import MonitoringApiService
from .providers import (
    CredentialResolver,
    ProviderCredentialConflictError,
    ProviderDescriptor,
    ProviderTestOperation,
    default_provider_test,
    runtime_provider_catalog,
)
from .providers_service import ProvidersService
from .publish_api import PublishApiService
from .quality_api import QualityApiService
from .query_service_api import QueryApiService
from .responses import FileResponse, ServiceResponse
from .routes import ROUTE_ADAPTERS
from .routes import uploads as uploads_route
from .routes.core import MAX_PREVIEW_LIMIT
from .stages_api import StagesApiService
from .uploads_service import UploadsService

logger = logging.getLogger(__name__)

_CREDENTIAL_MASTER_KEY_ENV = "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"
_PROVIDER_TEST_TIMEOUT_ENV = "KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT"
_DEFAULT_PROVIDER_TEST_TIMEOUT = 10.0

# 최근-window quality aggregate가 canonical manifest.json mtime으로 후보를 좁힐 때
# 쓰는 여유분. mtime은 완료 시점에 만들어지는 정본 파일의 것이라 항상
# finished_at 이상이지만, 완료 후 재기록(예: secret redaction)·시계 오차·파일시스템
# mtime 해상도를 흡수하려 window 하한을 이만큼 더 내려 잡는다. 정확한 경계는
# quality.aggregate_quality_window가 canonical timestamp로 다시 적용한다.
_QUALITY_WINDOW_MTIME_MARGIN_SECONDS = 3600


# /preview의 limit 방어적 상한 (#497). 기존에는 상한이 없었다 — Preview는 전체
@runtime_checkable
class _CloseableClient(Protocol):
    def close(self) -> None: ...


def _close_request_client(client: SourceClient) -> None:
    if isinstance(client, _CloseableClient):
        client.close()


def _raise_provider_test_error(client: SourceClient, provider: str) -> None:
    """provider_status의 client 생성 실패 fallback에서 쓰는 항상-raise operation."""
    raise RuntimeError()


def _credential_repository_from_env(output_root: Path) -> CredentialRepository | None:
    """master key가 설정된 경우에만 encrypted repository를 활성화한다.

    백엔드는 KPUBDATA_BUILDER_STORAGE_BACKEND 를 따른다 (ADR 0016): cubrid 이면 전역
    Engine 을 공유하는 CubridCredentialRepository, 아니면 기본 SQLite 파일.
    """
    encoded_key = os.environ.get(_CREDENTIAL_MASTER_KEY_ENV)
    if not encoded_key:
        return None
    cipher = AesGcmCredentialCipher.from_base64(encoded_key)
    if storage_backend() == "cubrid":
        from ..credentials.store_cubrid import CubridCredentialRepository
        from ..store.backend import get_engine

        return CubridCredentialRepository(get_engine(), cipher)
    return SQLiteCredentialRepository(
        output_root / ".service" / "provider-credentials.sqlite3", cipher
    )


def _factory_accepts_keyword(factory: Callable[..., SourceClient], keyword: str) -> bool:
    """factory가 특정 keyword 또는 **kwargs를 받는지 side effect 없이 판정한다."""
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _redact_secret_text(value: str | None, secrets: Iterable[str]) -> str | None:
    """응답/manifest 문자열에서 현재 요청 credential 원문을 제거한다."""
    if value is None:
        return None
    redacted = value
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted


def _redact_json_secrets(value: object, secrets: Iterable[str]) -> object:
    """JSON tree의 모든 문자열에서 credential을 재귀적으로 제거한다."""
    secret_values = tuple(secrets)
    if isinstance(value, str):
        return _redact_secret_text(value, secret_values)
    if isinstance(value, list):
        return [_redact_json_secrets(item, secret_values) for item in value]
    if isinstance(value, dict):
        return {key: _redact_json_secrets(item, secret_values) for key, item in value.items()}
    return value


_SECRET_LIKE_PARAM_NAMES = frozenset(
    {
        "servicekey",
        "service_key",
        "apikey",
        "api_key",
        "key",
        "secret",
        "token",
        "password",
        "authkey",
        "auth_key",
    }
)


def _catalog_request_parameters(dataset: DatasetRef) -> list[JsonValue]:
    """``raw_metadata``의 요청 파라미터 설명을 secret-free allowlist로 직렬화한다.

    UI가 필수 요청 파라미터를 사전에 안내하기 위한 최소 공개 metadata다
    (``dataset.raw_metadata["request_parameters"]``, 없으면 빈 배열).

    - ``dict`` 항목만, 비어 있지 않은 문자열 ``name`` 필수.
    - ``service_key_param``과 secret-like 이름(serviceKey/apiKey/key/secret/
      token/password 등)은 제외한다 — serviceKey/API key 입력을 user request
      params로 요구하지 않는다.
    - ``name``/``required``(bool)/``description``(str|None)/``example``(str|None)만
      담는다. provider 내부 구현 세부는 노출하지 않는다.
    """
    raw = dataset.raw_metadata.get("request_parameters")
    if not isinstance(raw, (list, tuple)):
        return []
    service_key_param = str(dataset.raw_metadata.get("service_key_param", "")).strip().lower()
    result: list[JsonValue] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name_raw = item.get("name")
        if not isinstance(name_raw, str) or not name_raw.strip():
            continue
        name = name_raw.strip()
        lowered = name.lower()
        if lowered in _SECRET_LIKE_PARAM_NAMES:
            continue
        if service_key_param and lowered == service_key_param:
            continue
        description = item.get("description")
        example = item.get("example")
        result.append(
            {
                "name": name,
                "required": bool(item.get("required", False)),
                "description": description
                if isinstance(description, str) and description
                else None,
                "example": example if isinstance(example, str) and example else None,
            }
        )
    return result


def _catalog_application(dataset: DatasetRef) -> JsonValue:
    """``raw_metadata.application``을 secret-free allowlist로 직렬화한다.

    공공데이터포털은 API Key 발급과 특정 Dataset 활용신청이 별개일 수 있다 —
    ``dataset.raw_metadata["application"]``(``{"required": bool, "url": str}``)이
    있으면 그대로 전달하고, 없으면 ``null``(활용신청 여부를 알 수 없음, "필요 없음"으로
    단정하지 않는다). ``url``이 http(s) 스킴이 아니면 노출하지 않는다(임의 스킴 차단).
    """
    raw = dataset.raw_metadata.get("application")
    if not isinstance(raw, dict):
        return None
    required = raw.get("required")
    if not isinstance(required, bool):
        return None
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return {"required": required, "url": url}


def _catalog_dataset_body(dataset: DatasetRef, requires_service_key: bool) -> dict[str, JsonValue]:
    """DatasetRef의 public/canonical metadata만 allowlist로 직렬화한다 (#490).

    ``raw_metadata``는 provider 내부 정보와 secret-like 값이 섞일 수 있어
    절대 그대로 노출하지 않는다 — UI 탐색에 필요한 필드만 명시적으로 골라
    담는다. metadata가 없는 dataset은 description/source_url/query_support가
    null, tags/operations가 빈 배열로 직렬화된다(응답 전체를 깨지 않는다).
    """
    query_support: JsonValue = None
    if dataset.query_support is not None:
        query_support = {
            "pagination": dataset.query_support.pagination.value,
            "filterable_fields": cast(JsonValue, sorted(dataset.query_support.filterable_fields)),
            "sortable_fields": cast(JsonValue, sorted(dataset.query_support.sortable_fields)),
            "time_range": dataset.query_support.time_range,
            "max_page_size": dataset.query_support.max_page_size,
        }
    return {
        "name": dataset.dataset_key,
        "title": dataset.name,
        "description": dataset.description,
        "tags": cast(JsonValue, sorted(dataset.tags)),
        "source_url": dataset.source_url,
        "representation": dataset.representation.value,
        "operations": cast(JsonValue, sorted(op.value for op in dataset.operations)),
        "query_support": query_support,
        "requires_service_key": requires_service_key,
        "request_parameters": cast(JsonValue, _catalog_request_parameters(dataset)),
        "application": _catalog_application(dataset),
    }


def _enforce_ownership() -> bool:
    """run 소유권 강제가 활성화되어 있는지 (#389). 기본 off — 하위 호환."""
    return ownership_module.enforce_ownership()


# Build list entry type for API responses
_BuildListEntry = dict[str, str | None]


# Builder API 계약 버전. contract/builder-api.yaml의 info.version과 일치해야 하며
# (test_service_contract가 강제), 응답에 실어 Studio 같은 소비자가 하위 호환을
# 협상할 수 있게 한다 (#209).
# 1.4.0 -> 1.5.0: Dataset Catalog·Detail·Stage Summary API 추가 (#488, additive).
# 1.5.0 -> 1.6.0: 구조화된 Quality/Schema Drift 결과, quality history/detail API 추가
# (#486, additive — 기존 엔드포인트는 변경되지 않는다).
# 1.8.0 -> 1.9.0: query startup/engine timing fields 추가 (#523, additive).
# 1.9.0 -> 1.10.0: POST /preview에 Source↔Silver diff와 sample_mode(first/random)
# 옵션 추가 (#497, additive — 기존 필드는 유지된다). limit 상한(1000) 신규 도입은
# behavioral tightening(이전엔 상한 없음) — 그 이상 값은 400.
# 1.10.0 -> 1.11.0: GET /monitoring/summary, GET /monitoring/builds 추가 (#516,
# additive — 기존 엔드포인트는 변경되지 않는다). Async build queue/worker는
# 기존 AsyncBuildExecutor/AsyncBuildJobRegistry(#511/#513)의 read-only
# snapshot을 반영하며, 정상 runtime에서는 availability=available이다.
# MonitoringSummaryResponse.status(healthy/degraded)는 required subsystem
# availability로부터 계산되는 deterministic aggregate다(latency threshold
# 미사용).
# 1.11.0 -> 1.12.0: BuildSpec.composition(JoinSpec)과 POST /build 응답의
# composition 키, manifest.composition(CompositionProvenance) 추가 (#506,
# additive — 기존 필드/엔드포인트는 변경되지 않는다).
# 1.12.0 -> 1.13.0: BuildSpec sources[]에 kind="file"/"url"을 추가하고 (기존
# provider/dataset source는 kind="public_api"로 additive 해석), POST /uploads,
# GET /uploads/{upload_id}, DELETE /uploads/{upload_id}를 추가한다(#498,
# additive — 기존 엔드포인트/kind 없는 source는 변경되지 않는다). url source는
# P0 범위(GET, Auth=None, https만 허용)로 SSRF를 방어하는 safe fetch를 거친다.
# 1.13.0 -> 1.14.0: GET /builds/{run_id}/events를 추가한다(#496, additive —
# 기존 엔드포인트는 변경되지 않는다). run/source fetch/medallion
# stage(bronze/silver/gold/export)/quality checkpoint의 structured event
# append-only timeline을 raw logger 파싱 없이 조회할 수 있다. limit/tail
# query parameter는 bounded(기본 200, 상한 1000)이며 반환은 항상 chronological
# ascending이다. Monitoring(#516)의 시스템 aggregate와는 역할이 분리된다 — 이
# endpoint는 단일 run의 이벤트만 다룬다.
# 1.14.0 -> 1.15.0: /catalog 응답(CatalogDataset)에 탐색용 metadata(description/
# tags/source_url/representation/operations/query_support)를 추가한다(#490,
# additive — 기존 필드는 유지되며 raw_metadata는 노출하지 않는다).
# 1.15.0 -> 1.16.0: async build job 표면을 계약에 문서화한다 — POST /builds(202/200
# idempotent/409/429)와 GET /builds/{run_id}(잡 상태 polling) 추가(#480). 잡 상태
# 조회에 ownership 게이트를 적용해 cross-owner의 build 출력(response) 노출을
# 차단한다(behavioral tightening — 이전에는 미검사).
# 1.16.0 -> 1.17.0: build publish 표면을 추가한다(#491) — GET /builds/{run_id}/publish/
# readiness와 POST /builds/{run_id}/publish(idempotent receipt, TOCTOU 재검사).
# 1.17.0 -> 1.18.0: POST /builds/{run_id}/cancel을 추가한다(#481, ADR 0008 —
# additive). queued job은 실행 전에 즉시 cancelled로, running job은 cancelling을
# 거쳐 안전한 stage 경계에서 cancelled로 종결된다(강제 종료 없음). BuildJobStatus
# 어휘(queued/running/cancelling/succeeded/failed/cancelled)는 그대로 재사용한다.
# 함께 additive로: BuildManifest에 status/partial(부분 산출물 표시),
# BuildEventName에 run_cancelled, BuildSummary.status enum에 cancelled(이미
# BuildIndex/dataset 계약이 쓰던 값이 이제 실제로 관측 가능해진다).
# 1.18.0 -> 1.19.0: publish receipt 운영 경로를 추가한다(#551, additive) —
# GET /builds/{run_id}/publish/receipt(unknown 상태 조회),
# POST /builds/{run_id}/publish/reconcile(원격 상태 확인 후 succeeded 확정 또는
# reset으로 재게시 허용), DELETE /builds/{run_id}/publish/receipt(명시적 reset,
# 감사 로그 기록). reconcile은 원격 판단 불가 시 503로 아무 것도 변경하지 않는다.
# 1.19.0 -> 1.20.0: HTTP publish target에 kaggle과 local을 추가한다(#550,
# additive). kaggle은 packaging의 dataset-metadata.json id가 destination과
# 일치할 때만(readiness blocker로 정합 검증), local은
# KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT로 지정한 publish-root 안의 상대
# owner/name 경로로 한정된다. GET readiness의 destination query는 선택 파라미터다.
# 1.20.0 -> 1.21.0: publish 감사 로그 조회를 추가한다(#563, additive) —
# GET /builds/{run_id}/publish/audit(reconcile/reset 이력, 소유권 게이트).
# 1.21.0 -> 1.22.0: Studio Home dashboard가 임의 숫자 합성 없이 authoritative
# aggregate를 읽도록 두 가지 additive 조회를 추가한다(기존 필드/동작 불변):
#   - GET /datasets 응답에 total — canonical grouping + ownership 필터 이후,
#     pagination(limit) 이전의 distinct dataset 개수. items.length/limit을 total로
#     오인하지 않도록 한다.
#   - GET /quality/summary?window=24h — 최근 24h 안에서 접근 가능한 run의
#     structured quality를 PASS/WARN/FAIL run 수로 요약(#486 도메인 quality의
#     bounded cross-run aggregate). 시스템 observability(/monitoring)와 섞지 않는다.
#   - GET /catalog 응답의 CatalogDataset에 request_parameters와 application(같은
#     미머지 릴리스 범위의 additive — 기존 필드/동작 불변). raw_metadata의
#     request_parameters를 secret-free allowlist(name/required/description/
#     example)로 직렬화한 것으로, serviceKey 등 시크릿 파라미터는 제외된다. Add
#     Data가 선택한 Dataset의 필수 요청 파라미터를 사전에 안내하는 데 쓴다.
#     metadata가 없는 dataset은 빈 배열. application은 API Key 발급과 Dataset별
#     활용신청이 별개일 수 있는 경우의 안내({required, url})로, raw_metadata에
#     없으면 null(신청 완료/승인 여부를 Builder/Studio가 추측하지 않는다).
# 1.22.0 -> 1.23.0: BuildSpec sources[].schema(SchemaContract)에 rename과
# derived를 추가한다(#611, additive — 기존 필드/동작 불변). rename은 원 필드명 →
# canonical 컬럼명 매핑으로 캐스팅보다 먼저 적용되고, derived는
# date_parts/join_key typed 규칙으로 캐스팅 뒤에 새 컬럼을 만든다. 손으로 쓴
# YAML만 지원하던 선언을 공개 계약에 올려 Studio와 타입 생성기가 발견/타이핑할
# 수 있게 한다.
# 1.23.0 -> 1.24.0: SchemaContract에 read_as와 null_tokens를 추가한다(#613,
# additive — 기존 필드/동작 불변). read_as는 레코드마다 타입이 다른 원천 컬럼을
# 선언으로 읽고(CSV는 파싱 단계에서 적용되어 앞자리 0이 보존된다), null_tokens는
# 결측을 나타내는 원천 표기를 캐스팅 전에 null로 모은다.
# 1.24.0 -> 1.25.0: SchemaContract에 coalesce와 zfill을 추가한다(#620, additive
# — 기존 필드/동작 불변). coalesce는 세대별 alias 컬럼을 하나의 canonical 컬럼으로
# 모으고(겹치는 그룹은 결과가 선언 순서에 달리므로 거부한다), zfill은 canonical
# 식별자를 선언된 폭으로 왼쪽 0 padding 한다. casts의 dtype 어휘에 year_month가
# 더해진다.
# 1.25.0 -> 1.26.0: SchemaContract에 column_null_tokens를 추가한다(#623,
# additive — 기존 필드/동작 불변). 같은 의미의 결측이 컬럼마다 다르게 표기되는
# 원천을 다른 컬럼의 의미를 바꾸지 않고 선언한다. 한 컬럼에서 인정하는 결측
# 표현은 전역 null_tokens + 그 컬럼의 선언이며, 컬럼별 선언이 전역을 덮어쓰지
# 않는다.
# 1.27.0 -> 1.28.0: 관리자 전용 GET /admin/runs 와 GET /admin/config 를 추가한다
# (#679, additive — 기존 경로·동작은 변경되지 않는다). 둘 다 메타데이터만 돌려주고
# 산출물 바이트나 credential 은 포함하지 않는다.
# 1.26.0 -> 1.27.0: SourceRef 에 param_grid 를 추가한다(#613, additive — 기존
# 필드/동작 불변). 한 source 가 여러 파라미터 조합을 반복 호출하고 결과를 하나의
# dataset 으로 이어붙인다. 전개 순서는 계약이다(키 이름순, 마지막 키가 가장 빨리
# 변함, 축 안에서는 선언 순서 유지) — 순서가 바뀌면 Bronze 바이트가 바뀌어
# 재빌드 결정성이 깨진다.
API_CONTRACT_VERSION = "1.28.0"


def _quality_result_to_json(r: QualityCheckResult) -> dict[str, JsonValue]:
    """QualityCheckResult를 wire JSON으로 변환한다 (#486)."""
    return {
        "source_key": r.source_key,
        "category": r.category,
        "rule": r.rule,
        "column": r.column,
        "status": r.status,
        "actual": cast(JsonValue, r.actual),
        "threshold": r.threshold,
        "affected_rows": r.affected_rows,
        "evaluated_rows": r.evaluated_rows,
        "detail": r.detail,
    }


#: manifest 상태 어휘(ok/failed/cancelled) → publish 상태 어휘 (#481, #491).
#: 두 어휘를 잇는 자리를 하나로 두어, publish 경로가 상태를 따로 파생시키다
#: 정본과 어긋나는 일을 막는다.
_MANIFEST_TO_PUBLISH_STATUS: dict[str, publish_service.RunStatus] = {
    "ok": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _parse_spec_text(spec_yaml: str) -> BuildSpec:
    """YAML 텍스트를 BuildSpec으로 파싱한다.

    문법이 깨진 YAML은 ``yaml.YAMLError`` 로 나오는데, 그것은 서버 결함이 아니라
    사용자가 보낸 입력의 문제다. SpecLoadError 로 옮겨 호출자가 다른 파싱 실패와
    똑같이 400 으로 다루게 한다 — 그러지 않으면 동기 경로는 500 을 돌려주고,
    비동기 worker 는 아예 종결하지 못한다.
    """
    try:
        raw = cast(object, yaml.safe_load(spec_yaml))
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"spec is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecLoadError("top-level YAML must be a mapping")
    return parse_spec(cast(dict[str, object], raw))


class BuilderService:
    """Builder 연산을 HTTP 전송과 무관하게 제공하는 서비스."""

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: Callable[..., SourceClient],
        query_service: QueryService | None = None,
        credential_repository: CredentialRepository | None = None,
        upload_repository: UploadRepository | None = None,
        provider_test_operation: ProviderTestOperation = default_provider_test,
        provider_test_timeout: float | None = None,
        async_max_workers: int = 10,
        async_max_queue_size: int = 10,
    ) -> None:
        self._output_root = output_root
        self._client_factory = client_factory
        self._build_index = make_build_index(output_root)  # #309, ADR 0003/0016
        self._store = make_artifact_store(output_root)  # ADR 0010/0016 (manifest 정본)
        # run event timeline 저장소(#496). `_upload_repository`와 동일하게 지연
        # 생성한다 — preview는 build를 전혀 실행하지 않아 event를 남길 일이
        # 없으므로("Preview writes no files" 기존 계약, #497 범위 밖 #496도
        # 동일), 실제로 처음 쓰일 때(build/submit_build/events 조회)까지
        # `_build_events.sqlite` 흔적을 남기지 않는다.
        self._event_store_lazy: BuildEventStore | None = None
        self._event_store_lock = threading.Lock()
        # kind="file" source(#498)의 업로드 저장소. 명시적으로 주입되지 않으면
        # 실제로 처음 쓰일 때까지 SQLite 파일을 만들지 않는다(지연 생성,
        # `_upload_repository` property) — credential repository(마스터 키
        # 미설정 시 None)처럼, 업로드 기능을 쓰지 않는 워크스페이스에는 흔적을
        # 남기지 않는다(예: preview는 파일을 하나도 쓰지 않는다는 기존 계약).
        self._upload_repository_override: UploadRepository | None = upload_repository
        self._upload_repository_lazy: UploadRepository | None = None
        self._upload_repository_lock = threading.Lock()
        # Monitoring API용 bounded latency recorder (#516). dispatch()가 매 요청
        # 처리 시간을 기록한다 — 인스턴스별로 분리해 테스트 간 상태가 섞이지 않는다.
        self._latency_recorder = monitoring_service.LatencyRecorder()
        # 인증 실패 스로틀. latency recorder와 같은 이유로 인스턴스별로 둔다.
        self._auth_throttle = AuthFailureThrottle()
        # 외부 publish side effect의 durable idempotency receipt. 객체 생성은
        # 파일을 만들지 않으며 최초 POST claim 때 SQLite를 지연 초기화한다.
        self._publish_receipts = publish_service.PublishReceiptStore(output_root)
        self._query_service = query_service or QueryService()
        repository = credential_repository or _credential_repository_from_env(output_root)
        self._credential_resolver = CredentialResolver(repository)
        self._provider_test_operation = provider_test_operation
        configured_timeout = os.environ.get(_PROVIDER_TEST_TIMEOUT_ENV)
        self._provider_test_timeout = (
            provider_test_timeout
            if provider_test_timeout is not None
            else float(configured_timeout or _DEFAULT_PROVIDER_TEST_TIMEOUT)
        )
        if self._provider_test_timeout <= 0:
            raise ValueError("provider test timeout must be positive")
        # provider 도메인은 자기 의존성만 받는 별도 서비스로 나갔다 (#596). 여기서는
        # 조립만 하고, BuilderService 의 provider 메서드는 얇은 위임으로 남는다.
        self._providers_service = ProvidersService(
            credential_resolver=self._credential_resolver,
            create_client=self._create_client,
            close_client=lambda client: (
                _close_request_client(client) if client is not None else None
            ),
            provider_test_operation=self._provider_test_operation,
            provider_test_timeout=self._provider_test_timeout,
        )
        # upload 저장소는 처음 필요할 때만 SQLite 를 만든다 (#498) — 저장소 객체가 아니라
        # property 를 호출하는 람다를 넘겨 그 지연 생성을 그대로 유지한다.
        self._uploads_service = UploadsService(repository=lambda: self._upload_repository)
        self._query_api = QueryApiService(output_root=self._output_root, engine=self._query_service)
        self._datasets_api = DatasetsApiService(
            output_root=self._output_root, build_index=self._build_index, store=self._store
        )
        self._stages_api = StagesApiService(output_root=self._output_root, store=self._store)
        self._builds_api = BuildArtifactsApiService(
            output_root=self._output_root,
            store=self._store,
            build_index=self._build_index,
            # 지연 생성을 유지하려고 값이 아니라 접근자를 넘긴다 (#496).
            event_store=lambda: self._event_store,
        )
        self._quality_api = QualityApiService(
            output_root=self._output_root, store=self._store, datasets=self._datasets_api
        )
        self._async_builds = AsyncBuildExecutor(
            max_workers=async_max_workers,
            max_queue_size=async_max_queue_size,
            # running job이 안전 경계에서 실제로 cancelled로 종결되는 순간 정확히
            # 한 번 호출된다 (#481). 종결 event는 job의 terminal 전이를 확정하는
            # 쪽에서만 남겨, queued 취소와 running 취소가 똑같이 run_cancelled
            # 하나로 끝나고 한 run에 종결 event가 둘 생기지 않는다.
            on_cancelled=self._record_run_cancelled,
        )
        # monitoring 은 async job registry 의 큐 상태를 읽으므로 그 뒤에 만든다.
        self._monitoring_api = MonitoringApiService(
            output_root=self._output_root,
            build_index=self._build_index,
            async_builds=self._async_builds,
            latency_recorder=self._latency_recorder,
        )
        # publish 도메인 (#637). async job registry 가 만들어진 뒤여야 한다 —
        # terminal 이 아닌 run 의 publish 를 막는 판정이 그 레지스트리를 읽는다.
        self._publish_api = PublishApiService(
            output_root=self._output_root,
            publish_receipts=self._publish_receipts,
            async_builds=self._async_builds,
            credential_repository=self._credential_resolver.repository,
        )

    @property
    def _event_store(self) -> BuildEventStore:
        """run event timeline 저장소를 지연 생성해 반환한다 (#496).

        처음 접근될 때만 ``_build_events.sqlite``를 만든다 — preview만 하는
        워크스페이스에는 흔적을 남기지 않는다(기존 "Preview writes no files"
        계약과 동일한 이유로 지연 생성한다).
        """
        if self._event_store_lazy is None:
            with self._event_store_lock:
                if self._event_store_lazy is None:
                    self._event_store_lazy = BuildEventStore(self._output_root)
        return self._event_store_lazy

    @property
    def _upload_repository(self) -> UploadRepository:
        """kind="file" source(#498)의 업로드 저장소를 지연 생성해 반환한다.

        명시적으로 주입됐으면 그대로 쓴다. 아니면 처음 호출될 때만
        SQLite 파일을 만든다 — preview/build가 업로드를 전혀 참조하지 않는
        워크스페이스에는 ``.service/uploads.sqlite3`` 흔적을 남기지 않는다.
        """
        if self._upload_repository_override is not None:
            return self._upload_repository_override
        with self._upload_repository_lock:
            if self._upload_repository_lazy is None:
                self._upload_repository_lazy = SQLiteUploadRepository(
                    self._output_root / ".service" / "uploads.sqlite3",
                    max_bytes=resolve_max_upload_bytes(),
                )
            return self._upload_repository_lazy

    def _upload_repository_for(self, spec: BuildSpec) -> UploadRepository | None:
        """spec에 ``kind="file"`` source가 있을 때만 업로드 저장소를 만든다 (#498).

        file source가 없는 preview/build는 이 property를 아예 건드리지 않아
        지연 생성이 실제로 지연되게 한다 — property 자체를 호출하면(설령
        결과를 안 쓰더라도) 매 요청마다 SQLite를 초기화하게 되므로 여기서
        먼저 필요 여부를 가른다.
        """
        if any(source.kind == "file" for source in spec.sources):
            return self._upload_repository
        return None

    def _create_client(
        self,
        principal: Principal | None = None,
        *,
        providers: Iterable[str] = (),
        timeout: float | None = None,
        resolved_provider_keys: Mapping[str, str] | None = None,
    ) -> SourceClient:
        """요청 principal의 credential로 격리된 새 provider client를 만든다."""
        provider_names = tuple(dict.fromkeys(providers))
        provider_keys = dict(resolved_provider_keys or {})
        if resolved_provider_keys is None and principal is not None and provider_names:
            provider_keys = self._credential_resolver.provider_keys(
                principal.owner_id, provider_names
            )

        kwargs: dict[str, object] = {}
        if provider_keys:
            if not _factory_accepts_keyword(self._client_factory, "provider_keys"):
                raise RuntimeError("client_factory cannot accept principal provider credentials")
            kwargs["provider_keys"] = provider_keys
            if not _factory_accepts_keyword(self._client_factory, "cache"):
                raise RuntimeError("client_factory cannot disable credential response cache")
            # kpubdata#263이 해결되기 전에는 credential이 cache key에 포함되지 않는다.
            # 사용자별 credential을 쓰는 service client는 환경설정과 무관하게 cache를 끈다.
            kwargs["cache"] = False
        if timeout is not None and _factory_accepts_keyword(self._client_factory, "timeout"):
            kwargs["timeout"] = timeout
        return self._client_factory(**kwargs)

    def _runtime_providers(self) -> tuple[ProviderDescriptor, ...] | ServiceResponse:
        return self._providers_service.runtime_providers()

    def _known_provider(self, provider: str) -> ProviderDescriptor | ServiceResponse:
        return self._providers_service.known_provider(provider)

    def providers(self, *, principal: Principal) -> ServiceResponse:
        """런타임 Provider 목록과 현재 principal의 configured 상태를 반환한다."""
        return self._providers_service.providers(principal=principal)

    def provider_status(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal credential로 lightweight connection test를 수행한다."""
        return self._providers_service.provider_status(provider, principal=principal)

    def provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """원문 없이 현재 principal의 저장 credential 메타데이터를 반환한다."""
        return self._providers_service.provider_credential(provider, principal=principal)

    def put_provider_credential(
        self,
        provider: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """현재 principal의 Provider credential을 생성 또는 교체한다."""
        return self._providers_service.put_provider_credential(provider, body, principal=principal)

    def delete_provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal의 Provider credential만 삭제한다."""
        return self._providers_service.delete_provider_credential(provider, principal=principal)

    def create_upload(
        self,
        raw: bytes,
        *,
        format: str,  # noqa: A002 - 계약 필드명과 맞춘다
        encoding: str,
        original_filename: str | None,
        principal: Principal,
    ) -> ServiceResponse:
        """업로드 content를 저장하고 즉시 파싱 가능한지 검증한다 (#498)."""
        return self._uploads_service.create_upload(
            raw,
            format=format,
            encoding=encoding,
            original_filename=original_filename,
            principal=principal,
        )

    def get_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal 소유 업로드의 안전한 메타데이터만 반환한다 (content 제외)."""
        return self._uploads_service.get_upload(upload_id, principal=principal)

    def delete_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """현재 principal 소유 업로드만 삭제한다."""
        return self._uploads_service.delete_upload(upload_id, principal=principal)

    def query(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """서버가 resolve한 stage 테이블에 대해 검증된 SQL 쿼리 1건을 실행한다."""
        return self._query_api.query(body, principal=principal)

    def version(self) -> ServiceResponse:
        """Builder API 계약 버전을 반환한다 (#209).

        소비자(Studio 등)가 호출 전에 계약 호환성을 확인할 수 있는 메타 엔드포인트다.
        """
        return ServiceResponse(
            200, {"service": "kpubdata-builder", "api_version": API_CONTRACT_VERSION}
        )

    def catalog(self) -> ServiceResponse:
        """사용 가능한 provider/dataset 카탈로그를 반환한다 (#416, BL2, #436).

        kpubdata Client의 provider별 공개 ``datasets.list(provider=...)``로 데이터셋을
        조회한다. KRX의 명시된 optional pandas 누락만 격리하고 다른 오류는 실패로
        드러낸다. Provider 목록은 runtime registry에서 얻는다 (ADR 0011 — Builder가
        하드코딩하지 않는다). 이전에는 ``getattr(client, "_catalog")`` private
        접근 + 8개 provider 하드코딩 튜플을 써서 kpubdata에 provider가 추가돼도
        카탈로그에 안 떴다 (#436). 시크릿 값은 노출하지 않고 필요 여부만 표시한다.
        """
        client = self._create_client()
        try:
            runtime_catalog = runtime_provider_catalog(client)
        except Exception:
            # upstream 예외 문자열은 요청 URL 을 실어 나를 수 있고, 그 URL 에는
            # API 키가 쿼리 파라미터로 들어 있다 (providers_service 와 같은 이유).
            logger.exception("provider catalog unavailable")
            return ServiceResponse(502, {"error": "catalog unavailable"})
        finally:
            _close_request_client(client)

        providers_data: list[JsonValue] = [
            {
                "name": item.descriptor.name,
                "datasets": [
                    _catalog_dataset_body(
                        dataset,
                        item.descriptor.requires_credential
                        or bool(dataset.raw_metadata.get("service_key_param")),
                    )
                    for dataset in item.datasets
                ],
            }
            for item in runtime_catalog
        ]
        return ServiceResponse(200, {"providers": providers_data})

    def validate(self, spec_yaml: str) -> ServiceResponse:
        """BuildSpec을 파싱·검증한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            body: dict[str, JsonValue] = {"status": "invalid", "problems": list(exc.problems)}
            if exc.structured_problems:
                body["structured_problems"] = [
                    {"code": p.code, "path": p.path, "message": p.message, "hint": p.hint}
                    for p in exc.structured_problems
                ]
            return ServiceResponse(400, body)
        return ServiceResponse(
            200,
            {
                "status": "valid",
                "dataset_id": spec.dataset_id,
                "api_version": API_CONTRACT_VERSION,
            },
        )

    def preview(
        self,
        spec_yaml: str,
        *,
        limit: int = DEFAULT_PREVIEW_LIMIT,
        sample_mode: str = "first",
        seed: int = DEFAULT_PREVIEW_SEED,
        principal: Principal | None = None,
    ) -> ServiceResponse:
        """각 소스의 스키마와 샘플 행, Source↔Silver diff를 산출한다 (파일 미기록, #497)."""
        if limit < 1 or limit > MAX_PREVIEW_LIMIT:
            return ServiceResponse(
                400, {"error": f"'limit' must be a positive integer up to {MAX_PREVIEW_LIMIT}"}
            )
        if sample_mode not in ("first", "random"):
            return ServiceResponse(400, {"error": "'sample_mode' must be 'first' or 'random'"})
        if not isinstance(seed, int) or isinstance(seed, bool):
            return ServiceResponse(400, {"error": "'seed' must be an integer"})
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        # provider credential은 kind="public_api" source에만 의미가 있다(#498) —
        # file/url source의 provider는 항상 빈 문자열이므로 섞으면 credential
        # resolver에 의미 없는 provider 이름이 전달된다.
        provider_names = tuple(
            source.provider for source in spec_or_error.sources if source.kind == "public_api"
        )
        try:
            provider_keys = (
                self._credential_resolver.provider_keys(principal.owner_id, provider_names)
                if principal is not None
                else {}
            )
            client = self._create_client(
                principal,
                providers=provider_names,
                resolved_provider_keys=provider_keys,
            )
        except (ProviderCredentialConflictError, ValueError) as exc:
            return ServiceResponse(400, {"error": str(exc)})
        except Exception:
            return ServiceResponse(502, {"error": "provider client unavailable"})
        try:
            result = preview_build(
                spec_or_error,
                client=client,
                limit=limit,
                sample_mode=cast(SampleMode, sample_mode),
                seed=seed,
                upload_repository=self._upload_repository_for(spec_or_error),
                owner_id=principal.owner_id if principal is not None else None,
            )
        finally:
            _close_request_client(client)
        previews: list[JsonValue] = [
            {
                "source_key": p.source_key,
                "status": p.status,
                "error": _redact_secret_text(p.error, provider_keys.values()),
                "schema": [
                    {
                        "name": column.name,
                        "dtype": column.dtype,
                        "nullable": column.nullable,
                        "unique_count": column.unique_count,
                    }
                    for column in p.schema.columns
                ],
                "sample": list(p.preview.rows),
                "total_rows": p.preview.total_rows,
                "statistics": {
                    "row_count": p.statistics.row_count,
                    "null_counts": dict(p.statistics.null_counts),
                    "duplicate_rate": p.statistics.duplicate_rate,
                },
                "quality_results": cast(
                    JsonValue, [_quality_result_to_json(r) for r in p.quality_results]
                ),
                "source_sample": list(p.source_sample),
                "sample_mode": p.sample_mode,
                "diff_available": p.diff_available,
                "diffs": cast(
                    JsonValue,
                    [
                        {
                            "row": d.row,
                            "column": d.column,
                            "before": d.before,
                            "after": d.after,
                            "transform": d.transform,
                        }
                        for d in p.diffs
                    ],
                ),
                "transform_summary": (
                    {
                        "changed_cells": p.transform_summary.changed_cells,
                        "changed_rows": p.transform_summary.changed_rows,
                    }
                    if p.transform_summary is not None
                    else None
                ),
                "diff_truncated": p.diff_truncated,
            }
            for p in result.previews
        ]
        return ServiceResponse(200, {"dataset_id": spec_or_error.dataset_id, "previews": previews})

    def build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
        manifest_owner_id: str | None = None,
        credential_owner_id: str | None = None,
        principal: Principal | None = None,
        cancellation: CancellationProbe | None = None,
    ) -> ServiceResponse:
        """파이프라인을 실행하고 결과를 반환한다.

        응답 코드 정책:
            - 모든 소스 성공: 200
            - 하나라도 소스 fetch/stage가 실패: 502 (upstream 소스 의존 실패).
              매니페스트는 partial 정책으로 남기 때문에 body에 outcomes와 manifest가
              실린다.

        ``owner_id``는 canonical stable owner identity다(#505). ``created_by``는
        기존(#388) display/legacy 라벨로 계속 함께 기록된다 — wire 계약은 바뀌지
        않는다.

        ``manifest_owner_id``는 persisted manifest ownership(및 그 manifest를
        읽어 채우는 BuildIndex, #505 SSOT)에만 쓰이는 별도 값이다 — ``owner_id``
        (kind="file" source resolver의 업로드 소유권 확인용, #498)와 분리하기
        위한 것으로, 생략하면 ``owner_id``를 그대로 쓴다(기존 동작과 동일).
        async run(``_run_build_job``)이 이 필드로 submitting principal의
        owner_id를 manifest/BuildIndex에는 기록하면서도 file resolver에는
        여전히 owner_id를 넘기지 않는 데 쓴다(#496 follow-up).

        ``credential_owner_id``는 async worker가 submitting principal의 stable
        identity로 public_api credential만 해석하기 위한 내부 값이다. file source
        resolver에는 전달하지 않으며, request principal이 있으면 principal의
        owner_id가 항상 우선한다(ADR 0012, ADR 0014).

        ``cancellation``은 async job(#481)에서만 전달되는 협력적 취소 probe다.
        ``None``이면(동기 ``POST /build``, CLI) 파이프라인이 취소 점검을 전혀
        하지 않아 기존 동작과 100% 동일하다 — 동기 build에 취소 상태를 강요하지
        않는다. 취소로 끝난 run은 409와 함께 ``status="cancelled"`` 요약을
        반환하는데, 이 응답은 async worker(``AsyncBuildExecutor._run``)만 보고
        job snapshot에도 실리지 않으므로 HTTP wire에는 노출되지 않는다 —
        부분 산출물의 정본은 partial manifest다.
        """
        spec_or_error = self._load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error

        # provider credential은 kind="public_api" source에만 의미가 있다(#498) —
        # file/url source의 provider는 항상 빈 문자열이다.
        provider_names = tuple(
            source.provider for source in spec_or_error.sources if source.kind == "public_api"
        )
        provider_owner_id = principal.owner_id if principal is not None else credential_owner_id
        try:
            provider_keys = (
                self._credential_resolver.provider_keys(provider_owner_id, provider_names)
                if principal is not None or credential_owner_id is not None
                else {}
            )
            client = self._create_client(
                principal,
                providers=provider_names,
                resolved_provider_keys=provider_keys,
            )
        except (ProviderCredentialConflictError, ValueError) as exc:
            return ServiceResponse(400, {"error": str(exc)})
        except Exception:
            return ServiceResponse(502, {"error": "provider client unavailable"})
        try:
            result = run_build(
                spec_or_error,
                client=client,
                output_root=self._output_root,
                run_id=run_id,
                created_by=created_by,
                owner_id=owner_id,
                manifest_owner_id=manifest_owner_id,
                upload_repository=self._upload_repository_for(spec_or_error),
                event_store=self._event_store,
                cancellation=cancellation,
            )
        finally:
            _close_request_client(client)
        secret_values = tuple(provider_keys.values())
        if secret_values:
            try:
                manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
                redacted_manifest = _redact_json_secrets(manifest_data, secret_values)
                if redacted_manifest != manifest_data:
                    result.manifest_path.write_text(
                        json.dumps(redacted_manifest, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            except (OSError, json.JSONDecodeError):
                return ServiceResponse(500, {"error": "failed to secure build manifest"})
        outcomes: list[JsonValue] = [
            {
                "source_key": outcome.source_key,
                "status": outcome.status,
                "stages_completed": list(outcome.stages_completed),
                "error": _redact_secret_text(outcome.error, secret_values),
            }
            for outcome in result.outcomes
        ]
        # 취소된 run(#481)은 성공도 실패도 아니다. 이 응답은 async worker만 보고
        # job snapshot(BuildJob.response)에도 실리지 않으므로 wire 계약(200/502)을
        # 넓히지 않는다 — outcomes도 싣지 않아 SourceOutcome enum(ok/failed)과
        # 어긋나지 않는다. 부분 산출물의 정본은 아래에서 이미 기록된 partial
        # manifest다.
        cancelled = result.status == "cancelled"
        status_code = 200 if result.status == "ok" else 409 if cancelled else 502
        body: dict[str, JsonValue] = {
            "status": result.status,
            "run_id": result.context.run_id,
            "manifest": str(result.manifest_path),
            "api_version": API_CONTRACT_VERSION,
        }
        if not cancelled:
            body["outcomes"] = outcomes
        # composition(#506) 결과는 outcomes(소스별)와 별도로 노출한다 — bronze/silver/gold
        # stage 개념이 없고 "combined result"로 명확히 구분되어야 하기 때문이다.
        # BuildSpec.composition이 없으면 null이다.
        if result.composition_outcome is not None:
            body["composition"] = {
                "name": result.composition_outcome.name,
                "status": result.composition_outcome.status,
                "error": _redact_secret_text(result.composition_outcome.error, secret_values),
            }
        else:
            body["composition"] = None
        # 실패한 빌드는 첫 번째 실패 outcome의 error를 최상위 `error` 요약으로 노출해,
        # Studio 같은 소비자가 outcomes 배열을 파싱하지 않고도 사람이 읽을 수 있는
        # 사유를 즉시 표면화할 수 있게 한다 (#226). composition만 실패하고 모든 source가
        # 성공한 경우도 놓치지 않도록 composition_outcome도 함께 살핀다 (#506).
        if result.status == "failed":
            first_error = next(
                (o.error for o in result.outcomes if o.status != "ok" and o.error), None
            )
            if first_error is None and result.composition_outcome is not None:
                first_error = result.composition_outcome.error
            body["error"] = _redact_secret_text(first_error, secret_values) or "build failed"

        # ADR 0003: 빌드 완료 후 인덱스 갱신 (best-effort, 실패해도 빌드 성공 유지)
        try:
            manifest_data = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            started_at = manifest_data.get("started_at")
            finished_at = manifest_data.get("finished_at")
            self._build_index.insert_or_replace(
                run_id=result.context.run_id,
                status=result.status,  # type: ignore[arg-type]
                started_at=started_at,
                finished_at=finished_at,
                spec_digest=result.spec_digest,
                created_by=manifest_data.get("created_by"),
                owner_id=manifest_data.get("owner_id"),
                # dataset_id는 이 run이 실제로 실행한 canonical spec에서 곧바로 얻는다
                # (snapshot과 동일한 값) — 파생 검색값일 뿐이므로 별도로 추측하지 않는다 (#488).
                dataset_id=spec_or_error.dataset_id,
            )
            # ADR 0016: manifest 문서를 authoritative store 로 승격한다. sqlite/local 은
            # FS 파일이 정본이라 동일 내용 재기록(무해), cubrid 는 CUBRID 행 정본 + FS 미러.
            # best-effort — FS 에 이미 정본/미러가 있으므로 승격 실패가 빌드를 실패시키지 않는다.
            self._store.put_manifest(result.context.run_id, manifest_data)
        except Exception:
            # 인덱스 갱신/manifest 승격 실패는 빌드를 실패시키지 않는다 (ADR 0003) —
            # FS 에 이미 정본/미러가 있기 때문이다. 다만 **조용히** 넘기지는 않는다.
            # 아무 기록도 남기지 않던 시절에는 BuildIndex 가 얼마나 오래, 왜
            # 뒤처졌는지 알 방법이 없었다. 목록 조회가 완료된 run 을 빠뜨려도
            # 원인을 찾을 단서가 없었다는 뜻이다.
            logger.exception(
                "build index update or manifest promotion failed; "
                "the run itself succeeded and the filesystem copy is authoritative",
                extra={"run_id": result.context.run_id},
            )

        return ServiceResponse(status_code, body)

    def submit_build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """비동기 build job을 큐에 넣고 초기 상태를 반환한다 (#482).

        ``owner_id``는 job registry snapshot에 보존된다 — wire 응답
        (``to_body()``)에는 노출되지 않는다. registry에 남은 이 값은 두 곳에
        쓰인다: (1) active run(``check_active_run_access``, #496 follow-up)
        ownership 판정, (2) ``_run_build_job``이 이를 ``build()``의
        ``manifest_owner_id``로 넘겨 persisted manifest/BuildIndex(#505 SSOT)
        에 정확한 owner_id를 기록. ``build()``의 ``owner_id``(``kind="file"``
        source resolver용, #498)에는 여전히 전달하지 않는다 — async
        file-backed source owner propagation 한계는 그대로 유지된다.
        """
        resolved_run_id = run_id or generate_run_id()
        if self._build_index.get(resolved_run_id) is not None:
            return ServiceResponse(
                409,
                {
                    "error": "run_id already completed",
                    "run_id": resolved_run_id,
                },
            )

        def _record_run_submitted() -> None:
            # AsyncBuildExecutor.submit()이 job을 worker pool에 큐잉하기 *전에*
            # 호출된다(#496) — "existing"/"queue_full"이면 새 submission이 아니므로
            # 아예 호출되지 않는다. store.append()는 실패를 삼키지 않고 그대로
            # 전파한다(BuildEventStore, event timeline의 유일한 정본) — 여기서는
            # 그 전파를 그대로 둔다. job이 아직 큐잉되지 않은 시점이라, 이 event가
            # 기록되지 못하면 job도 만들어지지 않는다: "event는 유실됐는데 job은
            # 이미 실행 중"이라는 모순이 생기지 않는다(recorder의 흡수 정책과
            # 달리, 여기는 아직 다른 정본을 침범할 real side effect가 없다).
            self._event_store.append(
                BuildEvent(
                    seq=0,
                    timestamp=datetime.now(tz=timezone.utc),
                    run_id=resolved_run_id,
                    event="run_submitted",
                    status="ok",
                    message="build accepted for async execution",
                )
            )

        def _record_enqueue_failure() -> None:
            # registry.mark_failed() 직후, 예외 재전파 *전에* 호출된다(#496
            # lifecycle 계약: timeline 자체도 이 실패를 표현해야 한다) —
            # run_submitted는 이미 기록됐으니 지우지 않고(append-only), 기존
            # "run_failed" vocabulary로 동일 run_id에 종결 event를 하나 더
            # 남긴다. raw exception/stack trace는 담지 않는다 — bounded,
            # 안전한 고정 message만 쓴다(recorder의 다른 event들과 동일한
            # 방어 원칙). 이 append 자체가 실패해도 로그만 남기고 흡수한다 —
            # 이미 registry가 "failed"로 확정된 뒤라, 이 부가 event 기록
            # 실패가 원래 enqueue 실패(재전파될 예외)를 가리면 안 된다.
            try:
                self._event_store.append(
                    BuildEvent(
                        seq=0,
                        timestamp=datetime.now(tz=timezone.utc),
                        run_id=resolved_run_id,
                        event="run_failed",
                        status="fail",
                        message="build could not be queued for execution",
                    )
                )
            except Exception:
                logger.error(
                    "failed to record run_failed event after enqueue failure (run_id=%s)",
                    resolved_run_id,
                    exc_info=True,
                )

        try:
            result = self._async_builds.submit(
                spec_yaml=spec_yaml,
                run_id=resolved_run_id,
                created_by=created_by,
                owner_id=owner_id,
                runner=self._run_build_job,
                on_accept=_record_run_submitted,
                on_enqueue_failure=_record_enqueue_failure,
            )
        except Exception:
            # run_submitted event append 실패, 또는 event는 기록됐지만 이후
            # worker pool 큐잉(executor.submit) 자체가 실패한 경우 둘 다
            # 여기로 온다 — 두 경우 모두 job은 실행되지 않으므로 202를 주지
            # 않는다(#496).
            logger.error(
                "failed to accept build submission; build was not queued (run_id=%s)",
                resolved_run_id,
                exc_info=True,
            )
            return ServiceResponse(500, {"error": "failed to accept build submission"})
        match result.status:
            case "accepted":
                if result.snapshot is None:
                    raise RuntimeError("accepted async build is missing snapshot")
                return ServiceResponse(202, result.snapshot.to_body())
            case "existing":
                if result.snapshot is None:
                    raise RuntimeError("existing async build is missing snapshot")
                return ServiceResponse(200, result.snapshot.to_body())
            case "queue_full":
                return ServiceResponse(429, {"error": "async build queue is full"})
            case unreachable:
                assert_never(unreachable)

    def build_status(self, run_id: str) -> ServiceResponse:
        """active/terminal 비동기 build job 상태를 반환한다 (#482).

        registry 는 terminal job 을 256개까지만 들고 있다(#666). 그보다 오래된
        run 을 registry 만 보고 404 로 답하면, 끝난 적이 있는 run 이 시간이
        지났다는 이유로 "없는 run" 이 된다 — 산출물과 manifest 는 디스크에
        그대로 있는데도. 그래서 registry 에 없으면 persisted manifest 로
        내려간다. manifest 가 종단 상태의 정본이므로(``status_from_manifest``)
        이 경로가 registry 캐시보다 오히려 권위 있다.

        manifest 도 없을 때만 404 다 — 그건 이 서버가 한 번도 만들지 않았거나,
        pipeline 진입 전에 끝나 아무 것도 남기지 않은 run 이다.
        """
        try:
            validate_path_segment(run_id, field_name="run_id")
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        snapshot = self._async_builds.get(run_id)
        if snapshot is not None:
            return ServiceResponse(200, snapshot.to_body())
        evicted = self._build_status_from_manifest(run_id)
        if evicted is not None:
            return ServiceResponse(200, evicted)
        return ServiceResponse(404, {"error": f"build job not found: {run_id}"})

    def _build_status_from_manifest(self, run_id: str) -> dict[str, JsonValue] | None:
        """evict 된 terminal job 의 상태를 persisted manifest 에서 복원한다.

        registry snapshot 과 같은 모양으로 맞춘다 — 호출자가 두 경로를 구분할
        필요가 없어야 한다. ``response`` 는 싣지 않는다: 그건 build 응답 본문의
        메모리 캐시였고 manifest 에서 복원할 수 있는 값이 아니다. 없는 것을
        지어내는 대신 빼는 쪽이 맞다.
        """
        manifest = self._store.get_manifest(run_id)
        if manifest is None:
            return None
        manifest_status = status_from_manifest(manifest)
        status = "succeeded" if manifest_status == "ok" else manifest_status
        started = manifest.get("started_at")
        finished = manifest.get("finished_at")
        body: dict[str, JsonValue] = {
            "run_id": run_id,
            "status": status,
            "created_at": started if isinstance(started, str) else "",
            "updated_at": finished if isinstance(finished, str) else "",
        }
        created_by = manifest.get("created_by")
        if isinstance(created_by, str):
            body["created_by"] = created_by
        # error 는 싣지 않는다. manifest 의 error 문자열에는 경로가 섞일 수 있고
        # (#664 와 같은 이유), 상세는 ``GET /builds/{run_id}/manifest`` 가 이미
        # 제공한다 — 여기서 중복해 내보낼 이유가 없다.
        return body

    def cancel_build(self, run_id: str) -> ServiceResponse:
        """active(queued/running) async build job의 취소를 요청한다 (#481).

        호출 전에 route가 run_id 형식 검증과 ownership 게이팅을 끝내 놓아야 한다
        (``routes/builds.py`` — ``GET /builds/{run_id}``와 동일한 canonical 규칙).

        응답은 registry의 원자적 판정(``request_cancel``) 하나로 결정되므로
        경합과 무관하게 결정적이다.

        - ``queued`` → 즉시 ``cancelled``(runner를 한 번도 실행하지 않는다), 200.
        - ``running`` → ``cancelling``, 200. 실제 종결은 다음 안전 경계다.
        - 이미 ``cancelling``/``cancelled`` → 200(멱등, 현재 snapshot 그대로).
        - ``succeeded``/``failed``이거나 pipeline이 이미 정상 종료로 확정한 job
          → 409. 새 오류 어휘를 만들지 않고 기존 conflict 관례를 쓴다
          (``POST /builds``의 "이미 완료된 run_id" 409와 같은 의미의 충돌이다).
        - registry가 모르는 run → 404 (``GET /builds/{run_id}``와 동일 메시지).
        """
        outcome, snapshot = self._async_builds.request_cancel(run_id)
        if outcome == "unknown" or snapshot is None:
            return ServiceResponse(404, {"error": f"build job not found: {run_id}"})
        if outcome == "terminal":
            return ServiceResponse(
                409,
                {
                    "error": "build job is no longer cancellable",
                    "run_id": run_id,
                    "status": snapshot.status,
                },
            )
        if outcome == "cancelled":
            # queued job은 여기서 이미 종결됐다 — worker는 runner를 실행하지
            # 않는다(``AsyncBuildExecutor._run``의 ``begin_run`` 게이트). 종결
            # event는 running 경로와 동일하게 terminal 전이 시점에 한 번 남긴다.
            self._record_run_cancelled(run_id)
        return ServiceResponse(200, snapshot.to_body())

    def _record_run_cancelled(self, run_id: str) -> None:
        """cancelled 종결 event를 남긴다 (#481). 실패해도 전파하지 않는다.

        job은 이 시점에 이미 ``cancelled``로 확정돼 있다 — event append 실패가
        확정된 종단 상태를 되돌릴 수는 없으므로, ``_record_enqueue_failure``와
        동일하게 로그만 남기고 흡수한다. worker thread에서도 호출되므로 예외를
        전파하면 안 된다. message는 고정 문자열이며 raw exception/경로/자격증명을
        담지 않는다.
        """
        try:
            self._event_store.append(
                BuildEvent(
                    seq=0,
                    timestamp=datetime.now(tz=timezone.utc),
                    run_id=run_id,
                    event="run_cancelled",
                    status="ok",
                    message="build cancelled at a safe stage boundary",
                )
            )
        except Exception:
            logger.error("failed to record run_cancelled event (run_id=%s)", run_id, exc_info=True)

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        """async job registry가 부르는 실제 실행 진입점 (#482, #496 follow-up).

        ``build()``에 file resolver용 ``owner_id``는 전달하지 않는다(``None`` 그대로) —
        ``kind="file"`` source resolver는 async 경로에서 여전히 stable owner
        identity를 알지 못한다(#498 async limitation 유지). registry snapshot에
        이미 보존된(``submit_build``가 채운) submitting principal의 owner_id는
        public_api credential 해석용 ``credential_owner_id``와 persisted manifest
        ownership용 ``manifest_owner_id``로만 넘긴다 — persisted manifest
        (및 그 manifest를 그대로 읽어 채우는 BuildIndex, #505 SSOT)는
        ``build()`` 내부에서 단 한 번의 write로 정확한 owner_id를 갖게 되고,
        build 종료 후 manifest를 별도로 사후 수정할 필요가 없다.
        """
        snapshot = self._async_builds.get(run_id)
        manifest_owner_id = snapshot.owner_id if snapshot is not None else None
        return self.build(
            spec_yaml,
            run_id=run_id,
            created_by=created_by,
            manifest_owner_id=manifest_owner_id,
            credential_owner_id=manifest_owner_id,
            # 협력적 취소 probe(#481)를 그대로 pipeline까지 내려보낸다 — service
            # 개념(registry/HTTP/Principal)은 pipeline domain으로 넘기지 않는다.
            cancellation=cancellation,
        )

    # --- build 산출물 조회 (#637) -------------------------------------------
    #
    # artifacts/manifest/spec/파일 서빙/build 목록/event timeline 은
    # BuildArtifactsApiService 가 들고 있다. 빌드를 실행하는 쪽은 여기 남는다 —
    # 그쪽은 의존성이 열한 가지라 지금 떼면 경계가 좁아지지 않는다.

    def artifacts(self, run_id: str) -> ServiceResponse:
        """run의 산출물 목록을 조회한다."""
        return self._builds_api.artifacts(run_id)

    def manifest(self, run_id: str) -> ServiceResponse:
        """run의 manifest를 조회한다."""
        return self._builds_api.manifest(run_id)

    def spec(self, run_id: str) -> ServiceResponse:
        """run의 BuildSpec 스냅샷을 조회한다 (#487)."""
        return self._builds_api.spec(run_id)

    def serve_artifact_file(self, run_id: str, file_path: str) -> ServiceResponse | FileResponse:
        """run 워크스페이스 안의 산출물 파일 하나를 서빙한다."""
        return self._builds_api.serve_artifact_file(run_id, file_path)

    def list_builds(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """접근 가능한 run 목록을 조회한다 (#433)."""
        return self._builds_api.list_builds(limit=limit, principal=principal)

    def get_build_events(self, run_id: str, *, limit: int, tail: bool) -> ServiceResponse:
        """run의 append-only structured event timeline을 조회한다 (#496)."""
        return self._builds_api.get_build_events(run_id, limit=limit, tail=tail)

    def _dataset_records(self, principal: Principal | None) -> list[datasets_service.RunRecord]:
        return self._datasets_api.dataset_records(principal)

    def _dataset_records_for(
        self, dataset_id: str, principal: Principal | None
    ) -> list[datasets_service.RunRecord]:
        return self._datasets_api.dataset_records_for(dataset_id, principal)

    def _recent_canonical_records(
        self, principal: Principal | None, *, now: datetime, window_seconds: int
    ) -> list[datasets_service.RunRecord]:
        return self._datasets_api.recent_canonical_records(
            principal, now=now, window_seconds=window_seconds
        )

    def list_datasets(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """동일 dataset_id의 여러 run을 하나의 built dataset으로 묶어 목록을 반환한다 (#488)."""
        return self._datasets_api.list_datasets(limit=limit, principal=principal)

    def get_dataset(
        self, dataset_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """단일 built dataset의 canonical 요약을 반환한다 (#488)."""
        return self._datasets_api.get_dataset(dataset_id, principal=principal)

    def list_dataset_runs(
        self, dataset_id: str, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """dataset_id의 접근 가능한 run history를 최신순으로 반환한다 (#488)."""
        return self._datasets_api.list_dataset_runs(dataset_id, limit=limit, principal=principal)

    def get_dataset_quality_history(
        self, dataset_id: str, *, limit: int = 30, principal: Principal | None = None
    ) -> ServiceResponse:
        """dataset_id의 접근 가능한 run들에 대한 quality 집계 이력 (#486)."""
        return self._datasets_api.get_dataset_quality_history(
            dataset_id, limit=limit, principal=principal
        )

    def get_build_quality(self, run_id: str) -> ServiceResponse:
        """run의 구조화된 Quality 결과와 schema drift를 조회한다 (#486, #514)."""
        return self._quality_api.get_build_quality(run_id)

    def quality_summary(
        self, *, window: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """최근 window 안 접근 가능한 run의 structured quality 집계 (#486 후속)."""
        return self._quality_api.quality_summary(window=window, principal=principal)

    # --- stage 조회 / observability (#637) ----------------------------------
    #
    # 단계별 산출물은 StagesApiService, /monitoring 은 MonitoringApiService 가
    # 들고 있다. 둘을 나눠 둔 이유는 #606 이 세운 경계와 같다 — 데이터가 어떤가와
    # 시스템이 어떤가는 한 응답에 섞지 않는다.

    def list_run_stages(self, run_id: str) -> ServiceResponse:
        """run의 단계별 산출물 목록을 조회한다 (#488)."""
        return self._stages_api.list_run_stages(run_id)

    def get_run_stage_detail(
        self, run_id: str, stage: str, source_key: str, *, limit: int
    ) -> ServiceResponse:
        """run의 특정 단계 상세를 조회한다 (#488)."""
        return self._stages_api.get_run_stage_detail(run_id, stage, source_key, limit=limit)

    def monitoring_summary(self) -> ServiceResponse:
        """큐/build/지연 요약을 조회한다 (#516)."""
        return self._monitoring_api.monitoring_summary()

    def monitoring_builds(
        self, *, window: str, bucket: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """window 안의 build 추이를 조회한다 (#516)."""
        return self._monitoring_api.monitoring_builds(
            window=window, bucket=bucket, principal=principal
        )

    # --- publish 도메인 (#637) ---------------------------------------------
    #
    # readiness/publish/receipt/reconcile/audit 은 PublishApiService 가 들고 있다.
    # 여기 남은 것은 같은 시그니처의 위임뿐이다 — route adapter 와 dispatch 가
    # 보는 표면은 그대로다.

    def publish_readiness(
        self,
        run_id: str,
        target: str,
        destination: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """GET /builds/{run_id}/publish/readiness (#491)."""
        return self._publish_api.publish_readiness(run_id, target, destination, owner_id)

    def publish(
        self,
        run_id: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """POST /builds/{run_id}/publish (#491)."""
        return self._publish_api.publish(run_id, body, principal=principal)

    def get_publish_receipt(
        self,
        run_id: str,
        target: str,
        destination: str,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """GET /builds/{run_id}/publish/receipt (#551)."""
        return self._publish_api.get_publish_receipt(
            run_id, target, destination, principal=principal
        )

    def publish_audit_log(self, run_id: str, *, principal: Principal) -> ServiceResponse:
        """GET /builds/{run_id}/publish/audit (#563)."""
        return self._publish_api.publish_audit_log(run_id, principal=principal)

    def reconcile_publish(
        self,
        run_id: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """POST /builds/{run_id}/publish/reconcile (#551)."""
        return self._publish_api.reconcile_publish(run_id, body, principal=principal)

    def reset_publish_receipt(
        self,
        run_id: str,
        target: str,
        destination: str,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """DELETE /builds/{run_id}/publish/receipt (#551)."""
        return self._publish_api.reset_publish_receipt(
            run_id, target, destination, principal=principal
        )

    def _load_validated(self, spec_yaml: str) -> BuildSpec | ServiceResponse:
        """spec_yaml을 파싱·검증하고, 실패 시 오류 ServiceResponse를 반환한다."""
        try:
            spec = _parse_spec_text(spec_yaml)
            validate_spec(spec)
        except SpecLoadError as exc:
            return ServiceResponse(400, {"status": "error", "error": str(exc)})
        except ValidationError as exc:
            body: dict[str, JsonValue] = {"status": "invalid", "problems": list(exc.problems)}
            if exc.structured_problems:
                body["structured_problems"] = [
                    {"code": p.code, "path": p.path, "message": p.message, "hint": p.hint}
                    for p in exc.structured_problems
                ]
            return ServiceResponse(400, body)
        return spec


def dispatch(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str = "",
    *,
    api_key: str | None = None,
    bearer_token: str | None = None,
    raw_body: bytes | None = None,
    client_id: str | None = None,
) -> ServiceResponse | FileResponse:
    """``_dispatch_impl``을 호출하고 처리 시간을 Monitoring latency 표본으로
    기록한다 (#516).

    타이밍은 라우팅+인증+비즈니스 로직 전체를 감싼다(HTTP 소켓 I/O는 제외 —
    그건 http.py 계층). metric 기록 실패가 요청 실패로 전파되지 않도록
    ``LatencyRecorder.record``가 이미 내부적으로 예외를 흡수한다.

    ``raw_body``는 ``POST /uploads``(#498)에서만 쓰이는 binary body다 — 다른
    모든 endpoint는 JSON ``body``만 사용하며 ``raw_body``는 None이다.
    """
    started = time.perf_counter()
    try:
        return _dispatch_impl(
            service,
            method,
            path,
            body,
            query,
            api_key=api_key,
            bearer_token=bearer_token,
            raw_body=raw_body,
            client_id=client_id,
        )
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000
        service._latency_recorder.record(elapsed_ms)


def _dispatch_impl(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str = "",
    *,
    api_key: str | None = None,
    bearer_token: str | None = None,
    raw_body: bytes | None = None,
    client_id: str | None = None,
) -> ServiceResponse | FileResponse:
    """(method, path)를 BuilderService 연산으로 라우팅한다.

    GET /healthz는 인증 없이 반환하고 (#372), 그 외 엔드포인트는
    authenticate()로 Principal을 얻어 인증 게이트를 통과한 후 라우팅한다.
    dev-mode이면 인증 생략, 그 외는 fail-closed(401)로 동작한다 (#248, #384).

    ``client_id``(HTTP 계층이 넘기는 TCP peer 주소)가 있으면 인증 실패를 세어
    반복 시도를 429로 끊는다. 실패 누적 자체를 인증보다 먼저 확인해, 막힌
    클라이언트는 키 비교·서명 검증 비용조차 발생시키지 못한다.

    반환값:
        ServiceResponse 또는 FileResponse (#323).
    """
    # /healthz는 인증 게이트 밖에서 무인증 노출 (#372).
    if method == "GET" and path == "/healthz":
        return ServiceResponse(200, {"status": "ok"})

    # 인증 실패가 누적된 클라이언트는 인증을 시도하기도 전에 끊는다.
    retry_after = service._auth_throttle.retry_after(client_id)
    if retry_after is not None:
        return ServiceResponse(
            429,
            {
                "error": "too many failed authentication attempts",
                "code": "auth_throttled",
                "retry_after_seconds": retry_after,
            },
        )

    # 인증 게이트 (#384): Principal을 얻지 못하면 401.
    principal = authenticate(api_key=api_key, bearer_token=bearer_token)
    if isinstance(principal, AuthError):
        # 401(잘못된 자격증명)만 센다 — 403은 유효한 토큰의 인가 실패라 추측 가치가
        # 없고, 503은 JWKS 일시 장애라 클라이언트 잘못이 아니다.
        if principal.status_code == 401:
            service._auth_throttle.record_failure(client_id)
        return ServiceResponse(principal.status_code, {"error": principal.reason})

    # 성공한 인증은 실패 기록을 비운다 — 토큰 만료로 몇 번 401을 받은 정상
    # 클라이언트가 이후 정상 사용 중에 스로틀에 걸리지 않는다.
    service._auth_throttle.record_success(client_id)

    # /uploads(#498)는 binary body(raw_body)가 필요한 유일한 endpoint라 표준
    # RouteAdapter(JSON body만 받음) 목록에 넣지 않고 여기서 직접 호출한다 —
    # 과거의 거대한 dispatch monolith를 복원하는 것이 아니라, 이 한 endpoint의
    # 전송 형태가 route adapter 계약과 다르기 때문이다.
    uploads_response = uploads_route.handle(
        service, method, path, principal, query=query, raw_body=raw_body
    )
    if uploads_response is not None:
        return uploads_response

    for adapter in ROUTE_ADAPTERS:
        response = adapter(service, method, path, body, query, principal)
        if response is not None:
            return response

    return ServiceResponse(404, {"error": f"not found: {method} {path}"})


__all__ = ["API_CONTRACT_VERSION", "BuilderService", "ServiceResponse", "FileResponse", "dispatch"]

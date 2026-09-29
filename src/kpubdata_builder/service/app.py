"""Builder service logic (#36).

Exposes validate/preview/build/artifacts operations as pure logic independent
of HTTP transport, so external UI like Studio can call Builder directly. Each
method returns a ServiceResponse (status code + JSON-serializable body), and
dispatch routes (method, path) to the corresponding operation.

Major components:
    - ServiceResponse: status code + body
    - BuilderService: validate/preview/build/artifacts operations
    - dispatch: path routing
"""

from __future__ import annotations

import inspect
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import __version__, logging_redaction
from ..credentials import (
    AesGcmCredentialCipher,
    CredentialRepository,
    SQLiteCredentialRepository,
)
from ..events import BuildEventStore
from ..pipeline import (
    DEFAULT_PREVIEW_SEED,
    CancellationProbe,
)
from ..query.service import QueryService
from ..spec import BuildSpec, JsonValue
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
from ..warehouse import TableCatalog
from . import datasets as datasets_service
from . import monitoring as monitoring_service
from . import ownership as ownership_module
from . import publish as publish_service
from .auth import AuthError, Principal, authenticate
from .auth_throttle import AuthFailureThrottle
from .build_runs_api import BuildRunsApiService
from .builds_api import BuildArtifactsApiService
from .datasets_api import DatasetsApiService
from .jobs import AsyncBuildExecutor
from .monitoring_api import MonitoringApiService
from .providers import (
    CredentialResolver,
    ProviderDescriptor,
    ProviderTestOperation,
    default_provider_test,
)
from .providers_service import ProvidersService
from .publish_api import PublishApiService
from .quality_api import QualityApiService
from .query_service_api import QueryApiService
from .responses import FileResponse, ServiceResponse
from .routes import ROUTE_ADAPTERS
from .routes import uploads as uploads_route

# Re-exported: the preview limit was part of this module before #596 moved preview out.
from .routes.core import MAX_PREVIEW_LIMIT as MAX_PREVIEW_LIMIT
from .spec_api import SpecApiService
from .stages_api import StagesApiService
from .uploads_service import UploadsService

logger = logging.getLogger(__name__)

_CREDENTIAL_MASTER_KEY_ENV = "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY"
_PROVIDER_TEST_TIMEOUT_ENV = "KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT"
_DEFAULT_PROVIDER_TEST_TIMEOUT = 10.0

# Recent-window quality aggregate uses this margin when narrowing candidates by
# canonical manifest.json mtime. mtime is the file's mtime at completion, so
# always >= finished_at; we lower the window lower bound by this amount to
# absorb rerecording after completion (e.g., secret redaction), clock skew, and
# filesystem mtime resolution. Exact boundary is reapplied canonically by
# quality.aggregate_quality_window.
_QUALITY_WINDOW_MTIME_MARGIN_SECONDS = 3600


# Defensive upper limit for /preview limit (#497). Previously had no limit — Preview
# retrieves all results, expensive when sources are large or paginated
@runtime_checkable
class _CloseableClient(Protocol):
    def close(self) -> None: ...


def _close_request_client(client: SourceClient) -> None:
    try:
        if isinstance(client, _CloseableClient):
            client.close()
    finally:
        # The client's keys stop being "in use" for log scrubbing once it is closed (#686).
        logging_redaction.release(client)


def _raise_provider_test_error(client: SourceClient, provider: str) -> None:
    """Always-raise operation for client creation failure fallback in provider_status."""
    raise RuntimeError()


def _credential_repository_from_env(output_root: Path) -> CredentialRepository | None:
    """Enable encrypted repository only if master key is set.

    Backend follows KPUBDATA_BUILDER_STORAGE_BACKEND (ADR 0016): if cubrid,
    share global Engine with CubridCredentialRepository; otherwise default
    SQLite file.
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
    """Check if factory accepts specific keyword or **kwargs without side effects."""
    try:
        parameters = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == keyword
        for parameter in parameters
    )


def _enforce_ownership() -> bool:
    """Check if run ownership enforcement is enabled (#389). Default off — backward compat."""
    return ownership_module.enforce_ownership()


# Build list entry type for API responses
_BuildListEntry = dict[str, str | None]


# Builder API contract version. Must match info.version in contract/builder-api.yaml
# (test_service_contract enforces this), returned in response so consumers like
# Studio can negotiate backward compatibility (#209).
# 1.4.0 -> 1.5.0: Added Dataset Catalog·Detail·Stage Summary API (#488, additive).
# 1.5.0 -> 1.6.0: Structured Quality/Schema Drift results, quality history/detail
# API added (#486, additive — existing endpoints unchanged).
# 1.8.0 -> 1.9.0: Added query startup/engine timing fields (#523, additive).
# 1.9.0 -> 1.10.0: POST /preview adds Source↔Silver diff and sample_mode
# (first/random) option (#497, additive — existing fields retained). Added limit
# cap (1000) is behavioral tightening (previously no limit) — higher values fail 400.
# 1.10.0 -> 1.11.0: Added GET /monitoring/summary, GET /monitoring/builds (#516,
# additive — existing endpoints unchanged). Async build queue/worker reflect
# read-only snapshot of existing AsyncBuildExecutor/AsyncBuildJobRegistry
# (#511/#513); in normal runtime availability=available.
# MonitoringSummaryResponse.status (healthy/degraded) is deterministic aggregate
# from required subsystem availability (latency threshold unused).
# 1.11.0 -> 1.12.0: Added BuildSpec.composition (JoinSpec) and composition key
# in POST /build response, manifest.composition (CompositionProvenance) (#506,
# additive — existing fields/endpoints unchanged).
# 1.12.0 -> 1.13.0: Added kind="file"/"url" to BuildSpec sources (existing
# provider/dataset sources interpreted as kind="public_api" additively), POST
# /uploads, GET /uploads/{upload_id}, DELETE /uploads/{upload_id} (#498,
# additive — existing endpoints/sources without kind unchanged). url source in
# P0 scope (GET, Auth=None, https only) defended by safe fetch against SSRF.
# 1.13.0 -> 1.14.0: Added GET /builds/{run_id}/events (#496, additive — existing
# endpoints unchanged). raw logger parsing not needed to query structured event
# append-only timeline for run/source fetch/medallion stage (bronze/silver/gold/
# export)/quality checkpoint. limit/tail query parameter bounded (default 200,
# cap 1000), always chronological ascending. Monitoring (#516) system aggregate
# separate role — this endpoint handles single run events only.
# 1.14.0 -> 1.15.0: Added discovery metadata (description/tags/source_url/
# representation/operations/query_support) to /catalog response (CatalogDataset)
# (#490, additive — existing fields retained, raw_metadata not exposed).
# 1.15.0 -> 1.16.0: Documented async build job surface in contract — POST
# /builds (202/200 idempotent/409/429) and GET /builds/{run_id} (job status
# polling) added (#480). Job status query applies ownership gate to block
# cross-owner build output exposure (behavioral tightening — previously unchecked).
# 1.16.0 -> 1.17.0: Added build publish surface (#491) — GET /builds/{run_id}/
# publish/readiness and POST /builds/{run_id}/publish (idempotent receipt, TOCTOU
# recheck).
# 1.17.0 -> 1.18.0: Added POST /builds/{run_id}/cancel (#481, ADR 0008 —
# additive). queued job cancelled immediately before execution; running job
# transitions through cancelling to cancelled at safe stage boundary (no forceful
# termination). BuildJobStatus vocabulary (queued/running/cancelling/succeeded/
# failed/cancelled) reused. Also additive: status/partial (partial artifacts flag)
# in BuildManifest, run_cancelled in BuildEventName, cancelled in BuildSummary
# .status enum (value already observed in BuildIndex/dataset contract, now
# actually observable).
# 1.18.0 -> 1.19.0: Added publish receipt operations path (#551, additive) —
# GET /builds/{run_id}/publish/receipt (query unknown state), POST
# /builds/{run_id}/publish/reconcile (check remote, confirm succeeded or reset
# for republish), DELETE /builds/{run_id}/publish/receipt (explicit reset, audit
# log). reconcile returns 503 without changes if remote unreachable.
# 1.19.0 -> 1.20.0: Added kaggle and local to HTTP publish targets (#550,
# additive). kaggle only when packaging dataset-metadata.json id matches
# destination (readiness blocker validation), local restricted to relative
# owner/name paths under KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT. GET readiness
# destination query parameter optional.
# 1.20.0 -> 1.21.0: Added publish audit log query (#563, additive) —
# GET /builds/{run_id}/publish/audit (reconcile/reset history, ownership gate).
# 1.21.0 -> 1.22.0: Studio Home dashboard reads authoritative aggregate without
# arbitrary numeric synthesis — two additive queries added (existing fields/
# behavior immutable):
#   - Total in GET /datasets response — distinct dataset count after canonical
#     grouping + ownership filter, before pagination (limit). Do not confuse
#     items.length/limit with total.
#   - GET /quality/summary?window=24h — summarize structured quality from
#     accessible runs in recent 24h as PASS/WARN/FAIL run counts (#486 domain
#     quality bounded cross-run aggregate). Keep separate from system
#     observability (/monitoring).
#   - Added request_parameters and application to CatalogDataset in GET /catalog
#     response (same unmerged release scope additive — existing fields/behavior
#     immutable). Serialized from raw_metadata request_parameters as secret-free
#     allowlist (name/required/description/example); secret parameters like
#     serviceKey excluded. Add Data uses selected Dataset's required request
#     parameters for early user guidance. Datasets without metadata yield empty
#     array. application is guidance when API Key issuance separate from
#     per-Dataset approval ({required, url}); null if absent from raw_metadata
#     (Builder/Studio not guessing approval status).
# 1.22.0 -> 1.23.0: Added rename and derived to BuildSpec sources[].schema
# (SchemaContract) (#611, additive — existing fields/behavior immutable). rename
# maps original field name → canonical column (applied before casting), derived
# creates new columns with date_parts/join_key typed rules (applied after
# casting). Hand-written YAML declarations now exposed in public contract so
# Studio and type generator can discover/type them.
# 1.23.0 -> 1.24.0: Added read_as and null_tokens to SchemaContract (#613,
# additive — existing fields/behavior immutable). read_as declares source columns
# with per-record type variation (CSV parsing applies at stage, preserving leading
# zeros), null_tokens collects source null representations before casting.
# 1.24.0 -> 1.25.0: Added coalesce and zfill to SchemaContract (#620, additive
# — existing fields/behavior immutable). coalesce gathers per-generation alias
# columns into one canonical column (overlapping groups rejected — result order
# differs per declaration), zfill left-pads canonical identifiers to declared
# width with zeros. casts dtype vocabulary adds year_month.
# 1.25.0 -> 1.26.0: Added column_null_tokens to SchemaContract (#623, additive
# — existing fields/behavior immutable). Declare source null representations
# varying per column without changing other columns' meaning. Null tokens
# accepted per column: global null_tokens + that column's declaration; per-column
# declaration does not override global.
# 1.27.0 -> 1.28.0: Added admin-only GET /admin/runs and GET /admin/config
# 1.28.0 -> 1.29.0: POST /build gains `materialized` when the deployment has a
#   warehouse — committed table snapshots per source (#703, additive). Absent, not
#   empty, when no warehouse is configured: an empty object would claim nothing was
#   committed, which a caller cannot tell from never having asked.
# (#679, additive — existing paths·behavior unchanged). Both return metadata only,
# no artifact bytes or credentials.
# 1.26.0 -> 1.27.0: Added param_grid to SourceRef (#613, additive — existing
# fields/behavior immutable). One source calls multiple parameter combinations
# repeatedly, concatenating results into one dataset. Expansion order is contract
# (by key name, last key fastest, declaration order within axes) — order changes
# break Bronze bytes, breaking rebuild determinism.
# 1.29.0 -> 1.30.0: column metadata gains logical_type and wire_encoding (#735,
#   additive) on /query (column_meta), /preview schema items and SilverColumnInfo.
#   Decimal columns and integer columns holding a value outside ±(2**53 - 1) are sent
#   as exact decimal text, because a JSON number is read as a double.
# 1.30.0 -> 1.31.0: GET /datasets/{dataset_id}/runs/{run_id} (studio#418, additive).
#   One run by id, not only the newest page; 404 when it is not the dataset's, 403
#   when it is but not the caller's.
# 1.31.0 -> 1.32.0: JoinSpec gains keys, cardinality and on_null_key; the manifest's
#   CompositionProvenance gains optional cardinality and ratio fields (#698, additive).
#   duplicate_key_warning is judged on keys present on both sides only.
# 1.32.0 -> 1.33.0: BuildSpec gains license_name and license_link, and publishes the
#   attribution it already accepted (#764, additive). `license: other` needs both.
# 1.34.0 -> 1.35.0: GET /version also reports the application version (#777, additive).
# 1.33.0 -> 1.34.0: the source_fetch_progress build event, one per finished param_grid
#   combination with metrics {done, total} (#648, additive).
API_CONTRACT_VERSION = "1.35.0"


#: manifest status vocabulary (ok/failed/cancelled) → publish status vocabulary
#: (#481, #491). Single mapping to avoid publish path deriving separate state,
#: preventing divergence from canonical.
_MANIFEST_TO_PUBLISH_STATUS: dict[str, publish_service.RunStatus] = {
    "ok": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
}


class BuilderService:
    """Service providing Builder operations independent of HTTP transport."""

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
        warehouse_root: Path | None = None,
    ) -> None:
        # Provider keys ride in request URLs, and the HTTP library logs those URLs (#686).
        logging_redaction.install()
        self._output_root = output_root
        # Configured, never taken from a request: a per-request path would let a
        # caller write a catalog anywhere the process can reach (#703).
        self._warehouse_root = warehouse_root
        self._catalog: TableCatalog | None = None
        self._client_factory = client_factory
        self._build_index = make_build_index(output_root)  # #309, ADR 0003/0016
        self._store = make_artifact_store(output_root)  # ADR 0010/0016 (canonical manifest)
        # Run event timeline store (#496). Lazily created like _upload_repository —
        # preview never executes build, leaving no events ("Preview writes no files"
        # existing contract, #497 scope excludes #496 similarly), so no
        # `_build_events.sqlite` footprint until actually needed (build/submit_build/
        # events query).
        self._event_store_lazy: BuildEventStore | None = None
        self._event_store_lock = threading.Lock()
        # Upload store for kind="file" source (#498). If not explicitly injected,
        # create SQLite only when actually needed (lazy creation, `_upload_repository`
        # property) — like credential repository (no master key → None), workspaces
        # not using uploads leave no footprint (e.g., preview never writes files,
        # existing contract).
        self._upload_repository_override: UploadRepository | None = upload_repository
        self._upload_repository_lazy: UploadRepository | None = None
        self._upload_repository_lock = threading.Lock()
        # Monitoring API bounded latency recorder (#516). dispatch() records each
        # request processing time — per-instance isolation prevents test state mixing.
        self._latency_recorder = monitoring_service.LatencyRecorder()
        # Auth failure throttle. Same isolation reason as latency recorder.
        self._auth_throttle = AuthFailureThrottle()
        # Durable idempotency receipt for external publish side effects. Object
        # creation does not create files; SQLite lazily initialized on first POST claim.
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
        # Provider domain moved to separate service (self-contained, #596). Here
        # only assembly; BuilderService provider methods remain thin delegates.
        self._providers_service = ProvidersService(
            credential_resolver=self._credential_resolver,
            create_client=self._create_client,
            close_client=lambda client: (
                _close_request_client(client) if client is not None else None
            ),
            provider_test_operation=self._provider_test_operation,
            provider_test_timeout=self._provider_test_timeout,
        )
        # Upload repository initialized only when needed (#498) — pass lambda,
        # not property value directly (#498) — calling property on every request
        # (even unused result) would eagerly initialize SQLite per request,
        # defeating lazy creation. Check need first here.
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
            # Lazy creation maintains constraint — pass accessor not value (#496).
            event_store=lambda: self._event_store,
        )
        self._quality_api = QualityApiService(
            output_root=self._output_root, store=self._store, datasets=self._datasets_api
        )
        self._async_builds = AsyncBuildExecutor(
            max_workers=async_max_workers,
            max_queue_size=async_max_queue_size,
            # running job safely terminates at boundary exactly once (#481). Terminal
            # event only recorded at terminal transition by the terminating side, so
            # queued/running cancellation both end with single run_cancelled, avoiding
            # duplicate terminal events per run.
            # Resolved at call time: the build service is assembled below, after this
            # registry, because it needs the registry.
            on_cancelled=lambda run_id: self._record_run_cancelled(run_id),
        )
        # Monitoring after async job registry created — it reads queue state.
        self._monitoring_api = MonitoringApiService(
            output_root=self._output_root,
            build_index=self._build_index,
            async_builds=self._async_builds,
            latency_recorder=self._latency_recorder,
        )
        # Spec authoring (#596). Same credential path as the build: `open_client`.
        self._spec_api = SpecApiService(
            api_version=API_CONTRACT_VERSION,
            open_client=lambda principal, owner, providers: self._open_build_client(
                principal, owner, providers
            ),
            catalog_client=lambda: self._create_client(),
            close_client=_close_request_client,
            upload_repository_for=lambda spec: self._upload_repository_for(spec),
        )
        # Build execution (#596, #637). Every accessor is a lambda so the lazy stores
        # stay lazy and a method a test replaces on this instance is the one called.
        self._build_runs = BuildRunsApiService(
            output_root=self._output_root,
            api_version=API_CONTRACT_VERSION,
            load_validated=lambda spec_yaml: self._load_validated(spec_yaml),
            open_client=lambda principal, owner, providers: self._open_build_client(
                principal, owner, providers
            ),
            close_client=_close_request_client,
            upload_repository_for=lambda spec: self._upload_repository_for(spec),
            event_store=lambda: self._event_store,
            table_catalog=lambda: self._table_catalog(),
            warehouse_configured=self._warehouse_root is not None,
            build_index=self._build_index,
            store=self._store,
            async_builds=self._async_builds,
        )
        # Publish domain (#637). Requires async job registry — terminal judgment
        # for blocking non-terminal run publish reads that registry.
        self._publish_api = PublishApiService(
            output_root=self._output_root,
            publish_receipts=self._publish_receipts,
            async_builds=self._async_builds,
            credential_repository=self._credential_resolver.repository,
        )

    @property
    def _event_store(self) -> BuildEventStore:
        """Lazily create and return run event timeline store (#496).

        Creates ``_build_events.sqlite`` only on first access — workspaces
        preview-only leave no footprint (lazy creation maintains existing
        "Preview writes no files" contract for same reason).
        """
        if self._event_store_lazy is None:
            with self._event_store_lock:
                if self._event_store_lazy is None:
                    self._event_store_lazy = BuildEventStore(self._output_root)
        return self._event_store_lazy

    @property
    def _upload_repository(self) -> UploadRepository:
        """Lazily create and return upload repository for kind="file" sources (#498).

        If explicitly injected, use as-is. Otherwise create SQLite file only on
        first call — workspaces not referencing uploads (preview/build) leave no
        ``.service/uploads.sqlite3`` footprint.
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

    def _table_catalog(self) -> TableCatalog | None:
        """The table catalog, or None when this deployment has no warehouse.

        Created lazily so a deployment that never materialises does not open a SQLite
        file it will not use, and returns None rather than a catalog under a default
        path — writing a catalog somewhere nobody asked for is worse than not writing
        one.
        """
        if self._warehouse_root is None:
            return None
        if self._catalog is None:
            self._catalog = TableCatalog(self._warehouse_root)
        return self._catalog

    def _upload_repository_for(self, spec: BuildSpec) -> UploadRepository | None:
        """Create upload repository only if spec has kind="file" source (#498).

        file source-free preview/build never touch this property, enabling true
        lazy creation — calling property itself (even discarding result) would
        eagerly initialize SQLite per request, defeating laziness. Check need first.
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
        """Create new provider client isolated with request principal's credentials."""
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
            # kpubdata#263 unfixed: credentials not in cache key.
            # Service clients with per-user credentials disable cache regardless
            # of configuration.
            kwargs["cache"] = False
        if timeout is not None and _factory_accepts_keyword(self._client_factory, "timeout"):
            kwargs["timeout"] = timeout
        client = self._client_factory(**kwargs)
        # While this client is open its keys are scrubbed from every log record by value,
        # which catches a key a provider puts in a path segment (#686).
        logging_redaction.register(client, provider_keys.values())
        return client

    def _runtime_providers(self) -> tuple[ProviderDescriptor, ...] | ServiceResponse:
        return self._providers_service.runtime_providers()

    def _known_provider(self, provider: str) -> ProviderDescriptor | ServiceResponse:
        return self._providers_service.known_provider(provider)

    def providers(self, *, principal: Principal) -> ServiceResponse:
        """Return runtime Provider list and current principal's configured status."""
        return self._providers_service.providers(principal=principal)

    def provider_status(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Perform lightweight connection test with current principal's credential."""
        return self._providers_service.provider_status(provider, principal=principal)

    def provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Return saved credential metadata for current principal without plaintext."""
        return self._providers_service.provider_credential(provider, principal=principal)

    def put_provider_credential(
        self,
        provider: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """Create or replace current principal's Provider credential."""
        return self._providers_service.put_provider_credential(provider, body, principal=principal)

    def delete_provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Delete only current principal's Provider credential."""
        return self._providers_service.delete_provider_credential(provider, principal=principal)

    def create_upload(
        self,
        raw: bytes,
        *,
        format: str,  # noqa: A002 - match contract field name
        encoding: str,
        original_filename: str | None,
        principal: Principal,
    ) -> ServiceResponse:
        """Save upload content and validate immediate parseability (#498)."""
        return self._uploads_service.create_upload(
            raw,
            format=format,
            encoding=encoding,
            original_filename=original_filename,
            principal=principal,
        )

    def get_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """Return safe metadata-only for current principal's upload (exclude content)."""
        return self._uploads_service.get_upload(upload_id, principal=principal)

    def delete_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """Delete only current principal's upload."""
        return self._uploads_service.delete_upload(upload_id, principal=principal)

    def query(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """Execute one validated SQL query against server-resolved stage table."""
        return self._query_api.query(body, principal=principal)

    def version(self) -> ServiceResponse:
        """Return the HTTP contract version and the application version (#209, #777).

        They count different things (kpubdata ADR 0004 §3): ``api_version`` is the wire
        contract a client checks before calling, ``version`` the installed application
        Studio compares with its own build to tell a mismatched pair. ``version`` is
        read from the installed distribution's metadata — the single source #592 set.
        """
        return ServiceResponse(
            200,
            {
                "service": "kpubdata-builder",
                "api_version": API_CONTRACT_VERSION,
                "version": __version__,
            },
        )

    # --- spec authoring (#596) -----------------------------------------------------
    #
    # Catalog, validation and preview live in SpecApiService; same-name delegates here.

    def catalog(self) -> ServiceResponse:
        """Return available provider/dataset catalog (#416, BL2, #436)."""
        return self._spec_api.catalog()

    def validate(self, spec_yaml: str) -> ServiceResponse:
        """Parse and validate BuildSpec."""
        return self._spec_api.validate(spec_yaml)

    def preview(
        self,
        spec_yaml: str,
        *,
        limit: int = DEFAULT_PREVIEW_LIMIT,
        sample_mode: str = "first",
        seed: int = DEFAULT_PREVIEW_SEED,
        principal: Principal | None = None,
    ) -> ServiceResponse:
        """Each source's schema, sample rows and Source↔Silver diff; writes nothing."""
        return self._spec_api.preview(
            spec_yaml, limit=limit, sample_mode=sample_mode, seed=seed, principal=principal
        )

    def _load_validated(self, spec_yaml: str) -> BuildSpec | ServiceResponse:
        """Parse and validate spec_yaml; return error ServiceResponse on failure."""
        return self._spec_api.load_validated(spec_yaml)

    # --- build execution (#596, #637) ---------------------------------------------
    #
    # Run, queue, poll and cancel live in BuildRunsApiService. These stay as thin
    # delegates so routing, dispatch and every caller see the same methods, and so a
    # subclass that overrides ``build`` or ``_run_build_job`` still intercepts the path.

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
        """Execute the pipeline and return the result (see ``BuildRunsApiService.build``)."""
        return self._build_runs.build(
            spec_yaml,
            run_id=run_id,
            created_by=created_by,
            owner_id=owner_id,
            manifest_owner_id=manifest_owner_id,
            credential_owner_id=credential_owner_id,
            principal=principal,
            cancellation=cancellation,
        )

    def submit_build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
    ) -> ServiceResponse:
        """Queue an async build job (#482).

        The runner is ``self._run_build_job``, looked up now, so a subclass override
        is what the worker calls.
        """
        return self._build_runs.submit_build(
            spec_yaml,
            runner=self._run_build_job,
            run_id=run_id,
            created_by=created_by,
            owner_id=owner_id,
        )

    def build_status(self, run_id: str) -> ServiceResponse:
        """Return active/terminal async build job status (#482)."""
        return self._build_runs.build_status(run_id)

    def cancel_build(self, run_id: str) -> ServiceResponse:
        """Request cancel of an active async build job (#481)."""
        return self._build_runs.cancel_build(run_id)

    def _record_run_cancelled(self, run_id: str) -> None:
        """Record the cancelled terminal event (#481); never raises."""
        self._build_runs.record_run_cancelled(run_id)

    def _open_build_client(
        self,
        principal: Principal | None,
        credential_owner_id: str | None,
        providers: tuple[str, ...],
    ) -> tuple[SourceClient, Mapping[str, str]]:
        """Resolve the requester's provider credentials and make a client with them.

        One callable for the build and preview paths, which never used the two apart
        (#637, #596). A
        request principal's owner wins over ``credential_owner_id``; with neither, no
        stored credential is looked up.
        """
        provider_owner_id = principal.owner_id if principal is not None else credential_owner_id
        provider_keys = (
            self._credential_resolver.provider_keys(provider_owner_id, providers)
            if principal is not None or credential_owner_id is not None
            else {}
        )
        client = self._create_client(
            principal, providers=providers, resolved_provider_keys=provider_keys
        )
        return client, provider_keys

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        """Actual execution entry point called by async job registry (#482, #496
        follow-up).

        Do not pass ``owner_id`` to build() for file resolver (stays ``None``) —
        kind="file" source resolver still lacks stable owner identity in async
        path (#498 async limitation maintained). SourceRef registry snapshot-
        preserved submitting principal owner_id used as ``credential_owner_id``
        for public_api credential resolution and ``manifest_owner_id`` for
        persisted manifest ownership (and BuildIndex reading it, #505 SSOT only) —
        persisted manifest (and BuildIndex reading it directly, #505 SSOT) gains
        accurate owner_id from single write inside build(). No post-build manifest
        amendments needed.
        """
        snapshot = self._async_builds.get(run_id)
        manifest_owner_id = snapshot.owner_id if snapshot is not None else None
        return self.build(
            spec_yaml,
            run_id=run_id,
            created_by=created_by,
            manifest_owner_id=manifest_owner_id,
            credential_owner_id=manifest_owner_id,
            # Pass cooperative cancel probe (#481) down to pipeline — don't carry
            # service concepts (registry/HTTP/Principal) across pipeline domain boundary.
            cancellation=cancellation,
        )

    # --- build artifacts query (#637) -------------------------------------------
    #
    # artifacts/manifest/spec/file serving/build list/event timeline handled by
    # BuildArtifactsApiService; execution by BuildRunsApiService, above.

    def artifacts(self, run_id: str) -> ServiceResponse:
        """Query run's artifact list."""
        return self._builds_api.artifacts(run_id)

    def manifest(self, run_id: str) -> ServiceResponse:
        """Query run's manifest."""
        return self._builds_api.manifest(run_id)

    def spec(self, run_id: str) -> ServiceResponse:
        """Query run's BuildSpec snapshot (#487)."""
        return self._builds_api.spec(run_id)

    def serve_artifact_file(self, run_id: str, file_path: str) -> ServiceResponse | FileResponse:
        """Serve one artifact file from run workspace."""
        return self._builds_api.serve_artifact_file(run_id, file_path)

    def list_builds(
        self, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """Query accessible run list (#433)."""
        return self._builds_api.list_builds(limit=limit, principal=principal)

    def get_build_events(self, run_id: str, *, limit: int, tail: bool) -> ServiceResponse:
        """Query run's append-only structured event timeline (#496)."""
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
        """Group multiple runs of same dataset_id into one built dataset; return
        list (#488)."""
        return self._datasets_api.list_datasets(limit=limit, principal=principal)

    def get_dataset(
        self, dataset_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """Query single built dataset's canonical summary (#488)."""
        return self._datasets_api.get_dataset(dataset_id, principal=principal)

    def list_dataset_runs(
        self, dataset_id: str, *, limit: int = 50, principal: Principal | None = None
    ) -> ServiceResponse:
        """Query dataset_id's accessible run history in reverse chronological order
        (#488)."""
        return self._datasets_api.list_dataset_runs(dataset_id, limit=limit, principal=principal)

    def get_dataset_run(
        self, dataset_id: str, run_id: str, *, principal: Principal | None = None
    ) -> ServiceResponse:
        """One run of dataset_id by id, with dataset and ownership checked here (studio#418)."""
        return self._datasets_api.get_dataset_run(dataset_id, run_id, principal=principal)

    def get_dataset_quality_history(
        self, dataset_id: str, *, limit: int = 30, principal: Principal | None = None
    ) -> ServiceResponse:
        """Query accessible runs' quality aggregate history for dataset_id (#486)."""
        return self._datasets_api.get_dataset_quality_history(
            dataset_id, limit=limit, principal=principal
        )

    def get_build_quality(self, run_id: str) -> ServiceResponse:
        """Query run's structured Quality results and schema drift (#486, #514)."""
        return self._quality_api.get_build_quality(run_id)

    def quality_summary(
        self, *, window: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """Aggregate structured quality of accessible runs within recent window
        (#486 follow-up)."""
        return self._quality_api.quality_summary(window=window, principal=principal)

    # --- stage query / observability (#637) ----------------------------------
    #
    # Per-stage artifacts handled by StagesApiService, /monitoring by
    # MonitoringApiService. Split mirrors #606 boundary — data questions
    # separate from system health questions.

    def list_run_stages(self, run_id: str) -> ServiceResponse:
        """Query per-stage artifact list for run (#488)."""
        return self._stages_api.list_run_stages(run_id)

    def get_run_stage_detail(
        self, run_id: str, stage: str, source_key: str, *, limit: int
    ) -> ServiceResponse:
        """Query specific stage detail for run (#488)."""
        return self._stages_api.get_run_stage_detail(run_id, stage, source_key, limit=limit)

    def monitoring_summary(self) -> ServiceResponse:
        """Query queue/build/latency summary (#516)."""
        return self._monitoring_api.monitoring_summary()

    def monitoring_builds(
        self, *, window: str, bucket: str, principal: Principal | None = None
    ) -> ServiceResponse:
        """Query build trend within window (#516)."""
        return self._monitoring_api.monitoring_builds(
            window=window, bucket=bucket, principal=principal
        )

    # --- publish domain (#637) -------------------------------------------
    #
    # readiness/publish/receipt/reconcile/audit handled by PublishApiService.
    # Only delegation thin wrapper remains — route adapter and dispatch surface
    # unchanged.

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
    """Call ``_dispatch_impl`` and record processing time as Monitoring latency
    sample (#516).

    Timing wraps entire routing + authentication + business logic (HTTP socket I/O
    excluded — that's http.py layer). LatencyRecorder.record already absorbs
    exceptions internally so metric recording failure doesn't propagate as request
    failure.

    ``raw_body`` is binary body used only in ``POST /uploads`` (#498) — all other
    endpoints use JSON ``body`` only and ``raw_body`` is None.
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
    """Route (method, path) to BuilderService operation.

    GET /healthz returns without authentication (#372); all other endpoints must
    pass authentication gate by calling authenticate() to obtain Principal, then
    route. Dev mode skips authentication; otherwise fail-closed (401) behavior
    (#248, #384).

    If ``client_id`` (TCP peer address passed by HTTP layer) exists, count
    authentication failures and cut off repeated attempts with 429. Check failure
    accumulation before authentication itself, so throttled clients never incur
    key comparison or signature verification cost.

    Returns:
        ServiceResponse or FileResponse (#323).
    """
    # /healthz exposed without authentication outside auth gate (#372).
    if method == "GET" and path == "/healthz":
        return ServiceResponse(200, {"status": "ok"})

    # Cut off clients with accumulated authentication failures before attempting
    # authentication.
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

    # Authentication gate (#384): return 401 if Principal cannot be obtained.
    principal = authenticate(api_key=api_key, bearer_token=bearer_token)
    if isinstance(principal, AuthError):
        # Count only 401 (invalid credentials) — 403 is valid token with authz
        # failure (not worth throttling), 503 is JWKS transient outage (not client
        # fault).
        if principal.status_code == 401:
            service._auth_throttle.record_failure(client_id)
        return ServiceResponse(principal.status_code, {"error": principal.reason})

    # Successful authentication clears failure record — normal client that received
    # a few 401s due to token expiry doesn't get throttled during subsequent normal
    # use.
    service._auth_throttle.record_success(client_id)

    # /uploads (#498) is the only endpoint needing binary body (raw_body), so it's
    # called directly here rather than added to standard RouteAdapter list (JSON
    # body only) — not restoring past monolithic dispatch, but this endpoint's
    # transport format differs from route adapter contract.
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

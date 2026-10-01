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
from collections.abc import Callable, Iterable, Mapping, Sequence
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
from . import pii_reads, publish_credentials, request_credentials
from . import publish as publish_service
from .analyses_api import AnalysesApiService, AnalysisStore
from .auth import AuthError, Principal, authenticate
from .auth_throttle import AuthFailureThrottle
from .build_runs_api import BuildRunsApiService
from .builds_api import BuildArtifactsApiService
from .datasets_api import DatasetsApiService
from .exports_api import ExportsApiService
from .jobs import AsyncBuildExecutor
from .monitoring_api import MonitoringApiService
from .pii_reads import PiiLookup, client_pii_lookup
from .profiles_api import ProfilesApiService
from .provider_tests import ProviderTestLog
from .providers import (
    CredentialResolver,
    ProviderDescriptor,
    ProviderTestOperation,
    default_provider_test,
    require_own_provider_credential,
)
from .providers_service import ProvidersService
from .publish_api import PublishApiService, VisibilityProbe
from .quality_api import ISSUE_STATUSES, QualityApiService
from .query_service_api import QueryApiService
from .redistribution import (
    BuildVerdict,
    TermsLookup,
    build_verdict,
    forbidden_response,
    kpubdata_terms,
)
from .responses import FileResponse, ServiceResponse
from .revisions import RevisionStore
from .revisions_api import RevisionsApiService
from .routes import ROUTE_ADAPTERS
from .routes import uploads as uploads_route

# Re-exported: the preview limit was part of this module before #596 moved preview out.
from .routes.core import MAX_PREVIEW_LIMIT as MAX_PREVIEW_LIMIT
from .spec_api import SpecApiService
from .stages_api import StagesApiService
from .uploads_service import UploadsService
from .user_ledger import UserLedger, admission_refusal
from .warehouse_api import WarehouseApiService

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
# 1.36.0 -> 1.37.0: POST /build may answer 409 with warehouse_failures when a table commit
#   failed after every source built (#787, #788, additive).
# 1.37.0 -> 1.38.0: GET /warehouse/tables, GET /warehouse/tables/{name} and
#   POST /warehouse/query read committed table snapshots, the last one pinning the
#   snapshot for the query's lifetime (#797, additive).
# 1.38.0 -> 1.39.0: /analyses saves a query bound to the snapshot id it read, holds that
#   snapshot, and re-runs against it (#783, additive).
# 1.39.0 -> 1.40.0: CatalogDataset gains quota, the spec licence's rate limit verbatim or
#   null (#778, additive).
# 1.40.0 -> 1.41.0: column metadata gains optional semantic, display and unit hints, each
#   with its origin (#813, additive; ADR 0019). Metadata only; absent when undescribed.
# 1.41.0 -> 1.42.0: manifest provenance gains fetched_row_count, source_reported_total and
#   coverage; WarehouseSnapshot gains coverage (#816, additive). Totals are never summed.
# 1.42.0 -> 1.43.0: POST /warehouse/rows reads one page of a pinned snapshot with a stable
#   order, filters, a column selection and a count with its status (#815, additive).
# 1.43.0 -> 1.44.0: POST /warehouse/aggregate runs named aggregates over a pinned snapshot,
#   top N after the whole aggregate, mixed units refused or split (#818, additive).
# 1.44.0 -> 1.45.0: /warehouse/exports writes a pinned query's full result as a bundle with
#   its manifest and terms, checked by licence and PII policy (#819, additive).
# 1.45.0 -> 1.46.0: GET /warehouse/tables/{name}/profile describes a snapshot's columns —
#   nulls, NaN/infinite counts, value ranges — withholding suspected PII columns and
#   small groups (#817, additive).
# 1.46.0 -> 1.47.0: WarehouseTable in GET /warehouse/tables gains current_snapshot and
#   dataset_id (#841, additive).
# 1.47.0 -> 1.48.0: BuildSummary gains dataset_id, dataset_title, snapshot_id and snapshots,
#   and GET /builds takes ?dataset_id= (#844, additive).
# 1.48.0 -> 1.49.0: GET /quality/issues lists warn/fail checks and schema drift across the
#   caller's tables with filters, a cursor and evaluation coverage (#843, additive).
# 1.49.0 -> 1.50.0: provider tests call a dataset chosen by declared parameters or answer
#   not_testable, and GET /providers reports each provider's last_test (#842).
# 1.50.0 -> 1.51.0: in a multi-user deployment another owner's run answers 404 on every run
#   route, like a missing run (#796, behaviour only).
# 1.51.0 -> 1.52.0: GET /admin/runs gives each run's failure reason (#679, additive).
# 1.52.0 -> 1.53.0: POST /build gains `materialized` when the deployment has a
#   warehouse — committed table snapshots per source (#703, additive). Absent, not
#   empty, when no warehouse is configured: an empty object would claim nothing was
#   committed, which a caller cannot tell from never having asked.
# (#679, additive — existing paths·behavior unchanged). Both return metadata only,
# 1.53.0 -> 1.54.0: the Builder sign-up ledger — /admin/users and approve/reject, and 403
#   signup_pending/signup_rejected for OIDC users not admitted (#785, additive).
# 1.54.0 -> 1.55.0: SourceRef gains gold (select/filters) and the manifest gold_selection
#   (#659, additive).
#   additive).
# 1.55.0 -> 1.56.0: provider keys by X-Provider-Key header for the request or job only, in a
#   multi-user deployment (#683, behaviour and a header).
#   additive).
# 1.56.0 -> 1.57.0: BuildSpec refresh_cadence and status_axes.health healthy/stale from it (#781,
# 1.57.0 -> 1.58.0: param_grid checkpoint resume and the manifest reproducibility mark (#648,
# 1.58.0 -> 1.59.0: /revisions — immutable document revisions with concurrency checks, revert
#   and an audit trail (#820, additive).
# 1.59.0 -> 1.60.0: versioned checksums — SourceProvenance.data_checksum_algorithm, the
#   manifest's inputs_fingerprint_algorithm and per-Gold artifacts {artifact_digest,
#   artifact_writer} (#867, additive; new runs' data_checksum values change algorithm).
# 1.60.0 -> 1.61.0: kpubdata code columns stored as text are reported as logical_type
#   identifier (wire_encoding always string) on query/preview/warehouse column metadata,
#   with the spec's semantic/display hints (#702, additive).
# 1.51.0 -> 1.52.0: preview/build/builds answer 403 url_source_forbidden for a url source in a
#   multi-user deployment, and declare the existing provider_credential_required (#685).
# 1.35.0 -> 1.36.0: DatasetSummary / DatasetDetailResponse gain status_axes — refresh,
#   completeness, health, access, maturity as separate fields (#781, additive).
# 1.34.0 -> 1.35.0: GET /version also reports the application version (#777, additive).
# 1.33.0 -> 1.34.0: the source_fetch_progress build event, one per finished param_grid
#   combination with metrics {done, total} (#648, additive).
# 1.61.0 -> 1.62.0: declared PII columns masked in Gold by default; SourceRef.gold gains
#   pii_columns and publish_unmasked, the manifest pii_masking (#689, additive).
# 1.62.0 -> 1.63.0: profiles get their own timeout and remember it per snapshot; nan_count/
#   infinite_count minimum 0; categorical/enum/list PII value checks (#896, #897).
# 1.63.0 -> 1.64.0: the pii scan gate counts declared columns Gold masks as handled;
#   manifest pii_masking gains declared_absent and masked[].masked_as, and a non-text
#   masked column keeps its dtype as null (#902, additive).
# 1.64.0 -> 1.65.0: redistribution terms (#688) — publish readiness reports and enforces
#   each source's verdict (option confirm_non_commercial), and data whose terms are
#   forbidden answers 403 redistribution_forbidden on every way out (additive).
# 1.65.0 -> 1.67.0 (1.66.0 is held by the open #923): publish tokens by the
#   X-Publish-Credential header for the request only in a multi-user deployment; no
#   stored publish credential and no server HF_TOKEN/KAGGLE_* fallback there (#925,
#   behaviour and a header).
# 1.67.0 -> 1.68.0: declared PII stays masked on Silver and Bronze reads (#900) — /query on
#   Silver, /preview and the Silver stage sample mask it (masked_columns), and a Bronze or
#   Silver artifact file of a source with declared PII answers 403 declared_pii_withheld;
#   an unreadable declaration fails closed with 503 pii_declaration_unavailable and
#   sample_withheld: pii_declaration_unavailable (additive).
# 1.68.0 -> 1.69.0: GET /version reports publish_credential — request, stored or
#   stored_or_server — so a client knows where this deployment takes publish
#   credentials from (#938, additive).
# 1.69.0 -> 1.70.0: the manifest's split_algorithm — ratio splits are hash-sort-v2 (#871,
#   additive; membership differs from shuffle-v1 for the same seed).
# 1.70.0 -> 1.71.0: dataset cards (#694) — card.json
#   beside README.md, and publish readiness blockers card_missing / card_incomplete
#   (additive).
# 1.71.0 -> 1.72.0: declared error bodies — RevisionConflictError (409 revision_conflict
#   with current_revision, #947) and SignupNotApprovedError / the SignupNotApproved
#   response (403 signup_pending / signup_rejected, #951) (additive).
# 1.72.0 -> 1.73.0: GET /admin/runs gains total, the runs before limit; count is the runs in
#   the response (#948, additive).
# 1.73.0 -> 1.74.0: queries, rows, aggregates, profiles and exports run on a locked DuckDB
#   connection (#874); result types follow DuckDB in Builder's spelling (COUNT int64,
#   SUM of integers int128, unnamed aggregates named by DuckDB, DESC nulls last).
# 1.74.0 -> 1.75.0: card.json is the declared DatasetCard, with license_declared,
#   license_provider, license_mismatch and processing_declared (#955, additive).
API_CONTRACT_VERSION = "1.75.0"


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
        terms_lookup: TermsLookup | None = None,
        publish_visibility_probe: VisibilityProbe | None = None,
        pii_lookup: PiiLookup | None = None,
    ) -> None:
        # Provider keys ride in request URLs, and the HTTP library logs those URLs (#686).
        logging_redaction.install()
        self._output_root = output_root
        # Each dataset's redistribution terms (#688): the kpubdata catalog, or a stand-in.
        self._terms_lookup: TermsLookup = terms_lookup or kpubdata_terms
        # Each dataset's declared PII columns (#900), read through a client from the
        # build's own factory so a read and Gold masking see the same declaration.
        self._pii_lookup: PiiLookup = pii_lookup or client_pii_lookup(
            lambda: self._create_client(), _close_request_client
        )
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
            test_log=lambda: self._provider_tests(),
        )
        # Upload repository initialized only when needed (#498) — pass lambda,
        # not property value directly (#498) — calling property on every request
        # (even unused result) would eagerly initialize SQLite per request,
        # defeating lazy creation. Check need first here.
        self._uploads_service = UploadsService(repository=lambda: self._upload_repository)
        self._query_api = QueryApiService(
            output_root=self._output_root,
            engine=self._query_service,
            terms_lookup=self._terms_lookup,
            pii_lookup=self._pii_lookup,
        )
        self._warehouse_api = WarehouseApiService(
            table_catalog=lambda: self._table_catalog(),
            engine=self._query_service,
            output_root=self._output_root,
            terms_lookup=self._terms_lookup,
        )
        self._exports_api = ExportsApiService(
            output_root=self._output_root,
            table_catalog=lambda: self._table_catalog(),
            engine=self._query_service,
            terms_lookup=self._terms_lookup,
        )
        self._profiles_api = ProfilesApiService(
            output_root=self._output_root,
            table_catalog=lambda: self._table_catalog(),
            engine=self._query_service,
            terms_lookup=self._terms_lookup,
        )
        self._analysis_store: AnalysisStore | None = None
        self._user_ledger_store: UserLedger | None = None
        self._revision_store: RevisionStore | None = None
        self._revisions_api = RevisionsApiService(store=lambda: self._revisions())
        # Provider keys of submitted async jobs, in memory only (#683).
        self._job_credentials = request_credentials.JobCredentials()
        self._provider_test_log: ProviderTestLog | None = None
        self._analyses_api = AnalysesApiService(
            store=lambda: self._analyses(),
            warehouse=self._warehouse_api,
            table_catalog=lambda: self._table_catalog(),
        )
        self._datasets_api = DatasetsApiService(
            output_root=self._output_root,
            build_index=self._build_index,
            store=self._store,
            # Resolved at call time: the job registry is created further down (#781).
            active_runs=lambda: self._async_builds.registry.active_snapshots(),
        )
        self._stages_api = StagesApiService(output_root=self._output_root, store=self._store)
        self._builds_api = BuildArtifactsApiService(
            output_root=self._output_root,
            store=self._store,
            build_index=self._build_index,
            # Lazy creation maintains constraint — pass accessor not value (#496).
            event_store=lambda: self._event_store,
            table_catalog=lambda: self._table_catalog(),
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
            terms_lookup=self._terms_lookup,
            visibility_probe=publish_visibility_probe,
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

    def _provider_tests(self) -> ProviderTestLog:
        """Last provider connection test per principal (#842), opened on first use."""
        if self._provider_test_log is None:
            self._provider_test_log = ProviderTestLog(
                self._output_root / ".service" / "provider_tests.sqlite3"
            )
        return self._provider_test_log

    def _revisions(self) -> RevisionStore:
        """Document revisions (#820), opened on first use."""
        if self._revision_store is None:
            self._revision_store = RevisionStore(
                self._output_root / ".service" / "revisions.sqlite3"
            )
        return self._revision_store

    def _user_ledger(self) -> UserLedger:
        """The Builder sign-up ledger (#785), opened on first OIDC sign-in."""
        if self._user_ledger_store is None:
            self._user_ledger_store = UserLedger(self._output_root / ".service" / "users.sqlite3")
        return self._user_ledger_store

    def _analyses(self) -> AnalysisStore:
        """Saved analysis store, opened on first use (#783)."""
        if self._analysis_store is None:
            self._analysis_store = AnalysisStore(
                self._output_root / ".service" / "analyses.sqlite3"
            )
        return self._analysis_store

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
        # A response cache shared between users must never stand in for authorization
        # (#684). kpubdata >=0.7 fingerprints credentials into its cache key and does not
        # cache GETs carrying sensitive headers, but the service does not rely on that:
        # this is defence in depth, and it also stops a disk cache written in one
        # deployment mode being read after a switch to another. Per-user credentials and
        # any multi-user deployment therefore get no cache, whatever KPUBDATA_CACHE says —
        # and a factory that cannot turn it off is refused rather than trusted.
        if provider_keys or ownership_module.multi_user_mode():
            if not _factory_accepts_keyword(self._client_factory, "cache"):
                raise RuntimeError("client_factory cannot disable the shared response cache")
            kwargs["cache"] = False
        if timeout is not None and _factory_accepts_keyword(self._client_factory, "timeout"):
            kwargs["timeout"] = timeout
        # With REQUIRE_OWN_PROVIDER_CREDENTIAL on, no client may carry the operator's keys
        # — not even a keyless one, which would read them from the environment itself
        # (#786). A factory that cannot be told so is refused, not trusted.
        if require_own_provider_credential():
            if not _factory_accepts_keyword(self._client_factory, "environment_keys"):
                raise RuntimeError("client_factory cannot be kept from the operator's credentials")
            kwargs["environment_keys"] = False
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

    def _run_verdict(self, run_id: str) -> BuildVerdict:
        """The redistribution verdict of a run's sources (#688)."""
        spec = datasets_service.read_snapshot_spec(self._output_root, run_id)
        return build_verdict(spec, self._terms_lookup)

    def list_warehouse_tables(self, *, principal: Principal) -> ServiceResponse:
        """List the caller's committed warehouse tables (#797)."""
        return self._warehouse_api.list_tables(principal=principal)

    def get_warehouse_profile(
        self, name: str, snapshot: str, *, principal: Principal
    ) -> ServiceResponse:
        """Column profile of one snapshot of a warehouse table (#817)."""
        return self._profiles_api.get(name, snapshot, principal=principal)

    def get_warehouse_table(self, name: str, *, principal: Principal) -> ServiceResponse:
        """One warehouse table and its readable snapshots (#797)."""
        return self._warehouse_api.get_table(name, principal=principal)

    def query_warehouse(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """Run read-only SQL against a pinned warehouse snapshot (#797)."""
        return self._warehouse_api.query(body, principal=principal)

    def read_warehouse_rows(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """One page of a pinned warehouse snapshot, in a stable order (#815)."""
        return self._warehouse_api.rows(body, principal=principal)

    def aggregate_warehouse(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """A validated aggregate over a pinned warehouse snapshot (#818)."""
        return self._warehouse_api.aggregate(body, principal=principal)

    def create_warehouse_export(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """Export a pinned query's full result through the policy checks (#819)."""
        return self._exports_api.create(body, principal=principal)

    def list_warehouse_exports(self, *, principal: Principal) -> ServiceResponse:
        """The caller's unexpired exports, newest first (#819)."""
        return self._exports_api.list(principal=principal)

    def get_warehouse_export(self, export_id: str, *, principal: Principal) -> ServiceResponse:
        """One export's status and manifest (#819)."""
        return self._exports_api.get(export_id, principal=principal)

    def download_warehouse_export(
        self, export_id: str, *, principal: Principal
    ) -> ServiceResponse | FileResponse:
        """An export's bundle, with ownership and expiry checked at download (#819)."""
        return self._exports_api.download(export_id, principal=principal)

    def delete_warehouse_export(self, export_id: str, *, principal: Principal) -> ServiceResponse:
        """Delete an export and its file (#819)."""
        return self._exports_api.delete(export_id, principal=principal)

    def create_analysis(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """Run a query once and save it bound to the snapshot it read (#783)."""
        return self._analyses_api.create(body, principal=principal)

    def list_analyses(self, *, principal: Principal) -> ServiceResponse:
        """The caller's saved analyses, newest first (#783)."""
        return self._analyses_api.list(principal=principal)

    def get_analysis(self, analysis_id: str, *, principal: Principal) -> ServiceResponse:
        """One saved analysis (#783)."""
        return self._analyses_api.get(analysis_id, principal=principal)

    def delete_analysis(self, analysis_id: str, *, principal: Principal) -> ServiceResponse:
        """Delete a saved analysis and release its snapshot hold (#783)."""
        return self._analyses_api.delete(analysis_id, principal=principal)

    def run_analysis(self, analysis_id: str, *, principal: Principal) -> ServiceResponse:
        """Re-run a saved analysis against the snapshot it was saved with (#783)."""
        return self._analyses_api.run(analysis_id, principal=principal)

    def version(self) -> ServiceResponse:
        """Return the HTTP contract version and the application version (#209, #777).

        They count different things (kpubdata ADR 0004 §3): ``api_version`` is the wire
        contract a client checks before calling, ``version`` the installed application
        Studio compares with its own build to tell a mismatched pair. ``version`` is
        read from the installed distribution's metadata — the single source #592 set.

        ``publish_credential`` says where a publish credential comes from (#938), so a
        client can ask for a token before readiness reports ``credential_required``.
        It is the deployment's policy, not whether any credential exists.
        """
        return ServiceResponse(
            200,
            {
                "service": "kpubdata-builder",
                "api_version": API_CONTRACT_VERSION,
                "version": __version__,
                "publish_credential": publish_credentials.publish_credential_source(),
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
        loaded = self._spec_api.load_validated(spec_yaml)
        if isinstance(loaded, BuildSpec):
            # Before anything is fetched (#688): the spec is the caller's own.
            refusal = forbidden_response(
                build_verdict(loaded, self._terms_lookup), what="a preview"
            )
            if refusal is not None:
                return refusal
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
            job_credentials=(self._job_credentials if ownership_module.multi_user_mode() else None),
        )

    def build_status(self, run_id: str) -> ServiceResponse:
        """Return active/terminal async build job status (#482)."""
        return self._build_runs.build_status(run_id)

    def cancel_build(self, run_id: str) -> ServiceResponse:
        """Request cancel of an active async build job (#481).

        A queued job's keys are dropped now (#683): if it never starts, nothing else
        would. A running job has already taken them.
        """
        response = self._build_runs.cancel_build(run_id)
        self._job_credentials.discard(run_id)
        return response

    def mark_interrupted_runs(self) -> tuple[str, ...]:
        """At startup of a multi-user deployment, fail runs a restart interrupted (#683).

        A single-user deployment keeps its stored and environment keys, so nothing is
        lost with a restart and nothing is marked.
        """
        if not ownership_module.multi_user_mode():
            return ()
        return self._build_runs.mark_interrupted_runs()

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
        # Multi-user mode (#683): the keys bound at submission, taken once and dropped
        # whichever way the job ends — success, failure or cancellation.
        keys = self._job_credentials.take(run_id, manifest_owner_id)
        try:
            with request_credentials.request_scope(keys):
                return self.build(
                    spec_yaml,
                    run_id=run_id,
                    created_by=created_by,
                    manifest_owner_id=manifest_owner_id,
                    credential_owner_id=manifest_owner_id,
                    # Pass cooperative cancel probe (#481) down to pipeline — don't carry
                    # service concepts (registry/HTTP/Principal) across pipeline domain
                    # boundary.
                    cancellation=cancellation,
                )
        finally:
            keys = None
            self._job_credentials.discard(run_id)

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
        response = self._builds_api.serve_artifact_file(run_id, file_path)
        if isinstance(response, FileResponse):
            refusal = forbidden_response(self._run_verdict(run_id), what="an artifact")
            if refusal is not None:
                return refusal
            # A Bronze or Silver file holds declared PII as is; it does not leave (#900).
            pii_refusal = pii_reads.artifact_refusal(
                self._output_root, run_id, file_path, self._pii_lookup
            )
            if pii_refusal is not None:
                return pii_refusal
        return response

    def list_builds(
        self,
        *,
        limit: int = 50,
        principal: Principal | None = None,
        dataset_id: str | None = None,
    ) -> ServiceResponse:
        """Query accessible run list (#433), optionally one dataset's (#844)."""
        return self._builds_api.list_builds(limit=limit, principal=principal, dataset_id=dataset_id)

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

    def list_quality_issues(
        self,
        *,
        principal: Principal | None = None,
        statuses: frozenset[str] | None = None,
        dataset_id: str | None = None,
        category: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> ServiceResponse:
        """Warn/fail checks and schema drift across the caller's tables (#843)."""
        return self._quality_api.list_issues(
            principal=principal,
            statuses=statuses if statuses is not None else frozenset(ISSUE_STATUSES),
            dataset_id=dataset_id,
            category=category,
            limit=limit,
            cursor=cursor,
        )

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
        response = self._stages_api.get_run_stage_detail(run_id, stage, source_key, limit=limit)
        if (
            response.status_code == 200
            and isinstance(response.body, dict)
            and response.body.get("sample")
            and self._run_verdict(run_id).verdict == "forbidden"
        ):
            # The stage's metadata stays; its sample rows do not leave (#688).
            body = dict(response.body)
            body["sample"] = []
            body["sample_withheld"] = "redistribution_forbidden"
            return ServiceResponse(response.status_code, body)
        if response.status_code == 200 and stage == "silver" and response.body.get("sample"):
            # Declared PII in the sample is masked as Gold masks it (#900).
            return ServiceResponse(
                200,
                pii_reads.mask_stage_sample(
                    self._output_root, run_id, source_key, response.body, self._pii_lookup
                ),
            )
        return response

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
    provider_key_headers: Sequence[str] = (),
    publish_credential_headers: Sequence[str] = (),
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
        # Provider keys for this request only (#683): parsed from the X-Provider-Key
        # header — never the URL — and forgotten when the request ends.
        try:
            request_keys = request_credentials.parse_provider_key_headers(provider_key_headers)
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_provider_key"})
        # Publish credentials for this request only (#925): the X-Publish-Credential
        # header, read by publish in a multi-user deployment and forgotten when the
        # request ends. The error message never carries a value.
        try:
            request_publish_values = publish_credentials.parse_publish_credential_headers(
                publish_credential_headers
            )
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_publish_credential"})
        with (
            request_credentials.request_scope(request_keys),
            publish_credentials.request_scope(request_publish_values),
        ):
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

    # Sign-up ledger (#785): an OIDC user not admitted by a list waits for an
    # administrator; a rejected one is shut out even when a list admits them.
    if principal.kind == "oidc" and principal.owner_id is not None:
        entry = service._user_ledger().observe(principal)
        refusal = admission_refusal(entry, principal)
        if refusal is not None:
            return ServiceResponse(403, refusal)

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

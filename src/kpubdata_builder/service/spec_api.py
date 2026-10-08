"""Spec authoring service — catalog, validate, preview (#596).

What a user touches before a build runs: which datasets exist, whether a BuildSpec is
valid, and what its sources would produce. None of it writes a file (#497).

The preview path needs a provider client carrying the requester's credentials — the
same ``open_client`` the build execution service takes (#637), so the two paths
resolve credentials one way. ``catalog`` needs a client with none, which is why it
takes a separate ``catalog_client``.

**Wire contract unchanged.** ``BuilderService`` delegates with the same signatures.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import cast
from urllib.parse import urlsplit

import yaml
from kpubdata.core.models import DatasetRef

from ..errors import SpecLoadError, ValidationError
from ..pipeline import DEFAULT_PREVIEW_SEED, SampleMode, preview_build
from ..pipeline.preview import SourcePreview
from ..quality import QualityCheckResult
from ..spec import BuildSpec, JsonValue, parse_spec
from ..spec.validator import validate_spec
from ..stages.bronze.build import SourceClient
from ..stages.gold.pii import core_pii_columns
from ..tabular import DEFAULT_PREVIEW_LIMIT
from ..tabular.types import SchemaInfo
from ..tabular.wire import encode_rows, encode_value
from ..uploads import UploadRepository
from . import vocabulary
from .auth import Principal
from .build_runs_api import OpenClient
from .column_semantics import describe_columns, spec_semantics
from .pii_reads import PiiDeclarationUnavailable, mask_source_preview, unavailable_response
from .providers import (
    ProviderCredentialConflictError,
    ProviderCredentialRequired,
    runtime_provider_catalog,
)
from .redaction import redact_secret_text
from .responses import ServiceResponse
from .routes.core import MAX_PREVIEW_LIMIT
from .source_policy import url_source_refusal
from .stages import match_source_ref

logger = logging.getLogger(__name__)


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
    """Serialize request parameter descriptions from ``raw_metadata`` as a secret-free
    allowlist.

    Minimal public metadata for UI to guide required request parameters in advance
    (``dataset.raw_metadata["request_parameters"]``, or empty array if absent).

    - Only ``dict`` items with non-empty string ``name`` required.
    - Exclude ``service_key_param`` and secret-like names (serviceKey/apiKey/key/
      secret/token/password, etc.) — serviceKey/API key input is not required as
      user request params.
    - Only include ``name``/``required``(bool)/``description``(str|None)/
      ``example``(str|None). Do not expose provider internal implementation details.
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
    """Serialize ``raw_metadata.application`` as a secret-free allowlist.

    Public Data Portal may separate API Key issuance from per-Dataset application
    approval — if ``dataset.raw_metadata["application"]`` (``{"required": bool,
    "url": str}``) exists, pass it through; otherwise ``null`` (approval status
    unknown, not assumed unnecessary). Do not expose if ``url`` lacks http(s)
    scheme (block arbitrary schemes).
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


def _catalog_quota(dataset: DatasetRef) -> str | None:
    """The spec licence's quota as the provider words it, or None (#778).

    Read with ``getattr`` because ``DatasetRef.license`` arrives in a kpubdata release
    after the one this package pins (kpubdata#609); until then every quota is None,
    which the contract defines as "unknown", never "unlimited". The text is passed
    through unparsed: providers phrase it differently, and a guessed number would be
    worse than the sentence.
    """
    terms = getattr(dataset, "license", None)
    quota = getattr(terms, "quota", None)
    if not isinstance(quota, str) or not quota.strip():
        return None
    return quota


def _catalog_dataset_body(dataset: DatasetRef, requires_service_key: bool) -> dict[str, JsonValue]:
    """Serialize only public/canonical metadata from DatasetRef as an allowlist (#490).

    ``raw_metadata`` may contain provider internals and secret-like values, so
    never expose directly — only explicitly select fields needed for UI discovery.
    Datasets without metadata serialize description/source_url/query_support as
    null, tags/operations as empty arrays (preserve response integrity).
    """
    query_support: JsonValue = None
    # Enum values go through Builder's own vocabulary (#831): a value kpubdata adds
    # later becomes a declared fallback, never an off-contract string.
    pagination = (
        vocabulary.pagination_mode(dataset.query_support.pagination)
        if dataset.query_support is not None
        else None
    )
    if dataset.query_support is not None and pagination is not None:
        query_support = {
            "pagination": pagination,
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
        "representation": vocabulary.representation(dataset.representation),
        "operations": cast(JsonValue, vocabulary.operations(dataset.operations)),
        "query_support": query_support,
        "requires_service_key": requires_service_key,
        "request_parameters": cast(JsonValue, _catalog_request_parameters(dataset)),
        "application": _catalog_application(dataset),
        "quota": _catalog_quota(dataset),
    }


def _encodings(schema: SchemaInfo) -> dict[str, str]:
    """Each column's wire encoding, for values sent outside the sample rows (#735)."""
    return {column.name: column.wire_encoding for column in schema.columns}


def _quality_result_to_json(r: QualityCheckResult) -> dict[str, JsonValue]:
    """Convert QualityCheckResult to wire JSON (#486)."""
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


def parse_spec_text(spec_yaml: str) -> BuildSpec:
    """Parse YAML text to BuildSpec.

    Malformed YAML yields ``yaml.YAMLError``, a user input problem not a server
    defect. Converted to SpecLoadError so callers treat other parse failures as
    400 — otherwise sync path returns 500 and async worker never terminates.
    """
    try:
        raw = cast(object, yaml.safe_load(spec_yaml))
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"spec is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecLoadError("top-level YAML must be a mapping")
    return parse_spec(cast(dict[str, object], raw))


class SpecApiService:
    """Catalog discovery, BuildSpec validation and preview."""

    def __init__(
        self,
        *,
        api_version: str,
        open_client: OpenClient,
        catalog_client: Callable[[], SourceClient],
        close_client: Callable[[SourceClient], None],
        upload_repository_for: Callable[[BuildSpec], UploadRepository | None],
    ) -> None:
        self._api_version = api_version
        self._open_client = open_client
        self._catalog_client = catalog_client
        self._close_client = close_client
        self._upload_repository_for = upload_repository_for

    def catalog(self) -> ServiceResponse:
        """Return available provider/dataset catalog (#416, BL2, #436).

        Uses kpubdata Client's provider-specific public ``datasets.list(provider=...)``.
        Isolates KRX explicit optional pandas-less case; other errors fail.
        Provider list from runtime registry (ADR 0011 — Builder not hardcoding).
        Previously used ``getattr(client, "_catalog")`` private + 8-provider
        tuple, so new providers weren't discovered (#436). Secrets unexposed;
        only requirement status shown.
        """
        client = self._catalog_client()
        try:
            runtime_catalog = runtime_provider_catalog(client)
        except Exception:
            # Upstream exception strings may carry request URLs, and URLs carry
            # API keys as query parameters (same reason as providers_service).
            logger.exception("provider catalog unavailable")
            return ServiceResponse(
                502, {"error": "catalog unavailable", "code": "catalog_unavailable"}
            )
        finally:
            self._close_client(client)

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
        """Parse and validate BuildSpec."""
        try:
            spec = parse_spec_text(spec_yaml)
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
                "api_version": self._api_version,
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
        """Produce each source's schema, sample rows, Source↔Silver diff (no file
        write, #497)."""
        if limit < 1 or limit > MAX_PREVIEW_LIMIT:
            return ServiceResponse(
                400, {"error": f"'limit' must be a positive integer up to {MAX_PREVIEW_LIMIT}"}
            )
        if sample_mode not in ("first", "random"):
            return ServiceResponse(400, {"error": "'sample_mode' must be 'first' or 'random'"})
        if not isinstance(seed, int) or isinstance(seed, bool):
            return ServiceResponse(400, {"error": "'seed' must be an integer"})
        spec_or_error = self.load_validated(spec_yaml)
        if isinstance(spec_or_error, ServiceResponse):
            return spec_or_error
        refusal = url_source_refusal(spec_or_error)
        if refusal is not None:
            return refusal

        # Provider credential meaningful only for kind="public_api" sources (#498) —
        # file/url sources' provider always empty string; mixing confuses credential
        # resolver with meaningless provider names.
        provider_names = tuple(
            source.provider for source in spec_or_error.sources if source.kind == "public_api"
        )
        try:
            # No credential owner besides the request principal: a preview is never a
            # queued job acting for someone who has left.
            client, provider_keys = self._open_client(principal, None, provider_names)
        except ProviderCredentialRequired as exc:
            # The requester has no key of their own and the operator's may not be used
            # (#786): an answer, before any client exists.
            return ServiceResponse(
                403,
                {
                    "error": str(exc),
                    "code": "provider_credential_required",
                    "providers": list(exc.providers),
                },
            )
        except (ProviderCredentialConflictError, ValueError) as exc:
            return ServiceResponse(400, {"error": str(exc)})
        except Exception:
            return ServiceResponse(
                502, {"error": "provider client unavailable", "code": "provider_client_unavailable"}
            )
        try:
            result = preview_build(
                spec_or_error,
                client=client,
                limit=limit,
                sample_mode=cast(SampleMode, sample_mode),
                seed=seed,
                upload_repository=self._upload_repository_for(spec_or_error),
                owner_id=principal.owner_id if principal is not None else None,
                # A provider that echoes the request would put the key into the sample.
                secret_values=tuple(provider_keys.values()),
                # By provider, to say which provider's key a refusal was about (#1187).
                provider_keys=dict(provider_keys),
            )
            # Declared PII leaves masked as Gold masks it (#900), read through the same
            # client the preview fetched with, as the build reads it.
            masked = _mask_previews(result.previews, spec_or_error, client)
        except PiiDeclarationUnavailable as exc:
            # Fail closed (#900): which columns are personal is not known.
            return unavailable_response(exc, what="a preview")
        finally:
            self._close_client(client)
        previews: list[JsonValue] = [
            {
                "source_key": p.source_key,
                "status": p.status,
                "error": redact_secret_text(p.error, provider_keys.values()),
                # A text column the source's kpubdata spec declares a code is reported
                # as an identifier (#702); its sample values are the strings it holds.
                "schema": cast(
                    JsonValue,
                    describe_columns(
                        [
                            {
                                "name": column.name,
                                "dtype": column.dtype,
                                "nullable": column.nullable,
                                "unique_count": column.unique_count,
                                "logical_type": column.logical_type,
                                "wire_encoding": column.wire_encoding,
                            }
                            for column in p.schema.columns
                        ],
                        spec_semantics(spec_or_error, p.source_key),
                    ),
                ),
                # Wire-encoded by column (#735). The diff below was computed on the
                # unencoded values, so encoding here changes what is sent, not what changed.
                "sample": list(encode_rows(p.preview.rows, p.schema.columns)),
                "total_rows": p.preview.total_rows,
                "statistics": {
                    "row_count": p.statistics.row_count,
                    "null_counts": dict(p.statistics.null_counts),
                    "duplicate_rate": p.statistics.duplicate_rate,
                },
                "quality_results": cast(
                    JsonValue, [_quality_result_to_json(r) for r in p.quality_results]
                ),
                # The raw bronze rows go through the same encoder, so a value reads the
                # same in `source_sample`, `sample` and the diff below (#735).
                "source_sample": list(encode_rows(p.source_sample, p.schema.columns)),
                "sample_mode": p.sample_mode,
                "diff_available": p.diff_available,
                "diffs": cast(
                    JsonValue,
                    [
                        {
                            "row": d.row,
                            "column": d.column,
                            "before": encode_value(
                                d.before, _encodings(p.schema).get(d.column, "json")
                            ),
                            "after": encode_value(
                                d.after, _encodings(p.schema).get(d.column, "json")
                            ),
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
                "fetch_complete": p.fetch_complete,
                "source_reported_total": p.source_reported_total,
                "reason": p.reason,
            }
            for p, _ in masked
        ]
        for entry, (_, columns) in zip(previews, masked, strict=True):
            if columns and isinstance(entry, dict):
                entry["masked_columns"] = list(columns)
        return ServiceResponse(200, {"dataset_id": spec_or_error.dataset_id, "previews": previews})

    def load_validated(self, spec_yaml: str) -> BuildSpec | ServiceResponse:
        """Parse and validate spec_yaml; return error ServiceResponse on failure."""
        try:
            spec = parse_spec_text(spec_yaml)
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


def _mask_previews(
    previews: Sequence[SourcePreview], spec: BuildSpec, client: SourceClient
) -> list[tuple[SourcePreview, tuple[str, ...]]]:
    """Each preview with its declared PII masked, and the Silver columns that were (#900)."""
    out: list[tuple[SourcePreview, tuple[str, ...]]] = []
    for preview in previews:
        if preview.status != "ok":
            out.append((preview, ()))
            continue
        source = match_source_ref(spec, preview.source_key)
        if source is None:
            raise PiiDeclarationUnavailable(preview.source_key)
        core: tuple[str, ...] = ()
        if source.kind == "public_api":
            dataset_id = f"{source.provider}.{source.dataset}"
            try:
                core = core_pii_columns(client.dataset(dataset_id))
            except Exception as exc:
                raise PiiDeclarationUnavailable(dataset_id) from exc
        out.append(mask_source_preview(preview, source, core))
    return out


__all__ = ["SpecApiService", "parse_spec_text"]

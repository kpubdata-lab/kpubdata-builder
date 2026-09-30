"""source kind resolver—creates identical BronzeArtifacts from public_api/file/url (#498)."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from ...ingestion import IngestionError, parse_tabular_bytes, safe_fetch_get
from ...ingestion.url_fetch import default_max_fetch_bytes
from ...spec import JsonValue, SourceRef, expand_param_grid
from ...spec.models import SOURCE_KINDS
from ...uploads import UploadRepository
from .build import SourceClient, build_bronze_artifact
from .checkpoint import CombinationCheckpoint
from .models import BronzeArtifact, ProvenanceEvent, require_timezone_aware, utc_now

_NON_SLUG_CHARS = re.compile(r"[^a-zA-Z0-9]+")


def source_identity(source: SourceRef) -> tuple[str, str]:
    """canonical identity that fills (provider, dataset) slots for all kinds."""
    if source.kind == "file":
        return "file", source.upload_id
    if source.kind == "url":
        return "url", _url_path_safe_identity(source.endpoint)
    return source.provider, source.dataset


def _url_path_safe_identity(endpoint: str) -> str:
    """endpoint to `validate_path_segment` always-passing slug."""
    sanitized = sanitize_endpoint_identity(endpoint)
    host = urlsplit(sanitized).hostname or "host"
    slug = _NON_SLUG_CHARS.sub("-", host).strip("-") or "host"
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}"


def sanitize_endpoint_identity(endpoint: str) -> str:
    """creates human-readable identity from endpoint with query/userinfo/fragment removed."""
    parts = urlsplit(endpoint)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path or "/", "", ""))


def build_bronze_artifact_for_source(
    source: SourceRef,
    *,
    client: SourceClient,
    upload_repository: UploadRepository | None = None,
    owner_id: str | None = None,
    fetched_at: datetime | None = None,
    secret_values: tuple[str, ...] = (),
    on_combination_done: Callable[[int, int], None] | None = None,
    checkpoint_path: Path | None = None,
) -> BronzeArtifact:
    """fetches per source.kind and creates BronzeArtifact (#498).

    ``checkpoint_path`` (#648): where a public_api ``param_grid`` fetch appends each
    finished combination, scrubbed of ``secret_values``, and resumes from on a rebuild.

    ``on_combination_done`` reaches the ``param_grid`` loop of a public_api source
    (#648); other kinds make one read and never call it.

    ``secret_values`` are the requester's provider keys. A provider that echoes the
    request back in its response puts the key into the records, and from there into
    every stage output, card and export (#686). Bronze is where records enter, so it
    is where exact occurrences of a key are replaced — before anything is written.
    """
    artifact = _fetch_bronze(
        source,
        client=client,
        upload_repository=upload_repository,
        owner_id=owner_id,
        fetched_at=fetched_at,
        on_combination_done=on_combination_done,
        checkpoint=(
            CombinationCheckpoint(
                checkpoint_path,
                scrub=lambda value: scrub_secret_values(value, secret_values),
            )
            if checkpoint_path is not None
            else None
        ),
    )
    if not secret_values:
        return artifact
    scrubbed = tuple(
        cast(dict[str, JsonValue], scrub_secret_values(record, secret_values))
        for record in artifact.raw_records
    )
    if scrubbed == artifact.raw_records:
        return artifact
    return replace(artifact, raw_records=scrubbed)


def scrub_secret_values(value: JsonValue, secrets: tuple[str, ...]) -> JsonValue:
    """``value`` with every string occurrence of a secret — raw or URL-encoded — replaced."""
    if isinstance(value, str):
        cleaned = value
        for form in sorted(
            {f for s in secrets if s for f in (s, quote(s, safe=""), quote_plus(s))},
            key=len,
            reverse=True,
        ):
            cleaned = cleaned.replace(form, "[REDACTED]")
        return cleaned
    if isinstance(value, list):
        return [scrub_secret_values(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: scrub_secret_values(item, secrets) for key, item in value.items()}
    return value


def _fetch_bronze(
    source: SourceRef,
    *,
    client: SourceClient,
    upload_repository: UploadRepository | None,
    owner_id: str | None,
    fetched_at: datetime | None,
    on_combination_done: Callable[[int, int], None] | None = None,
    checkpoint: CombinationCheckpoint | None = None,
) -> BronzeArtifact:
    if source.kind == "file":
        return _build_from_upload(
            source, upload_repository=upload_repository, owner_id=owner_id, fetched_at=fetched_at
        )
    if source.kind == "url":
        return _build_from_url(source, fetched_at=fetched_at)
    if source.kind == "public_api":
        provider, dataset = source_identity(source)
        # if no param_grid, do not pass cartesian product list — single call path and
        # provenance shape preserved as before (#613).
        combinations = (
            expand_param_grid(dict(source.params), dict(source.param_grid))
            if source.param_grid
            else None
        )
        return build_bronze_artifact(
            client,
            source_key=f"{provider}.{dataset}",
            fetch_params=dict(source.params),
            fetched_at=fetched_at,
            param_combinations=combinations,
            on_combination_done=on_combination_done,
            checkpoint=checkpoint,
        )
    # BuildSpec that bypassed loader validation (direct SourceRef construction) also
    # already rejected, but, resolver itself "else is public_api"implicit
    # does not have fallback — canonical kind contract(public_api|file|url) outside
    # values also blocked fail-closed here(BLOCKER, #538 review).
    raise IngestionError(
        f"unsupported source kind: {source.kind!r} (canonical kinds: {SOURCE_KINDS})"
    )


def _finalize(
    *,
    provider: str,
    dataset: str,
    records: tuple[dict[str, JsonValue], ...],
    fetch_params: dict[str, JsonValue],
    fetched_at: datetime | None,
) -> BronzeArtifact:
    resolved_fetched_at = fetched_at or utc_now()
    require_timezone_aware(resolved_fetched_at, field_name="fetched_at")
    source_key = f"{provider}.{dataset}"
    provenance = ProvenanceEvent(
        source_key=source_key, fetch_params=fetch_params, fetched_at=resolved_fetched_at
    )
    return BronzeArtifact(
        source_key=source_key,
        raw_records=records,
        fetch_params=fetch_params,
        fetched_at=resolved_fetched_at,
        provenance=provenance,
    )


def _declared_read_as(source: SourceRef) -> dict[str, str]:
    """source's declared ``schema.read_as``extracted (#613)."""
    return dict(source.schema.read_as) if source.schema else {}


def _build_from_upload(
    source: SourceRef,
    *,
    upload_repository: UploadRepository | None,
    owner_id: str | None,
    fetched_at: datetime | None,
) -> BronzeArtifact:
    if upload_repository is None:
        raise IngestionError("file source requires an upload store to be configured")
    if not owner_id:
        raise IngestionError("file source requires an authenticated, stable principal owner")
    metadata = upload_repository.get_metadata(owner_id, source.upload_id)
    if metadata is None:
        # non-existent upload_id and "not owned by principal" upload_id
        # not distinguished — do not leak whether other user's upload exists
        # (fail-closed, #505 same as ownership pattern).
        raise IngestionError(f"upload not found: {source.upload_id}")
    if metadata.format != source.format or metadata.encoding != source.encoding:
        raise IngestionError(
            "sources[].format/encoding does not match the stored upload "
            f"(upload format={metadata.format!r} encoding={metadata.encoding!r})"
        )
    content = upload_repository.get_content(owner_id, source.upload_id)
    if content is None:
        raise IngestionError(f"upload not found: {source.upload_id}")
    records = parse_tabular_bytes(
        content,
        format=source.format,
        encoding=source.encoding,
        read_as=_declared_read_as(source),
    )
    provider, dataset = source_identity(source)
    fetch_params: dict[str, JsonValue] = {
        "upload_id": source.upload_id,
        "format": source.format,
        "encoding": source.encoding,
    }
    return _finalize(
        provider=provider,
        dataset=dataset,
        records=records,
        fetch_params=fetch_params,
        fetched_at=fetched_at,
    )


def _build_from_url(source: SourceRef, *, fetched_at: datetime | None) -> BronzeArtifact:
    result = safe_fetch_get(source.endpoint, max_bytes=default_max_fetch_bytes())
    resolved_format = source.format or _infer_format(result.content_type) or "json"
    records = parse_tabular_bytes(
        result.content,
        format=resolved_format,
        encoding="utf-8",
        read_as=_declared_read_as(source),
    )
    provider, dataset = source_identity(source)
    # fetch_params.endpoint is human-readable (query stripped) original endpoint — path
    # segments used as `dataset` (slug+hash, see source_identity) are different values.
    fetch_params: dict[str, JsonValue] = {
        "endpoint": sanitize_endpoint_identity(source.endpoint),
        "method": source.method,
    }
    return _finalize(
        provider=provider,
        dataset=dataset,
        records=records,
        fetch_params=fetch_params,
        fetched_at=fetched_at,
    )


def _infer_format(content_type: str) -> str | None:
    """infers format from Content-Type only when explicit `source.format` absent."""
    normalized = content_type.split(";", 1)[0].strip().lower()
    if normalized == "application/json":
        return "json"
    if normalized in ("application/x-ndjson", "application/jsonl"):
        return "jsonl"
    if normalized in ("text/csv", "application/csv"):
        return "csv"
    return None


__all__ = ["build_bronze_artifact_for_source", "sanitize_endpoint_identity", "source_identity"]

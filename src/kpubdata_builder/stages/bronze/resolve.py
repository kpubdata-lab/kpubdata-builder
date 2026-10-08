"""source kind resolver—creates identical BronzeArtifacts from public_api/file/url (#498)."""

from __future__ import annotations

import hashlib
import io
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote, quote_plus, urlsplit, urlunsplit

from ...ingestion import IngestionError, safe_fetch_get
from ...ingestion.tabular_ingest import iter_tabular_batches
from ...ingestion.url_fetch import default_max_fetch_bytes
from ...spec import JsonValue, SourceRef, expand_param_grid
from ...spec.models import SOURCE_KINDS
from ...uploads import UploadRepository
from .build import FetchBound, SourceClient, build_bronze_artifact
from .checkpoint import CombinationCheckpoint
from .models import BronzeArtifact, ProvenanceEvent, require_timezone_aware, utc_now
from .writer import BronzeWriter, Scrub, new_staging_dir

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
    staging_dir: Path | None = None,
    bound: FetchBound | None = None,
) -> BronzeArtifact:
    """fetches per source.kind and writes a BronzeArtifact (#498, #622).

    ``checkpoint_path`` (#648): the directory where a public_api ``param_grid`` fetch
    keeps each finished combination, scrubbed of ``secret_values``, and resumes from on
    a rebuild.

    ``staging_dir`` (#622): where the records are written as they arrive; a fresh
    private directory if omitted. The artifact points into it until it is discarded.

    ``on_combination_done`` reaches the ``param_grid`` loop of a public_api source
    (#648); other kinds make one read and never call it.

    ``secret_values`` are the requester's provider keys. A provider that echoes the
    request back in its response puts the key into the records, and from there into
    every stage output, card and export (#686). Bronze is where records enter, so it
    is where exact occurrences of a key are replaced — before anything is written.

    ``bound`` (#1185) stops a public_api fetch early, for a preview. A file or URL
    source is read whole: it costs no provider quota.
    """
    scrub: Scrub | None = (
        (lambda value: scrub_secret_values(value, secret_values)) if secret_values else None
    )
    if source.kind == "file":
        return _build_from_upload(
            source,
            upload_repository=upload_repository,
            owner_id=owner_id,
            fetched_at=fetched_at,
            staging_dir=staging_dir,
            scrub=scrub,
        )
    if source.kind == "url":
        return _build_from_url(source, fetched_at=fetched_at, staging_dir=staging_dir, scrub=scrub)
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
            checkpoint=(
                CombinationCheckpoint(checkpoint_path, scrub=scrub)
                if checkpoint_path is not None
                else None
            ),
            staging_dir=staging_dir,
            scrub=scrub,
            bound=bound,
        )
    # BuildSpec that bypassed loader validation (direct SourceRef construction) also
    # already rejected, but, resolver itself "else is public_api"implicit
    # does not have fallback — canonical kind contract(public_api|file|url) outside
    # values also blocked fail-closed here(BLOCKER, #538 review).
    raise IngestionError(
        f"unsupported source kind: {source.kind!r} (canonical kinds: {SOURCE_KINDS})"
    )


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


def _write_parsed(
    content: BinaryIO,
    *,
    source: SourceRef,
    format: str,  # noqa: A002 - matches contract field name
    encoding: str,
    fetch_params: dict[str, JsonValue],
    fetched_at: datetime | None,
    staging_dir: Path | None,
    scrub: Scrub | None,
) -> BronzeArtifact:
    """Parse ``content`` batch by batch straight into a Bronze writer."""
    staging_dir = staging_dir or new_staging_dir()
    resolved_fetched_at = fetched_at or utc_now()
    require_timezone_aware(resolved_fetched_at, field_name="fetched_at")
    with BronzeWriter(staging_dir, scrub=scrub) as writer:
        for batch in iter_tabular_batches(
            content,
            format=format,
            encoding=encoding,
            read_as=_declared_read_as(source),
            workdir=staging_dir,
        ):
            writer.write_batch(batch)
        records_path, record_count = writer.commit()
    provider, dataset = source_identity(source)
    source_key = f"{provider}.{dataset}"
    return BronzeArtifact(
        source_key=source_key,
        records_path=records_path,
        record_count=record_count,
        staging_dir=staging_dir,
        fetch_params=fetch_params,
        fetched_at=resolved_fetched_at,
        provenance=ProvenanceEvent(
            source_key=source_key, fetch_params=fetch_params, fetched_at=resolved_fetched_at
        ),
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
    staging_dir: Path | None,
    scrub: Scrub | None,
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
    # A stream, not the payload (#622): a large upload is read from its file as it is
    # parsed, never loaded whole.
    content = upload_repository.open_content(owner_id, source.upload_id)
    if content is None:
        raise IngestionError(f"upload not found: {source.upload_id}")
    fetch_params: dict[str, JsonValue] = {
        "upload_id": source.upload_id,
        "format": source.format,
        "encoding": source.encoding,
    }
    with content:
        return _write_parsed(
            content,
            source=source,
            format=source.format,
            encoding=source.encoding,
            fetch_params=fetch_params,
            fetched_at=fetched_at,
            staging_dir=staging_dir,
            scrub=scrub,
        )


def _build_from_url(
    source: SourceRef,
    *,
    fetched_at: datetime | None,
    staging_dir: Path | None,
    scrub: Scrub | None,
) -> BronzeArtifact:
    # A URL response is bounded by default_max_fetch_bytes, so it is read whole; the
    # parse and the write still go batch by batch.
    result = safe_fetch_get(source.endpoint, max_bytes=default_max_fetch_bytes())
    resolved_format = source.format or _infer_format(result.content_type) or "json"
    # fetch_params.endpoint is human-readable (query stripped) original endpoint — path
    # segments used as `dataset` (slug+hash, see source_identity) are different values.
    fetch_params: dict[str, JsonValue] = {
        "endpoint": sanitize_endpoint_identity(source.endpoint),
        "method": source.method,
    }
    return _write_parsed(
        io.BytesIO(result.content),
        source=source,
        format=resolved_format,
        encoding="utf-8",
        fetch_params=fetch_params,
        fetched_at=fetched_at,
        staging_dir=staging_dir,
        scrub=scrub,
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

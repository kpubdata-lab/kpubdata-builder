"""Upload domain service (#596, second segment after providers).

Same form as ``ProvidersService`` — **takes only self-dependencies**, wire contract
untouched. The upload domain only uses one repository.

Why receive the repository as a **callable provider** rather than object:
``UploadRepository`` creates SQLite file only on first use (#498). Accepting the
repository in the constructor would create ``.service/uploads.sqlite3`` even in
workspaces that never use uploads — lazy creation remains actual by fetching at
call time.
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable

from kpubdata_builder.ingestion import IngestionError
from kpubdata_builder.ingestion.tabular_ingest import iter_tabular_batches
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.service.upload_limits import UploadLimits, resolve_upload_limits
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.spec.models import SOURCE_FILE_FORMATS
from kpubdata_builder.uploads import UploadMetadata, UploadRepository

RepositoryProvider = Callable[[], UploadRepository]


def upload_metadata_body(metadata: UploadMetadata) -> dict[str, JsonValue]:
    """Convert UploadMetadata to wire JSON (#498). Content is never included."""
    return {
        "upload_id": metadata.upload_id,
        "format": metadata.format,
        "encoding": metadata.encoding,
        "size_bytes": metadata.size_bytes,
        "original_filename": metadata.original_filename,
        "created_at": metadata.created_at,
    }


class UploadsService:
    """Create/retrieve/delete uploads referenced by ``kind="file"`` source (#498)."""

    def __init__(
        self,
        *,
        repository: RepositoryProvider,
        limits: Callable[[], UploadLimits | None] = resolve_upload_limits,
    ) -> None:
        self._repository = repository
        # Read at call time, like the repository: the deployment's mode comes from the
        # environment, and a test sets it per case.
        self._limits = limits
        # The check and the insert are one step, so two uploads arriving together
        # cannot both fit under a limit only one of them fits under (#1045).
        self._quota_lock = threading.Lock()

    def _quota_refusal(
        self, owner_id: str, size: int, limits: UploadLimits
    ) -> ServiceResponse | None:
        """Why this upload would take the owner past a limit, or None when it fits."""
        usage = self._repository().usage_for_owner(owner_id)
        if limits.max_files is not None and usage.files >= limits.max_files:
            which, limit, used = "max_files", limits.max_files, usage.files
        elif (
            limits.max_total_bytes is not None and usage.total_bytes + size > limits.max_total_bytes
        ):
            which, limit, used = "max_total_bytes", limits.max_total_bytes, usage.total_bytes
        else:
            return None
        return ServiceResponse(
            409,
            {
                "error": (
                    "upload limit reached: delete an upload you no longer need, "
                    "then send this one again"
                ),
                "code": "upload_quota_exceeded",
                "limit": which,
                "limit_value": limit,
                "used": used,
            },
        )

    def create_upload(
        self,
        raw: bytes,
        *,
        format: str,  # noqa: A002 - match wire contract field name
        encoding: str,
        original_filename: str | None,
        principal: Principal,
    ) -> ServiceResponse:
        """Save upload content and validate immediate parseability (#498).

        Storage is isolated by owner_id — later, BuildSpec's ``kind="file"`` source
        referencing this upload_id must be from the same principal (build/preview
        resolvers re-check). Parseability is checked fail-fast here — avoids
        discovering corrupted files only at build time.
        """
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        if format not in SOURCE_FILE_FORMATS:
            return ServiceResponse(
                400,
                {"error": f"format must be one of {SOURCE_FILE_FORMATS}, got {format!r}"},
            )
        try:
            # Parsed to prove it parses; the batches are dropped as they come (#622).
            for _batch in iter_tabular_batches(io.BytesIO(raw), format=format, encoding=encoding):
                pass
        except IngestionError as exc:
            return ServiceResponse(400, {"error": str(exc)})
        limits = self._limits()
        with self._quota_lock:
            if limits is not None:
                # What is past retention does not count against the owner (#1045).
                cutoff = limits.cutoff()
                if cutoff is not None:
                    self._repository().purge_created_before(cutoff, owner_id=principal.owner_id)
                refusal = self._quota_refusal(principal.owner_id, len(raw), limits)
                if refusal is not None:
                    return refusal
            try:
                metadata = self._repository().put(
                    principal.owner_id,
                    content=raw,
                    format=format,
                    encoding=encoding,
                    original_filename=original_filename,
                )
            except ValueError as exc:
                return ServiceResponse(400, {"error": str(exc)})
        return ServiceResponse(200, upload_metadata_body(metadata))

    def purge_expired(self) -> int:
        """Delete every owner's uploads that are past retention; how many went (#1045)."""
        limits = self._limits()
        cutoff = limits.cutoff() if limits is not None else None
        if cutoff is None:
            return 0
        return self._repository().purge_created_before(cutoff)

    def get_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """Return safe metadata-only for upload owned by current principal (content excluded)."""
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        metadata = self._repository().get_metadata(principal.owner_id, upload_id)
        if metadata is None:
            return ServiceResponse(404, {"error": f"upload not found: {upload_id}"})
        return ServiceResponse(200, upload_metadata_body(metadata))

    def delete_upload(self, upload_id: str, *, principal: Principal) -> ServiceResponse:
        """Delete only uploads owned by current principal."""
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        deleted = self._repository().delete(principal.owner_id, upload_id)
        if not deleted:
            return ServiceResponse(404, {"error": f"upload not found: {upload_id}"})
        return ServiceResponse(200, {"upload_id": upload_id, "deleted": True})


__all__ = ["UploadsService", "upload_metadata_body"]

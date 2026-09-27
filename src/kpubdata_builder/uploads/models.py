"""Upload metadata model (#498)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UploadMetadata:
    """Safe (secret-free) metadata of stored upload.

    Raw content is obtained only via separate ``get_content()`` — never included
    in list/query responses.

    Attributes:
        upload_id: Server-issued opaque identifier (``upl_<hex32>``). Not
            user-specified filename/path.
        format: Format validated at upload time (csv/json/jsonl/parquet).
        encoding: Encoding validated at upload time (parquet is N/A).
        size_bytes: Content size.
        original_filename: Original filename from user (display-only, sanitized).
            Never used as filesystem path.
        created_at: ISO-8601 UTC creation time.
    """

    upload_id: str
    format: str
    encoding: str
    size_bytes: int
    original_filename: str | None
    created_at: str

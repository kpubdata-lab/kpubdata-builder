"""File/URL source ingestion (#498)."""

from __future__ import annotations

from .errors import IngestionError
from .tabular_ingest import parse_tabular_bytes
from .url_fetch import FetchResult, safe_fetch_get

__all__ = [
    "FetchResult",
    "IngestionError",
    "parse_tabular_bytes",
    "safe_fetch_get",
]

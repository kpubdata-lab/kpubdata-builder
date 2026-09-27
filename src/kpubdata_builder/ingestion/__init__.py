"""File/URL source ingestion (#498).

Public API source uses existing kpubdata client path (``stages.bronze.build``) as-is.
This package provides two things needed only for ``kind="file"``/``kind="url"`` sources.

    - ``url_fetch``: Safe GET (Auth=None) fetch defending against SSRF.
    - ``tabular_ingest``: Parse CSV/JSON/JSONL/Parquet raw bytes to records.

Both paths ultimately feed existing Bronze→Silver→Gold pipeline consuming
``dict[str, JsonValue]`` records — not new pipeline but front-end to existing
pipeline resolver.
"""

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

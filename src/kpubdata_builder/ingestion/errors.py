"""File/URL ingestion error hierarchy (#498)."""

from __future__ import annotations

from ..errors import BuildError


class IngestionError(BuildError):
    """Indicates fetch or parsing failure for file/url source (#498).

    SSRF blocking, response size exceeded, empty/corrupted content, unsupported format, etc.
    Hold only messages safe to show clearly explained to user — don't include raw response
    body or internal stack.
    """


__all__ = ["IngestionError"]

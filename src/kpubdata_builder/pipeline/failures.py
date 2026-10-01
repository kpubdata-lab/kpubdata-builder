"""Public failure messages for per-source pipeline errors (#225, #954).

Build (``orchestrator._run_source_pipeline``) and preview (``preview._preview_source``)
both turn a per-source exception into a message that reaches the caller — the build
manifest/events and the ``/preview`` response. Both paths share this one rule so they
cannot drift:

- Exceptions designed to carry only caller-safe text pass their message through
  unchanged (``PUBLIC_MESSAGE_ERRORS``).
- DuckDB's own out-of-memory/spill-quota error that escaped ``within_limits`` is
  stated as Builder's ``RESOURCE_LIMIT_MESSAGE`` (#701), never in the engine's words.
- Any other exception (engine errors such as ``duckdb.OutOfMemoryException``, export
  or manifest errors that may name filesystem paths, unexpected bugs) is replaced with
  a generic message, and the original is logged server-side with its traceback.
"""

from __future__ import annotations

import logging

import duckdb

from ..errors import DatasetValidationError, ValidationError
from ..ingestion import IngestionError
from ..stages.gold.pii import PiiDeclarationError
from ..stages.gold.select import GoldSelectionError
from ..tabular.duckdb_runtime import RESOURCE_LIMIT_MESSAGE, ResourceLimitError

logger = logging.getLogger(__name__)

# ValidationError/DatasetValidationError include no filesystem paths. IngestionError
# (#498) carries only safe messages (no raw response body/internal stack) — SSRF
# block/oversize/corrupt file reasons are visible to the user immediately.
# GoldSelectionError and PiiDeclarationError describe the spec's own declarations.
# ResourceLimitError (#701) only ever carries Builder's fixed RESOURCE_LIMIT_MESSAGE.
# Other BuildError subclasses (ExportError/ManifestError) may include internal info
# like destination paths, so they are deliberately absent (#225).
PUBLIC_MESSAGE_ERRORS: tuple[type[Exception], ...] = (
    ValidationError,
    DatasetValidationError,
    IngestionError,
    GoldSelectionError,
    PiiDeclarationError,
    ResourceLimitError,
)


def generic_failure_message(source_key: str) -> str:
    """Generic message returned for a source whose error text is not public."""
    return f"pipeline failed for source {source_key!r}"


def public_failure_message(exc: BaseException, source_key: str) -> str:
    """Message safe to return to the caller for ``exc`` raised while handling a source.

    Allow-listed exceptions keep their own message. DuckDB's out-of-memory error is
    logged and stated as ``RESOURCE_LIMIT_MESSAGE``. Anything else is logged with its
    traceback and replaced with :func:`generic_failure_message`.
    """
    if isinstance(exc, PUBLIC_MESSAGE_ERRORS):
        return str(exc)
    if isinstance(exc, duckdb.OutOfMemoryException):
        logger.error("source %r hit a resource limit: %s", source_key, exc, exc_info=exc)
        return RESOURCE_LIMIT_MESSAGE
    logger.error(
        "source pipeline failed for %r: %s",
        source_key,
        exc,
        exc_info=exc,
    )
    return generic_failure_message(source_key)

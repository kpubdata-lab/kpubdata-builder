"""Public failure messages for per-source pipeline errors (#225, #954).

Build (``orchestrator._run_source_pipeline``) and preview (``preview._preview_source``)
both turn a per-source exception into a message that reaches the caller — the build
manifest/events and the ``/preview`` response. Both paths share this one rule so they
cannot drift:

- Exceptions designed to carry only caller-safe text pass their message through
  unchanged (``PUBLIC_MESSAGE_ERRORS``).
- DuckDB's own out-of-memory/spill-quota error that escaped ``within_limits`` is
  stated as Builder's ``RESOURCE_LIMIT_MESSAGE`` (#701), never in the engine's words.
- A provider's refusal (kpubdata's typed errors) is stated by its reason (#1187): one
  of the key check's words, with a fixed sentence saying what to do next. The
  provider's own text is neither returned nor logged: it can echo the request, key
  included. The log has the error type, the provider's code and the HTTP status.
- Any other exception (engine errors such as ``duckdb.OutOfMemoryException``, export
  or manifest errors that may name filesystem paths, unexpected bugs) is replaced with
  a generic message, and the original is logged server-side with its traceback.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import duckdb
from kpubdata import (
    AuthError,
    InvalidRequestError,
    RateLimitError,
    ServiceUnavailableError,
    TransportError,
    TransportTimeoutError,
)

from ..errors import DatasetValidationError, TabularError, ValidationError
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
# TabularError (#984) names the spec's own columns and dtypes — a sweep of all 35
# call sites (`grep -rn "raise TabularError(" src/`) found none that embed a
# filesystem path, workdir or other server internal; every message describes what
# the data or declaration got wrong, in the caller's terms.
# Other BuildError subclasses (ExportError/ManifestError) may include internal info
# like destination paths, so they are deliberately absent (#225).
PUBLIC_MESSAGE_ERRORS: tuple[type[Exception], ...] = (
    ValidationError,
    DatasetValidationError,
    IngestionError,
    GoldSelectionError,
    PiiDeclarationError,
    ResourceLimitError,
    TabularError,
)


SourceFailureReason = Literal[
    "application_required",
    "auth_unknown",
    "params_invalid",
    "rate_limited",
    "temporarily_unavailable",
    "network_error",
    "retired",
]
"""Why a provider refused a source (#1187), in the key check's words.

The values are the probe statuses Builder already serves (``vocabulary.AccessStatus``,
``POST /providers/{provider}/probe``), so a client explains a failed build the way it
explains a probe. Only the statuses a refused call can have are used.
"""

SOURCE_FAILURE_REASONS: tuple[SourceFailureReason, ...] = (
    "application_required",
    "auth_unknown",
    "params_invalid",
    "rate_limited",
    "temporarily_unavailable",
    "network_error",
    "retired",
)

# data.go.kr's standard result codes (the table kpubdata's executor raises from, and its
# probe classifies by). The code says more than the exception type: a 403 can be "not
# applied for" (30) or "this IP is not registered" (32), which need different advice.
_DATAGO_CODES: dict[str, SourceFailureReason] = {
    "01": "temporarily_unavailable",  # APPLICATION_ERROR
    "02": "temporarily_unavailable",  # DB_ERROR
    "04": "temporarily_unavailable",  # HTTP_ERROR
    "05": "temporarily_unavailable",  # SERVICETIME_OUT
    "10": "params_invalid",  # INVALID_REQUEST_PARAMETER
    "11": "params_invalid",  # NO_MANDATORY_REQUEST_PARAMETERS
    "12": "retired",  # NO_OPENAPI_SERVICE: the service is not there
    "20": "application_required",  # SERVICE_ACCESS_DENIED
    "21": "auth_unknown",  # TEMPORARILY_DISABLE_THE_SERVICEKEY
    "22": "rate_limited",  # LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS
    "30": "application_required",  # SERVICE_KEY_IS_NOT_REGISTERED
    "31": "application_required",  # DEADLINE_HAS_EXPIRED
    "32": "auth_unknown",  # UNREGISTERED_IP
    "33": "auth_unknown",  # UNSIGNED_CALL
}

_REASON_MESSAGES: dict[SourceFailureReason, str] = {
    "application_required": (
        "the provider refused this key for this dataset: the key has no approved "
        "application (활용신청) for it. Apply for the dataset with this key at the "
        "provider, or wait until the application is approved, then build again."
    ),
    "auth_unknown": (
        "the provider did not accept this key. Check that it is the key the provider "
        "issued and that the provider has not suspended it, and that the provider allows "
        "calls from this server."
    ),
    "params_invalid": (
        "the provider refused the request's parameters. Check the source's params "
        "against the dataset's required parameters and their formats."
    ),
    "rate_limited": (
        "the provider's request limit for this key is used up. Try again after it "
        "resets; a data.go.kr key's daily limit resets the next day."
    ),
    "temporarily_unavailable": ("the provider is temporarily unavailable. Try again later."),
    "network_error": "the provider could not be reached. Try again later.",
    "retired": (
        "the provider no longer offers this dataset's service, so it cannot be fetched. "
        "Check the dataset's status in the catalogue."
    ),
}

# A key copied in its URL-encoded form ("Encoding" on data.go.kr) is encoded again on
# the way out, so a registered key is refused as unregistered (#1187).
_ENCODED_KEY = re.compile(r"%[0-9A-Fa-f]{2}")
_ENCODED_KEY_HINT = (
    " The key contains a percent-encoded sequence such as %2B: use the key the provider "
    'shows decoded (data.go.kr\'s "Decoding" key), not the encoded one.'
)


@dataclass(frozen=True)
class SourceFailure:
    """What a caller is told about a failed source: a safe message and, when a provider
    refused the call, why (#1187)."""

    message: str
    reason: SourceFailureReason | None = None


def provider_failure_reason(exc: BaseException) -> SourceFailureReason | None:
    """Why a provider refused the call that raised ``exc``; None when it did not.

    The data.go.kr result code decides where there is one. Otherwise the kpubdata error
    type does, narrowest first: ``RateLimitError`` and ``TransportTimeoutError`` are
    ``TransportError`` subclasses.

    An error raised because this installation lacks a decoder (an XML response without
    ``xmltodict``) is not the provider's refusal, whatever its type says: None.
    """
    if isinstance(exc.__cause__, ImportError):
        return None
    code = getattr(exc, "provider_code", None)
    if getattr(exc, "provider", None) == "datago" and isinstance(code, str):
        mapped = _DATAGO_CODES.get(code.strip())
        if mapped is not None:
            return mapped
    status = getattr(exc, "status_code", None)
    if isinstance(exc, RateLimitError):
        return "rate_limited"
    if isinstance(exc, TransportTimeoutError):
        return "network_error"
    if isinstance(exc, AuthError):
        # HTTP 401 refuses the key itself; anything else (403, a code) its access.
        return "auth_unknown" if status == 401 else "application_required"
    if isinstance(exc, InvalidRequestError):
        return "params_invalid"
    if isinstance(exc, ServiceUnavailableError):
        return "temporarily_unavailable"
    if isinstance(exc, TransportError):
        if status == 400:
            return "params_invalid"
        if status == 401:
            return "auth_unknown"
        if status == 403:
            return "application_required"
        if status == 429:
            return "rate_limited"
        if status is None:
            # No HTTP exchange completed: DNS, TLS, connection refused.
            return "network_error"
        if getattr(exc, "retryable", False) or (isinstance(status, int) and status >= 500):
            return "temporarily_unavailable"
    return None


def source_failure(
    exc: BaseException,
    source_key: str,
    *,
    provider_keys: Mapping[str, str] | None = None,
) -> SourceFailure:
    """:func:`public_failure_message` with the provider's reason, when there is one.

    ``provider_keys`` are the requester's keys by provider. They are never repeated or
    logged. When the refusing provider's key looks URL-encoded, a refused key's message
    says to use the decoded one. Keys Builder was not handed — the CLI's and the
    server's own, read by kpubdata from the environment — get no hint.
    """
    reason = provider_failure_reason(exc)
    if reason is None:
        return SourceFailure(public_failure_message(exc, source_key))
    # The provider's text is not logged: it can echo the request, key included, in
    # forms no scrub can know. Its code and status are what an operator needs.
    logger.warning(
        "provider refused source %r (%s): %s provider_code=%r status=%r",
        source_key,
        reason,
        type(exc).__name__,
        getattr(exc, "provider_code", None),
        getattr(exc, "status_code", None),
    )
    message = f"source {source_key!r}: {_REASON_MESSAGES[reason]}"
    refused_key = (provider_keys or {}).get(str(getattr(exc, "provider", "") or ""))
    if (
        reason in ("application_required", "auth_unknown")
        and refused_key
        and _ENCODED_KEY.search(refused_key)
    ):
        message += _ENCODED_KEY_HINT
    return SourceFailure(message, reason)


def reason_sentence(reason: str) -> str | None:
    """The fixed sentence of a refusal reason (#1187), or None for a word that is not one.

    For a list that serves other owners' runs (#1221): the sentence holds nothing of
    the run's data, so it can be shown wherever the reason is.
    """
    for known, sentence in _REASON_MESSAGES.items():
        if known == reason:
            return sentence
    return None


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

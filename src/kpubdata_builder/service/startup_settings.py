"""Read every setting once before the server takes a request (#1108).

Each setting is read where it is used, and a value that cannot be used showed up there:
a DuckDB memory limit of ``lots`` as a failed build, ``OIDC_JWKS_TTL=1h`` as a failed
sign-in, a query limit of ``0`` as a traceback while the service was being put
together. ``serve`` calls :func:`check_settings` first and prints what it finds, all of
it at once, so an operator fixes a deployment in one pass rather than one start per
mistake.

Two outcomes, because the readers already differ and this module does not change them:

- **A problem** stops the start. These are the values the reader itself refuses — the
  check calls the reader, so the rule is written once — and the ones that had no check
  and broke on use.
- **A warning** does not. These readers fall back to the default when the value is
  unreadable, by design: the service starts, with a setting other than the one the
  operator wrote. The warning says which, so that it is at least visible. A flag written
  as anything but ``true``/``1``/``false``/``0`` is in this group: it is read as off.

No message repeats the value of a variable that holds a secret.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from ..credentials.crypto import AesGcmCredentialCipher, CredentialCryptoError
from ..query.service import (
    query_max_concurrency_from_env,
    query_memory_budget_from_env,
    query_memory_limit_from_env,
)
from ..store.backend import cubrid_url, storage_backend
from ..tabular.duckdb_runtime import BuildProfile
from .http import shutdown_grace_seconds

#: Settings ``serve`` reads itself, with its flags taking precedence; it reports them.
READ_BY_SERVE: frozenset[str] = frozenset(
    {
        "KPUBDATA_BUILDER_MAX_WORKERS",
        "KPUBDATA_BUILDER_MAX_BUILDS",
        "KPUBDATA_BUILDER_MAX_PREVIEWS",
        "KPUBDATA_BUILDER_BUILD_WAIT_SECONDS",
    }
)


@dataclass(frozen=True)
class SettingsReport:
    """What :func:`check_settings` found. Each entry is one line for the operator."""

    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _raw(name: str) -> str:
    return os.environ.get(name, "").strip()


def _integer(raw: str) -> int | None:
    try:
        return int(raw)
    except ValueError:
        return None


def _number(raw: str) -> float | None:
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _is_integer(raw: str) -> bool:
    return _integer(raw) is not None


def _is_positive_integer(raw: str) -> bool:
    value = _integer(raw)
    return value is not None and value > 0


def _is_non_negative_integer(raw: str) -> bool:
    value = _integer(raw)
    return value is not None and value >= 0


def _is_positive_number(raw: str) -> bool:
    # ``inf`` is positive to the readers that take a positive number; ``nan`` is not.
    try:
        return float(raw) > 0
    except ValueError:
        return False


def _is_non_negative_number(raw: str) -> bool:
    value = _number(raw)
    return value is not None and value >= 0


#: Settings whose reader uses the default when the value is unreadable: what the reader
#: accepts, and how to say it. ``tests/unit/test_startup_settings.py`` holds each
#: predicate against its reader.
FALLS_BACK: dict[str, tuple[Callable[[str], bool], str]] = {
    "KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT": (_is_integer, "an integer"),
    "KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS": (_is_positive_number, "a number > 0"),
    "KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS": (_is_non_negative_number, "a finite number >= 0"),
    "KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS": (_is_positive_number, "a number > 0"),
    "KPUBDATA_BUILDER_CHECKPOINT_MAX_AGE_SECONDS": (
        _is_non_negative_number,
        "a finite number >= 0",
    ),
    "KPUBDATA_BUILDER_MAX_UPLOAD_BYTES": (_is_positive_integer, "an integer > 0"),
    "KPUBDATA_BUILDER_UPLOAD_MAX_FILES": (_is_non_negative_integer, "an integer >= 0"),
    "KPUBDATA_BUILDER_UPLOAD_MAX_TOTAL_BYTES": (_is_non_negative_integer, "an integer >= 0"),
    "KPUBDATA_BUILDER_UPLOAD_RETENTION_DAYS": (_is_non_negative_integer, "an integer >= 0"),
    "KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES": (_is_positive_integer, "an integer > 0"),
}

#: Flags, and whether the reader strips the value first. Each reader takes ``true`` or
#: ``1`` as on and everything else as off.
FLAGS: dict[str, bool] = {
    "KPUBDATA_BUILDER_DEV_MODE": False,
    "ENFORCE_OWNERSHIP": False,
    "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL": True,
    "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL": False,
}
_FLAG_WORDS = frozenset({"true", "1", "false", "0"})

#: Settings a reader refuses, or that had no check and broke when used.
REFUSED: frozenset[str] = frozenset(
    {
        "KPUBDATA_DUCKDB_THREADS",
        "KPUBDATA_DUCKDB_MEMORY_LIMIT",
        "KPUBDATA_DUCKDB_MAX_TEMP_SIZE",
        "KPUBDATA_QUERY_MAX_CONCURRENCY",
        "KPUBDATA_QUERY_MAX_MEMORY_MB",
        "KPUBDATA_QUERY_MEMORY_BUDGET_MB",
        "KPUBDATA_BUILDER_STORAGE_BACKEND",
        "KPUBDATA_BUILDER_CUBRID_URL",
        "KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS",
        "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY",
        "KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT",
        "OIDC_JWKS_TTL",
    }
)


def _storage() -> None:
    if storage_backend() == "cubrid":
        cubrid_url()


def _master_key() -> None:
    encoded = os.environ.get("KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY")
    if encoded:
        try:
            AesGcmCredentialCipher.from_base64(encoded)
        except CredentialCryptoError as exc:
            # The cipher's message names what is wrong and never the key.
            raise ValueError(f"KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY: {exc}") from None


def _provider_test_timeout() -> None:
    raw = _raw("KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT")
    if not raw:
        return
    value = _number(raw)
    if value is None or value <= 0:
        raise ValueError(
            f"KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT must be a finite number > 0, got {raw!r}"
        )


def _jwks_ttl() -> None:
    raw = os.environ.get("OIDC_JWKS_TTL", "")
    # Read the way the two places that use it do: ``int`` of the text as written.
    if raw and _integer(raw) is None:
        raise ValueError(f"OIDC_JWKS_TTL must be a whole number of seconds, got {raw!r}")


_READERS: tuple[Callable[[], object], ...] = (
    BuildProfile.from_env,
    query_max_concurrency_from_env,
    query_memory_limit_from_env,
    query_memory_budget_from_env,
    _storage,
    shutdown_grace_seconds,
    _master_key,
    _provider_test_timeout,
    _jwks_ttl,
)


def check_settings() -> SettingsReport:
    """Every setting that cannot be used as written, found without starting anything.

    Nothing here opens a file, a socket or a database; the checks that do
    (``validate_storage_config``, the OIDC combination rules) stay in ``serve``.
    """
    report = SettingsReport()
    for read in _READERS:
        try:
            read()
        except (ValueError, RuntimeError) as exc:
            report.problems.append(str(exc))
    for name, (accepts, expected) in FALLS_BACK.items():
        raw = _raw(name)
        if raw and not accepts(raw):
            report.warnings.append(
                f"{name} must be {expected}, got {raw!r}; ignored, the default is in use"
            )
    for name, strips in FLAGS.items():
        raw = os.environ.get(name, "")
        # A reader that does not strip judges the value as written: ``"true "`` is off.
        written = raw.strip() if strips else raw
        if written and written.lower() not in _FLAG_WORDS:
            report.warnings.append(
                f"{name} is {raw!r}, which is neither true/1 nor false/0; it is read as off"
            )
    return report

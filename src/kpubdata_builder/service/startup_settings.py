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
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

from ..credentials.crypto import AesGcmCredentialCipher, CredentialCryptoError
from ..query.service import (
    query_max_concurrency_from_env,
    query_memory_budget_from_env,
    query_memory_limit_from_env,
)
from ..store.backend import cubrid_url, storage_backend
from ..tabular.duckdb_runtime import BuildProfile
from .auth import oidc_enabled
from .http import shutdown_grace_seconds
from .ownership import multi_user_mode

#: Settings ``serve`` reads itself, each with a flag that takes precedence. A value the
#: flag overrides is never read, so the caller says which those are.
READ_BY_SERVE: frozenset[str] = frozenset(
    {
        "KPUBDATA_BUILDER_MAX_WORKERS",
        "KPUBDATA_BUILDER_MAX_BUILDS",
        "KPUBDATA_BUILDER_MAX_PREVIEWS",
        "KPUBDATA_BUILDER_BUILD_WAIT_SECONDS",
    }
)

#: Settings the container entrypoint hands to ``serve`` as flags, checked here so that a
#: stack's values can be judged without starting a container.
READ_BY_ENTRYPOINT: frozenset[str] = frozenset({"KPUBDATA_BUILDER_PORT"})


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
    "KPUBDATA_BUILDER_MAX_ACTIVE_BUILDS_PER_OWNER": (_is_non_negative_integer, "an integer >= 0"),
    "KPUBDATA_BUILDER_BUILD_TIME_LIMIT_SECONDS": (
        _is_non_negative_number,
        "a finite number >= 0",
    ),
    "KPUBDATA_BUILDER_UPLOAD_MAX_TOTAL_BYTES": (_is_non_negative_integer, "an integer >= 0"),
    "KPUBDATA_BUILDER_UPLOAD_RETENTION_DAYS": (_is_non_negative_integer, "an integer >= 0"),
    "KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES": (_is_positive_integer, "an integer > 0"),
}

#: Flags: whether the reader strips the value first, and the words it takes as on.
#: Everything else is off.
FLAGS: dict[str, tuple[bool, frozenset[str]]] = {
    "KPUBDATA_BUILDER_DEV_MODE": (False, frozenset({"true", "1"})),
    "ENFORCE_OWNERSHIP": (False, frozenset({"true", "1"})),
    # This reader alone takes ``yes`` and ``on`` (``service/providers.py``).
    "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL": (
        True,
        frozenset({"true", "1", "yes", "on"}),
    ),
    "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL": (False, frozenset({"true", "1"})),
}
#: The words that say off on purpose. Any other word is off too, and gets a warning.
_OFF_WORDS = frozenset({"false", "0"})

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


def _serve_problems(skip: Collection[str]) -> list[str]:
    """What ``serve`` and the entrypoint would refuse, said before either is reached."""
    problems: list[str] = []

    def check(name: str, accepts: Callable[[str], bool], expected: str) -> None:
        raw = _raw(name)
        if name not in skip and raw and not accepts(raw):
            problems.append(f"{name} must be {expected}, got {raw!r}")

    for name in (
        "KPUBDATA_BUILDER_MAX_WORKERS",
        "KPUBDATA_BUILDER_MAX_BUILDS",
        "KPUBDATA_BUILDER_MAX_PREVIEWS",
    ):
        check(name, _is_positive_integer, "an integer >= 1")
    check("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", _is_non_negative_number, "a finite number >= 0")
    # Not stripped first, unlike the rest: the entrypoint hands the value to ``--port``
    # as it is, and one of nothing but spaces is a usage error there, not "not set".
    port = os.environ.get("KPUBDATA_BUILDER_PORT", "")
    if "KPUBDATA_BUILDER_PORT" not in skip and port and not _is_port(port):
        problems.append(
            f"KPUBDATA_BUILDER_PORT must be a port number from 0 to 65535, got {port!r}"
        )
    return problems


def _is_port(raw: str) -> bool:
    value = _integer(raw)
    return value is not None and 0 <= value <= 65535


def _forced_on(name: str) -> bool:
    """Whether the deployment turns ``name`` on whatever the variable says.

    Sign-in through OIDC makes a deployment multi-user, and so does
    ``ENFORCE_OWNERSHIP``; a multi-user deployment keeps each user's runs apart and
    uses nobody's credentials but the requester's (ADR 0012).
    """
    if name == "ENFORCE_OWNERSHIP":
        return oidc_enabled()
    if name in (
        "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL",
        "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL",
    ):
        return multi_user_mode()
    return False


def check_settings(*, overridden: Collection[str] = ()) -> SettingsReport:
    """Every setting that cannot be used as written, found without starting anything.

    Nothing here opens a file, a socket or a database; the checks that do
    (``validate_storage_config``, the OIDC combination rules) stay in ``serve``.

    Args:
        overridden: Settings a command-line flag takes the place of. Their values in
            the environment are never read, so they are not judged.
    """
    report = SettingsReport()
    for read in _READERS:
        try:
            read()
        except (ValueError, RuntimeError) as exc:
            report.problems.append(str(exc))
    report.problems.extend(_serve_problems(overridden))
    for name, (accepts, expected) in FALLS_BACK.items():
        raw = _raw(name)
        if raw and not accepts(raw):
            report.warnings.append(
                f"{name} must be {expected}, got {raw!r}; ignored, the default is in use"
            )
    for name, (strips, on_words) in FLAGS.items():
        raw = os.environ.get(name, "")
        # A reader that does not strip judges the value as written: ``"true "`` is off.
        written = (raw.strip() if strips else raw).lower()
        if not written or written in on_words:
            continue
        if _forced_on(name):
            # Whatever it says: a warning that it "is read as off" would tell an
            # operator that a switch which is on is off.
            report.warnings.append(
                f"{name} is {raw!r}, which is ignored: this deployment serves more than "
                "one user, and that turns it on whatever it says"
            )
        elif written not in _OFF_WORDS:
            accepted = "/".join(sorted(on_words))
            report.warnings.append(
                f"{name} is {raw!r}, which is neither {accepted} nor false/0; it is read as off"
            )
    return report

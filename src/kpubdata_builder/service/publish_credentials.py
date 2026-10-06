"""Publish credential resolution — per-requester priority, server-wide fallback (#635).

Publish credentials were server-wide environment variables, not per-requester. This
meant **any authenticated user could publish as the server owner's Hugging Face /
Kaggle account**. Provider credentials already maintain per-principal repositories
(ADR 0012), but the publish path bypassed them and read environment variables
directly.

This module does not change defaults. If requester has stored credentials, they are
used; otherwise, server environment variables are consulted as before — for
single-user deployments, one global token is correct configuration and is preserved.
For multi-user deployments that want per-requester separation, that is now possible.
This module makes the mechanism available; deployment configuration enables it.

In a multi-user deployment (#925, ADR 0020 item 2 as confirmed on 2026-10-01) a publish
token is a key under the same rule as a provider key (#683): it is taken only from the
current request's ``X-Publish-Credential`` header and held in memory until the request
ends. Nothing stored is read — not even a token saved before the deployment switched
modes — and nothing from the server environment is used, whatever
``REQUIRE_OWN_PUBLISH_CREDENTIAL`` says.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

from kpubdata_builder.credentials.store import CredentialRepository

__all__ = [
    "PUBLISH_CREDENTIAL_HEADER",
    "PublishCredentialSource",
    "PUBLISH_CREDENTIAL_SLOTS",
    "PublishCredentialResolution",
    "current_publish_credential",
    "parse_publish_credential_headers",
    "request_scope",
    "publish_credential_source",
    "resolve_publish_credentials",
    "server_fallback_allowed",
]

logger = logging.getLogger(__name__)

#: If true, do not fall back to server environment variables when requester has
#: no stored credentials. Default: unset (= fallback allowed) — preserves
#: single-user deployments.
_REQUIRE_OWN_CREDENTIAL_ENV = "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL"

#: Per-target, environment variable names required and the credential slot storing
#: their value.
#:
#: Prefix slot names with ``publish-`` so they don't collide with provider
#: credentials (``datago`` etc.) — the same owner should be able to have both.
PUBLISH_CREDENTIAL_SLOTS: Mapping[str, tuple[str, ...]] = {
    "huggingface": ("HF_TOKEN",),
    "kaggle": ("KAGGLE_USERNAME", "KAGGLE_KEY"),
    "local": (),
}

#: The request header that carries publish credentials in a multi-user deployment (#925):
#: ``<VARIABLE>=<value>`` where the variable is one of the names above (``HF_TOKEN``,
#: ``KAGGLE_USERNAME``, ``KAGGLE_KEY``), one per header or comma-separated. Like
#: ``X-Provider-Key``, it is a header and never a URL query, which proxies and logs keep.
PUBLISH_CREDENTIAL_HEADER = "X-Publish-Credential"

_KNOWN_VARIABLES = frozenset(v for vs in PUBLISH_CREDENTIAL_SLOTS.values() for v in vs)

_request_values: ContextVar[Mapping[str, str] | None] = ContextVar(
    "kpubdata_request_publish_credentials", default=None
)


def parse_publish_credential_headers(values: Iterable[str]) -> dict[str, str]:
    """``X-Publish-Credential`` header values → ``{VARIABLE: value}``.

    The variable name is matched without regard to case and returned upper-case.

    Raises:
        ValueError: A value is not ``<VARIABLE>=<value>``, names a variable no publish
            target uses, or gives one variable two different values. The message never
            contains a value.
    """
    parsed: dict[str, str] = {}
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if not item:
                continue
            name, separator, secret = item.partition("=")
            name, secret = name.strip().upper(), secret.strip()
            if not separator or not name or not secret:
                raise ValueError(f"{PUBLISH_CREDENTIAL_HEADER} must be '<VARIABLE>=<value>'")
            if name not in _KNOWN_VARIABLES:
                raise ValueError(
                    f"{PUBLISH_CREDENTIAL_HEADER} names an unknown variable; "
                    f"expected one of {sorted(_KNOWN_VARIABLES)}"
                )
            if parsed.get(name, secret) != secret:
                raise ValueError(f"{PUBLISH_CREDENTIAL_HEADER} gives {name} two different values")
            parsed[name] = secret
    return parsed


def route_reads_publish_credentials(method: str, path: str) -> bool:
    """Whether the route uses the request's publish credentials — the operations the
    contract declares ``X-Publish-Credential`` on (a test holds the two together).

    A malformed header is an error only there (#1105). Elsewhere the request carries no
    publish credential, exactly as if the header were absent.
    """
    parts = path.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "builds" or parts[2] != "publish":
        return False
    operation = "/".join(parts[3:])
    return (method, operation) in (
        ("POST", ""),
        ("POST", "reconcile"),
        ("GET", "readiness"),
        ("GET", "receipt"),
        ("DELETE", "receipt"),
    )


@contextmanager
def request_scope(values: Mapping[str, str] | None) -> Iterator[None]:
    """Make ``values`` the current request's publish credentials until the block ends."""
    token = _request_values.set(dict(values) if values else None)
    try:
        yield
    finally:
        _request_values.reset(token)


def current_publish_credential(variable: str) -> str | None:
    """The current request's value for ``variable`` (e.g. ``HF_TOKEN``), or None."""
    values = _request_values.get()
    return None if values is None else values.get(variable.upper())


def _multi_user_mode() -> bool:
    # Imported here: ownership pulls in the auth configuration, which this module's
    # importers (the publish service, the admin route) do not need at import time.
    from .ownership import multi_user_mode

    return multi_user_mode()


def _slot(target: str, variable: str) -> str:
    """Credential slot name. Must pass ``normalize_provider``.

    Repository validates provider key as ``^[a-z0-9][a-z0-9_-]{0,63}$``, so
    colons cannot be used. Initially created as ``publish:huggingface:HF_TOKEN``
    but raised ValueError in actual SQLite repository — tested only with fake
    repository. Use hyphens to separate, lowercase throughout.
    """
    return f"publish-{target}-{variable}".lower().replace("_", "-")


@dataclass(frozen=True, slots=True)
class PublishCredentialResolution:
    """Credentials to use for this publish and why they are empty if so.

    An empty mapping alone cannot distinguish three different cases — target
    requires no credentials (local), no values exist anywhere, or no stored values
    exist and **policy refuses** them. When callers saw an empty dict and thought
    "then I won't pass it", publisher fell back to ``os.environ`` and
    ``REQUIRE_OWN_PUBLISH_CREDENTIAL`` had no effect.
    """

    # Kept out of repr: a repr reaches logs, exception messages and test output (#686).
    values: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: Requester has no stored credentials and server fallback is also closed.
    refused: bool = False
    #: This target requires no credentials (local).
    not_required: bool = False
    #: Multi-user deployment (#925): the only accepted source is the request's
    #: ``X-Publish-Credential`` header, so a refusal tells the caller to send it there.
    request_only: bool = False


def resolve_publish_credentials(
    repository: CredentialRepository | None,
    owner_id: str | None,
    target: str,
) -> PublishCredentialResolution:
    """Resolve credentials to use for this publish.

    Priority matches ``CredentialResolver`` — requester credentials first, then
    server environment variables. If neither has a value, that key is absent from
    the result.

    **Does not do partial resolution.** For targets like Kaggle where two values
    form a pair, mixing one from requester and one from server means no one knows
    which account publishes. If requester has stored any value for that target,
    the target is resolved from requester credentials only.

    **Multi-user deployment (#925).** Only the current request's
    ``X-Publish-Credential`` values count, and only when they cover every variable the
    target needs. The repository is never read and the server environment never
    consulted; without a complete set the result is ``refused``.
    """
    variables = PUBLISH_CREDENTIAL_SLOTS.get(target, ())
    if not variables:
        return PublishCredentialResolution(not_required=True)

    if _multi_user_mode():
        from_request = {
            variable: value
            for variable in variables
            if (value := current_publish_credential(variable))
        }
        if len(from_request) == len(variables):
            return PublishCredentialResolution(values=from_request, request_only=True)
        return PublishCredentialResolution(refused=True, request_only=True)

    stored: dict[str, str] = {}
    if repository is not None and owner_id is not None:
        for variable in variables:
            # Repository lookup failures (key format, decryption, backend error) are
            # not propagated as publish failures — treated same as stored credentials
            # not existing and fall back to server. Log it though.
            try:
                value = repository.get_secret(owner_id, _slot(target, variable))
            except Exception:
                logger.warning(
                    "could not read the stored %s publish credential for this principal; "
                    "falling back to the server environment",
                    target,
                    exc_info=True,
                )
                stored = {}
                break
            if value:
                stored[variable] = value
    if stored:
        return PublishCredentialResolution(values=stored)

    if not server_fallback_allowed():
        # Closing server token fallback means principals without stored credentials
        # cannot publish. Multi-user deployments need this switch to end
        # "anyone can publish as the server owner account" (#635).
        return PublishCredentialResolution(refused=True)

    resolved: dict[str, str] = {}
    for variable in variables:
        value = os.environ.get(variable, "").strip()
        if value:
            resolved[variable] = value
    return PublishCredentialResolution(values=resolved)


def server_fallback_allowed() -> bool:
    """Check if server environment variables are consulted when no stored credentials exist.

    Default: allowed — for single-user deployments, one global token is correct
    configuration and is preserved. Flipping the default would silently break that
    deployment. A single-user deployment can close it with
    ``KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL=true``; a multi-user deployment
    has it closed whatever that variable says (#925, ADR 0020 items 2 to 4).
    """
    if _multi_user_mode():
        return False
    return os.environ.get(_REQUIRE_OWN_CREDENTIAL_ENV, "").lower() not in ("true", "1")


#: Where this deployment takes a publish credential from (#938), as ``GET /version``
#: tells a client: ``request`` — only the request's ``X-Publish-Credential`` header
#: (multi-user, #925); ``stored`` — only the requester's stored ``publish-*`` credential
#: (single-user with ``REQUIRE_OWN_PUBLISH_CREDENTIAL``, #635); ``stored_or_server`` —
#: the stored credential, else the server's ``HF_TOKEN`` / ``KAGGLE_*`` (single-user
#: default).
PublishCredentialSource = Literal["request", "stored", "stored_or_server"]


def publish_credential_source() -> PublishCredentialSource:
    """Where a publish credential comes from in this deployment (#938).

    This is the policy :func:`resolve_publish_credentials` follows, and nothing more:
    it does not say whether the server has a token configured, or whether anyone has
    stored one. So it is safe to tell any authenticated caller, where
    ``GET /admin/config`` stays the administrator's view.
    """
    if _multi_user_mode():
        return "request"
    return "stored_or_server" if server_fallback_allowed() else "stored"

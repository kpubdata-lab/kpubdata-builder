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
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from kpubdata_builder.credentials.store import CredentialRepository

__all__ = [
    "PUBLISH_CREDENTIAL_SLOTS",
    "PublishCredentialResolution",
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

    values: Mapping[str, str] = field(default_factory=dict)
    #: Requester has no stored credentials and server fallback is also closed.
    refused: bool = False
    #: This target requires no credentials (local).
    not_required: bool = False


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
    """
    variables = PUBLISH_CREDENTIAL_SLOTS.get(target, ())
    if not variables:
        return PublishCredentialResolution(not_required=True)

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
    deployment. Multi-user deployments close fallback via
    ``KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL=true``.
    """
    return os.environ.get(_REQUIRE_OWN_CREDENTIAL_ENV, "").lower() not in ("true", "1")

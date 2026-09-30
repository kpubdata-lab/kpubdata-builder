"""Provider credential resolution and connection test service."""

from __future__ import annotations

import logging
import os
import socket
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from kpubdata import Client, Operation
from kpubdata.config import KPubDataConfig
from kpubdata.core.models import DatasetRef
from kpubdata.exceptions import (
    AuthError,
    ConfigError,
    ProviderResponseError,
    PublicDataError,
    TransportError,
    TransportTimeoutError,
)

from ..credentials import CredentialMetadata, CredentialRepository
from ..stages.bronze.build import SourceClient

logger = logging.getLogger("kpubdata_builder.service.providers")

#: When set, a requester with no stored credential of their own is refused rather than
#: served with the operator's. Unset by default, so a single-user deployment keeps
#: working without per-user setup. Same shape as REQUIRE_OWN_PUBLISH_CREDENTIAL (#687):
#: two switches for the same reasoning that behaved differently would be worse than one.
_REQUIRE_OWN_PROVIDER_CREDENTIAL_ENV = "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL"


class ProviderCredentialRequired(Exception):
    """The requester has no credential of their own and the operator's may not be used.

    Raised before a client is made, so the refusal is an answer (403) rather than an
    upstream authentication error — and so no path reaches the operator key (#786).
    """

    def __init__(self, providers: Iterable[str]) -> None:
        self.providers = tuple(sorted(providers))
        super().__init__("your own credential is required for: " + ", ".join(self.providers))


def require_own_provider_credential() -> bool:
    """Public name of the switch, for callers outside this module (#786)."""
    return _require_own_provider_credential()


def _require_own_provider_credential() -> bool:
    """Whether the operator credential fallback is switched off.

    Read at call time rather than cached: the value is a policy, and an operator who
    changes it should not have to restart to find out whether it took.
    """
    return os.environ.get(_REQUIRE_OWN_PROVIDER_CREDENTIAL_ENV, "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


CredentialSource = Literal["user", "server", "none", "refused"]
ProviderState = Literal["connected", "failed", "not_configured", "not_testable"]
ProviderErrorCategory = Literal["auth", "network", "timeout", "provider", "unknown"]

# kpubdata uses the same data.go.kr key slot as this Provider name. In Builder,
# we save the API key by the provider name in the request path, but when creating
# a per-request Client we convert it to the public provider_keys slot.
_CLIENT_KEY_SLOT: dict[str, str] = {
    "localdata": "datago",
    "lofin": "datago",
    "semas": "datago",
}


@dataclass(frozen=True)
class ResolvedCredential:
    """Interpreted credential. Not used as an API response model."""

    source: CredentialSource
    # Kept out of repr: a repr reaches logs, exception messages and test output (#686).
    value: str | None = field(repr=False)


@dataclass(frozen=True)
class ProviderDescriptor:
    """Runtime Provider metadata."""

    name: str
    requires_credential: bool


@dataclass(frozen=True)
class RuntimeProviderCatalog:
    """Isolated parsed single runtime Provider and its dataset list."""

    descriptor: ProviderDescriptor
    datasets: tuple[DatasetRef, ...]


@dataclass(frozen=True)
class ProviderTestResult:
    """Connection test result without raw exceptions/credentials."""

    provider: str
    status: ProviderState
    configured: bool
    latency_ms: int
    checked_at: str
    error_category: ProviderErrorCategory | None = None
    response_code: int | None = None
    dataset: str | None = None


class ProviderNotTestable(Exception):
    """No dataset of the provider can be called without guessing a parameter (#842)."""


class ProviderCredentialConflictError(ValueError):
    """Different user credentials required for one Client key slot."""


class ProviderTestOperation(Protocol):
    """Injected lightweight connection test operation."""

    def __call__(self, client: SourceClient, provider: str) -> str | None:
        """Run the test; return the dataset id it called, when there is one."""
        ...


class CredentialResolver:
    """Unify order: user credential > server default > not configured."""

    def __init__(self, repository: CredentialRepository | None) -> None:
        self._repository = repository

    @property
    def repository(self) -> CredentialRepository | None:
        return self._repository

    @staticmethod
    def client_key_slot(provider: str) -> str:
        return _CLIENT_KEY_SLOT.get(provider, provider)

    def resolve(self, owner_id: str | None, provider: str) -> ResolvedCredential:
        """Find the credential to call ``provider`` with, for this requester.

        The fallback to the operator's key is what makes a single-user deployment work
        without per-user setup, and what makes a shared one spend the operator's quota
        on other people's queries. Which of those it is depends on the deployment, so it
        is a switch rather than a decision taken here (F-07).

        A requester with no ``owner_id`` at all — an unauthenticated call in dev mode —
        is not refused by the switch. There is no owner to look a credential up for, so
        refusing would break dev mode without protecting anyone; ``ENFORCE_OWNERSHIP``
        is the switch that closes that door.
        """
        if self._repository is not None and owner_id is not None:
            user_value = self._repository.get_secret(owner_id, provider)
            if user_value is not None:
                return ResolvedCredential("user", user_value)
            if _require_own_provider_credential():
                logger.info(
                    "refusing the operator credential for %s: the requester has none of "
                    "their own and REQUIRE_OWN_PROVIDER_CREDENTIAL is set",
                    provider,
                )
                # "refused", not "none": the caller must stop, not carry on keyless —
                # a keyless client falls back to the environment by itself (#786).
                return ResolvedCredential("refused", None)
        server_value = KPubDataConfig.from_env().get_provider_key(self.client_key_slot(provider))
        if server_value:
            return ResolvedCredential("server", server_value)
        return ResolvedCredential("none", None)

    def provider_keys(self, owner_id: str | None, providers: Iterable[str]) -> dict[str, str]:
        """Create new Client key mapping from only the providers needed for the request.

        Raises:
            ProviderCredentialRequired: A provider the request needs resolved to
                ``refused`` (#786). Checked for every provider first, so the message
                names them all.
            ProviderCredentialConflictError: Two providers sharing a key slot resolved
                to different keys.
        """
        resolved: dict[str, str] = {}
        credentials = {provider: self.resolve(owner_id, provider) for provider in providers}
        refused = [p for p, c in credentials.items() if c.source == "refused"]
        if refused:
            raise ProviderCredentialRequired(refused)
        for provider, credential in credentials.items():
            if credential.value is None:
                continue
            slot = self.client_key_slot(provider)
            previous = resolved.get(slot)
            if previous is not None and previous != credential.value:
                raise ProviderCredentialConflictError(
                    f"providers sharing credential slot {slot!r} have conflicting credentials"
                )
            resolved[slot] = credential.value
        return resolved

    def metadata(self, owner_id: str, provider: str) -> CredentialMetadata:
        if self._repository is None:
            return CredentialMetadata(provider, False, None, None)
        return self._repository.get_metadata(owner_id, provider)


def runtime_provider_catalog(client: SourceClient) -> tuple[RuntimeProviderCatalog, ...]:
    """Parse provider catalogs, isolating only explicitly declared optional dependencies."""
    typed_client = cast(Client, client)
    # Built-in adapters are registered lazily.  Resolve them one at a time so an
    # optional dependency of one adapter (for example KRX -> pandas) cannot make
    # the catalog for every other provider unavailable.
    registry = getattr(typed_client, "_registry", None)
    if registry is None:
        authenticated = frozenset(p.name for p in typed_client.iter_authenticated_providers())
        grouped: dict[str, list[DatasetRef]] = {}
        for dataset in typed_client.datasets.list():
            grouped.setdefault(dataset.provider, []).append(dataset)
        return tuple(
            RuntimeProviderCatalog(
                ProviderDescriptor(name, name in authenticated), tuple(grouped[name])
            )
            for name in sorted(grouped)
        )

    catalogs: list[RuntimeProviderCatalog] = []
    for name in sorted(registry):
        try:
            adapter = registry.get(name)
            datasets = typed_client.datasets.list(provider=name)
        except ModuleNotFoundError as exc:
            # KRX is the sole built-in provider with this optional dependency.
            # Do not hide a missing internal module or another provider bug.
            if name == "krx" and exc.name == "pandas":
                continue
            raise
        catalogs.append(
            RuntimeProviderCatalog(
                ProviderDescriptor(name, bool(getattr(adapter, "requires_api_key", True))),
                tuple(datasets),
            )
        )
    return tuple(catalogs)


def provider_descriptors(client: SourceClient) -> tuple[ProviderDescriptor, ...]:
    """Get Provider list and authentication requirements from kpubdata runtime catalog."""
    return tuple(item.descriptor for item in runtime_provider_catalog(client))


def select_test_target(
    refs: Iterable[DatasetRef], provider: str
) -> tuple[str, dict[str, object]] | None:
    """A dataset and parameters a connection test can call without guessing (#842).

    The first LIST dataset of the provider, by id, whose declared request parameters
    give an example for every required one, none of them a date (an example date goes
    stale and the call then fails for a reason that is not the key), and that does not
    declare a per-dataset application. A dataset that declares no parameters at all
    is unknown, not parameter-free, so it is not chosen. None when nothing qualifies:
    calling the first dataset anyway made a valid key look broken whenever that
    dataset needed a parameter or an application.
    """
    candidates: list[tuple[str, dict[str, object]]] = []
    for ref in refs:
        if ref.provider != provider or Operation.LIST not in ref.operations:
            continue
        application = ref.raw_metadata.get("application")
        if isinstance(application, Mapping) and application.get("required") is not False:
            continue
        declared = ref.raw_metadata.get("request_parameters")
        if not isinstance(declared, Sequence) or isinstance(declared, str) or not declared:
            continue
        params: dict[str, object] = {}
        for item in declared:
            if not isinstance(item, Mapping) or not item.get("required"):
                continue
            example = item.get("example")
            if example in (None, "") or "date" in str(item.get("type") or ""):
                break
            params[str(item["name"])] = example
        else:
            candidates.append((ref.id, params))
    return min(candidates, default=None, key=lambda c: c[0])


def default_provider_test(client: SourceClient, provider: str) -> str | None:
    """Fetch one row of a dataset chosen by ``select_test_target``; return its id."""
    typed_client = cast(Client, client)
    refs = [ref for ref in typed_client.datasets.list() if ref.provider == provider]
    if not refs:
        raise ValueError("unknown provider")
    target = select_test_target(refs, provider)
    if target is None:
        raise ProviderNotTestable(provider)
    dataset_id, params = target
    _ = typed_client.dataset(dataset_id).list(page=1, page_size=1, **params)
    return dataset_id


def run_provider_test(
    *,
    provider: str,
    configured: bool,
    client: SourceClient | None,
    operation: ProviderTestOperation = default_provider_test,
) -> ProviderTestResult:
    """Run connection test and convert to stable secret-free result categories."""
    started = time.perf_counter()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not configured or client is None:
        return ProviderTestResult(provider, "not_configured", False, 0, checked_at)
    try:
        dataset = operation(client, provider)
    except ProviderNotTestable:
        return ProviderTestResult(provider, "not_testable", True, 0, checked_at)
    except Exception as exc:
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        category = categorize_provider_error(exc)
        response_code = reliable_response_code(exc)
        return ProviderTestResult(
            provider,
            "failed",
            True,
            latency_ms,
            checked_at,
            error_category=category,
            response_code=response_code,
        )
    latency_ms = max(0, round((time.perf_counter() - started) * 1000))
    return ProviderTestResult(
        provider, "connected", True, latency_ms, checked_at, dataset=dataset or None
    )


def categorize_provider_error(exc: Exception) -> ProviderErrorCategory:
    """Map kpubdata/stdlib exceptions to Issue #492 error categories."""
    if isinstance(exc, (TransportTimeoutError, TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, (AuthError, ConfigError)):
        return "auth"
    if isinstance(exc, TransportError):
        return "network"
    if isinstance(exc, (ProviderResponseError, PublicDataError)):
        return "provider"
    return "unknown"


def reliable_response_code(exc: Exception) -> int | None:
    """Return only HTTP status codes that kpubdata structurally provides."""
    if not isinstance(exc, PublicDataError):
        return None
    status_code = exc.status_code
    return status_code if isinstance(status_code, int) and 100 <= status_code <= 599 else None


def test_result_body(result: ProviderTestResult) -> dict[str, object]:
    """Wire body including optional fields only if successfully obtained."""
    body: dict[str, object] = {
        "provider": result.provider,
        "status": result.status,
        "configured": result.configured,
        "latency_ms": result.latency_ms,
        "checked_at": result.checked_at,
    }
    if result.error_category is not None:
        body["error_category"] = result.error_category
    if result.response_code is not None:
        body["response_code"] = result.response_code
    if result.dataset is not None:
        body["dataset"] = result.dataset
    return body


__all__ = [
    "CredentialResolver",
    "ProviderCredentialConflictError",
    "ProviderCredentialRequired",
    "ProviderDescriptor",
    "ProviderNotTestable",
    "RuntimeProviderCatalog",
    "ProviderTestOperation",
    "ProviderTestResult",
    "categorize_provider_error",
    "default_provider_test",
    "provider_descriptors",
    "require_own_provider_credential",
    "select_test_target",
    "runtime_provider_catalog",
    "reliable_response_code",
    "run_provider_test",
    "test_result_body",
]

"""Provider domain service (#596 first piece).

This is the first step of dividing the structure where ``BuilderService`` held all of
providers/uploads/query/builds/datasets/quality in one class into domain-separated modules.
The patterns established here are followed by remaining domains.

Two core rules:
    - **Only receive self-owned dependencies.** Do not inject the entire ``BuilderService`` —
      that only adds classes without reducing coupling. The provider domain actually uses
      only three things: credential resolver, client factory, provider test configuration.
    - **Wire contract is unchanged.** Return types (``ServiceResponse``), status codes, and
      body keys do not change. Routing and auth gates are not touched.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import cast

from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.providers import (
    CredentialResolver,
    ProviderDescriptor,
    ProviderTestOperation,
    provider_descriptors,
    run_provider_test,
    test_result_body,
)
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.build import SourceClient

# Factory that receives principal/providers/timeout to create per-request clients.
# Signature follows BuilderService._create_client directly.
CreateClient = Callable[..., SourceClient]
CloseClient = Callable[[SourceClient | None], None]


def _raise_provider_test_error(client: SourceClient, provider: str) -> None:
    """Do not expose raw exceptions even if Client creation itself fails."""
    del client, provider
    raise RuntimeError("provider test unavailable")


class ProvidersService:
    """Provider list, connection test, credential CRUD."""

    def __init__(
        self,
        *,
        credential_resolver: CredentialResolver,
        create_client: CreateClient,
        close_client: CloseClient,
        provider_test_operation: ProviderTestOperation,
        provider_test_timeout: float,
    ) -> None:
        self._credential_resolver = credential_resolver
        self._create_client = create_client
        self._close_client = close_client
        self._provider_test_operation = provider_test_operation
        self._provider_test_timeout = provider_test_timeout

    # --- Internal queries -------------------------------------------------

    def runtime_providers(self) -> tuple[ProviderDescriptor, ...] | ServiceResponse:
        """Runtime provider catalog. Query failure converts to 502."""
        client = self._create_client()
        try:
            return provider_descriptors(client)
        except Exception:
            # upstream client exception strings may contain request URLs, and
            # data.go.kr variants ship API keys as query parameters — returning
            # them as-is in responses leaks others' keys in error messages.
            _logger_exception("provider catalog unavailable")
            return ServiceResponse(502, {"error": "catalog unavailable"})
        finally:
            self._close_client(client)

    def known_provider(self, provider: str) -> ProviderDescriptor | ServiceResponse:
        providers = self.runtime_providers()
        if isinstance(providers, ServiceResponse):
            return providers
        match = next((item for item in providers if item.name == provider), None)
        if match is None:
            return ServiceResponse(404, {"error": "provider not found"})
        return match

    # --- Public endpoints ---------------------------------------------------

    def providers(self, *, principal: Principal) -> ServiceResponse:
        """Return runtime Provider list and current principal's configured status."""
        descriptors = self.runtime_providers()
        if isinstance(descriptors, ServiceResponse):
            return descriptors
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        items: list[JsonValue] = []
        for descriptor in descriptors:
            resolved = self._credential_resolver.resolve(principal.owner_id, descriptor.name)
            configured = not descriptor.requires_credential or resolved.value is not None
            items.append(
                {
                    "provider": descriptor.name,
                    "requires_credential": descriptor.requires_credential,
                    "configured": configured,
                }
            )
        return ServiceResponse(200, {"providers": items})

    def provider_status(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Perform lightweight connection test with current principal credential."""
        descriptor = self.known_provider(provider)
        if isinstance(descriptor, ServiceResponse):
            return descriptor
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        resolved = self._credential_resolver.resolve(principal.owner_id, provider)
        configured = not descriptor.requires_credential or resolved.value is not None
        client: SourceClient | None = None
        try:
            if configured:
                client = self._create_client(
                    principal, providers=(provider,), timeout=self._provider_test_timeout
                )
            result = run_provider_test(
                provider=provider,
                configured=configured,
                client=client,
                operation=self._provider_test_operation,
            )
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        except Exception:
            # Even if Client creation fails, don't expose raw exceptions - limit to unknown.
            result = run_provider_test(
                provider=provider,
                configured=True,
                client=cast(SourceClient, object()),
                operation=_raise_provider_test_error,
            )
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        finally:
            if client is not None:
                self._close_client(client)

    def provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Return current principal's stored credential metadata without raw value."""
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        repository = self._credential_resolver.repository
        if repository is None:
            return ServiceResponse(503, {"error": "credential store is not configured"})
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        metadata = repository.get_metadata(principal.owner_id, provider)
        return ServiceResponse(
            200,
            {
                "configured": metadata.configured,
                "masked": metadata.masked,
                "updated_at": metadata.updated_at,
            },
        )

    def put_provider_credential(
        self,
        provider: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """Create or replace current principal's Provider credential."""
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        repository = self._credential_resolver.repository
        if repository is None:
            return ServiceResponse(503, {"error": "credential store is not configured"})
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        if body is None or set(body) != {"credential"}:
            return ServiceResponse(400, {"error": "body must contain only 'credential'"})
        credential = body.get("credential")
        if not isinstance(credential, str) or not credential.strip():
            return ServiceResponse(400, {"error": "credential must be a non-empty string"})
        metadata = repository.put(principal.owner_id, provider, credential)
        return ServiceResponse(
            200,
            {
                "provider": metadata.provider,
                "configured": metadata.configured,
                "masked": metadata.masked,
                "updated_at": metadata.updated_at,
            },
        )

    def delete_provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Delete only current principal's Provider credential."""
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        repository = self._credential_resolver.repository
        if repository is None:
            return ServiceResponse(503, {"error": "credential store is not configured"})
        if principal.owner_id is None:
            return ServiceResponse(403, {"error": "stable principal is required"})
        _ = repository.delete(principal.owner_id, provider)
        return ServiceResponse(
            200,
            {"provider": provider, "configured": False, "masked": None, "updated_at": None},
        )

    def provider_keys(self, owner_id: str | None, providers: Iterable[str]) -> Mapping[str, str]:
        """Resolve per-principal provider keys used by build execution (thin delegation)."""
        return self._credential_resolver.provider_keys(owner_id, tuple(providers))


def _logger_exception(message: str) -> None:
    import logging

    logging.getLogger("kpubdata_builder.service").exception(message)


__all__ = ["ProvidersService"]

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

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from typing import cast

from kpubdata_builder.service import provider_probe, request_credentials
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import multi_user_mode
from kpubdata_builder.service.provider_tests import ProviderTestLog
from kpubdata_builder.service.providers import (
    CredentialResolver,
    ProviderDescriptor,
    ProviderTestOperation,
    ProviderTestResult,
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
        test_log: Callable[[], ProviderTestLog | None] = lambda: None,
        open_probe: provider_probe.OpenProbe = provider_probe.open_kpubdata_probe,
        probe_datasets: provider_probe.ListDatasets = provider_probe.spec_dataset_ids,
    ) -> None:
        """Args:
        test_log: Where each principal's last test per provider is kept (#842); None
            keeps nothing and reports every ``last_test`` as null.
        """
        self._credential_resolver = credential_resolver
        self._create_client = create_client
        self._close_client = close_client
        self._provider_test_operation = provider_test_operation
        self._provider_test_timeout = provider_test_timeout
        self._test_log = test_log
        self._open_probe = open_probe
        self._probe_datasets = probe_datasets

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
        log = self._test_log()
        last_tests = log.last_tests(principal.owner_id) if log is not None else {}
        items: list[JsonValue] = []
        for descriptor in descriptors:
            resolved = self._credential_resolver.resolve(principal.owner_id, descriptor.name)
            configured = not descriptor.requires_credential or resolved.value is not None
            items.append(
                {
                    "provider": descriptor.name,
                    "requires_credential": descriptor.requires_credential,
                    "configured": configured,
                    "last_test": cast(JsonValue, last_tests.get(descriptor.name)),
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
            self._remember(principal.owner_id, result)
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        except Exception:
            # Even if Client creation fails, don't expose raw exceptions - limit to unknown.
            result = run_provider_test(
                provider=provider,
                configured=True,
                client=cast(SourceClient, object()),
                operation=_raise_provider_test_error,
            )
            self._remember(principal.owner_id, result)
            return ServiceResponse(200, cast(dict[str, JsonValue], test_result_body(result)))
        finally:
            if client is not None:
                self._close_client(client)

    def probe_provider(
        self,
        provider: str,
        body: Mapping[str, JsonValue] | None,
        *,
        principal: Principal,
    ) -> ServiceResponse:
        """Say what the key this request carries can reach, per dataset (#802).

        Uses the ``X-Provider-Key`` header's key and no other; stores nothing.
        """
        del principal  # any authenticated caller; nothing is read or written for them
        descriptor = self.known_provider(provider)
        if isinstance(descriptor, ServiceResponse):
            return descriptor
        parsed = provider_probe.parse_probe_body(body)
        if isinstance(parsed, str):
            return ServiceResponse(400, {"error": parsed, "code": "invalid_request"})
        key = request_credentials.current_key(provider)
        if key is None:
            return ServiceResponse(
                400,
                {
                    "error": "send the key to probe in the X-Provider-Key header "
                    f"('{provider}=<key>'); a stored key is not used",
                    "code": "provider_key_required",
                },
            )
        available = list(self._probe_datasets(provider))
        if parsed.datasets is None:
            dataset_ids = available
        else:
            dataset_ids = [f"{provider}.{name}" for name in parsed.datasets]
            unknown = sorted(set(dataset_ids) - set(available))
            if unknown:
                names = ", ".join(item.removeprefix(f"{provider}.") for item in unknown)
                return ServiceResponse(
                    400,
                    {"error": f"cannot probe: {names}", "code": "invalid_request"},
                )
        try:
            result = provider_probe.run_probe(
                provider, key, dataset_ids, open_probe=self._open_probe
            )
        except Exception:
            # An exception's text may hold the request URL, and with it the key.
            _logger_exception("provider probe failed")
            return ServiceResponse(502, {"error": "probe unavailable"})
        return ServiceResponse(200, result)

    def _remember(self, owner_id: str, result: ProviderTestResult) -> None:
        """Keep the result as the last test; a store failure never fails the test."""
        log = self._test_log()
        if log is None:
            return
        try:
            log.record(owner_id, result)
        except sqlite3.Error:
            _logger_exception("could not record the provider test result")

    def provider_credential(self, provider: str, *, principal: Principal) -> ServiceResponse:
        """Return current principal's stored credential metadata without raw value.

        In a multi-user deployment nothing is stored or used (#683), so the answer is
        always "not configured" — even for a key saved before the deployment changed.
        """
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        if multi_user_mode():
            return ServiceResponse(200, {"configured": False, "masked": None, "updated_at": None})
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
        """Create or replace current principal's Provider credential.

        Refused in a multi-user deployment (#683, ADR 0012 amendment of 2026-09-30):
        there a key exists only while a request or job runs, sent in the
        ``X-Provider-Key`` header, and is never stored.
        """
        known = self.known_provider(provider)
        if isinstance(known, ServiceResponse):
            return known
        if multi_user_mode():
            return ServiceResponse(
                403,
                {
                    "error": "this deployment does not store provider keys; send the key "
                    "with each request in the X-Provider-Key header",
                    "code": "credential_storage_disabled",
                },
            )
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

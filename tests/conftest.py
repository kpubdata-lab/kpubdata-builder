"""Shared test configuration."""

import os

import pytest


@pytest.fixture(autouse=True)
def dev_mode_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set dev-mode in all tests to skip authentication (#321, ADR 0006).

    To validate authentication behavior in individual tests, override this fixture
    or explicitly delete/set environment variables.
    """
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")


@pytest.fixture(autouse=True)
def hermetic_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Block leaks of provider API keys from developer shell into tests (#577).

    CredentialResolver reads KPUBDATA_<PROVIDER>_API_KEY environment variables
    (e.g., KPUBDATA_DATAGO_API_KEY) as server defaults when user credentials
    are absent (kpubdata KPubDataConfig.from_env). If set in the local devbox,
    dev-mode principal's provider_keys becomes non-empty, causing simple
    client_factory (lambda: client) to reject provider_keys kwarg, raising
    RuntimeError → 502 → run directory not created → subsequent FileNotFoundError:
    manifest.json chain reaction. CI runners lack these keys and pass, creating
    divergence between local and CI results. To make tests hermetic regardless
    of environment, remove all KPUBDATA_*_API_KEY here. Tests requiring specific
    keys should explicitly set them via monkeypatch.setenv.
    """
    for env_name in list(os.environ):
        if env_name.startswith("KPUBDATA_") and env_name.endswith("_API_KEY"):
            monkeypatch.delenv(env_name, raising=False)

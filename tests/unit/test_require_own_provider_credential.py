"""The query path fell back to the operator's key without saying so (F-07).

`REQUIRE_OWN_PUBLISH_CREDENTIAL` closed this door for publishing (#687). The read path
had no equivalent, so in a shared deployment one person's queries spent the operator's
quota and used the operator's identity against the provider.
"""

from __future__ import annotations

import pytest

from kpubdata_builder.service.providers import CredentialResolver

ENV = "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL"
ALICE = "oidc:issuer|alice"


class _Repository:
    """A credential store holding whatever the test puts in it."""

    def __init__(self, secrets: dict[tuple[str, str], str] | None = None) -> None:
        self._secrets = secrets or {}

    def get_secret(self, owner_id: str, slot: str) -> str | None:
        return self._secrets.get((owner_id, slot))


@pytest.fixture(autouse=True)
def _clear_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch is off unless a test turns it on."""
    monkeypatch.delenv(ENV, raising=False)


@pytest.fixture(autouse=True)
def _operator_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """An operator key exists, which is the condition that makes the fallback visible."""
    monkeypatch.setenv("KPUBDATA_DATAGO_API_KEY", "operator-key")


def test_a_requester_with_their_own_key_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case the switch must not change."""
    resolver = CredentialResolver(_Repository({(ALICE, "datago"): "alice-key"}))

    resolved = resolver.resolve(ALICE, "datago")

    assert resolved.source == "user"
    assert resolved.value == "alice-key"


def test_the_operator_key_is_served_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unchanged default: a single-user deployment works without per-user setup.

    Recorded rather than assumed, because the switch exists to change exactly this and
    a silent change of default would break every existing deployment.
    """
    resolver = CredentialResolver(_Repository())

    resolved = resolver.resolve(ALICE, "datago")

    assert resolved.source == "server"
    assert resolved.value == "operator-key"


def test_the_switch_refuses_the_operator_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch on, a requester with no key of their own gets none."""
    monkeypatch.setenv(ENV, "1")
    resolver = CredentialResolver(_Repository())

    resolved = resolver.resolve(ALICE, "datago")

    assert resolved.source == "none"
    assert resolved.value is None


def test_the_switch_does_not_affect_a_requester_with_their_own_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turning it on must not break the people who did set a key up."""
    monkeypatch.setenv(ENV, "1")
    resolver = CredentialResolver(_Repository({(ALICE, "datago"): "alice-key"}))

    assert resolver.resolve(ALICE, "datago").value == "alice-key"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_the_switch_accepts_the_usual_spellings(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """An operator who writes "true" must not silently get the old behaviour."""
    monkeypatch.setenv(ENV, value)
    resolver = CredentialResolver(_Repository())

    assert resolver.resolve(ALICE, "datago").source == "none"


@pytest.mark.parametrize("value", ["0", "false", "no", "", "  "])
def test_anything_else_leaves_the_fallback_alone(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A typo must not turn a policy on by accident either."""
    monkeypatch.setenv(ENV, value)
    resolver = CredentialResolver(_Repository())

    assert resolver.resolve(ALICE, "datago").source == "server"


def test_an_unauthenticated_call_is_not_refused_by_this_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No owner means no credential to look up, so there is nothing to refuse.

    Refusing here would break dev mode without protecting anyone —
    ``ENFORCE_OWNERSHIP`` is the switch that closes that door.
    """
    monkeypatch.setenv(ENV, "1")
    resolver = CredentialResolver(_Repository())

    assert resolver.resolve(None, "datago").source == "server"


def test_no_credential_anywhere_is_still_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the switch off and nothing configured, the answer is unchanged."""
    monkeypatch.delenv("KPUBDATA_DATAGO_API_KEY", raising=False)
    resolver = CredentialResolver(_Repository())

    assert resolver.resolve(ALICE, "datago").source == "none"

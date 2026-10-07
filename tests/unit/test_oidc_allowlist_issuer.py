"""An allowlist entry admits a name from the issuer it is written for (#1074).

``OIDC_ISSUER`` takes several issuers. The allowlists compared a subject, an e-mail or an
``hd`` alone, so an entry meant for an account at one issuer admitted whoever held the
same name at another — while ``owner_id`` and the administrator list already bind both.
"""

from __future__ import annotations

import pytest

from kpubdata_builder.service.auth import Principal, authenticate, validate_oidc_config

from .test_oidc_auth import (
    _ISSUER,
    _clean_auth_env,  # noqa: F401 - a fixture
    _make_token,
    oidc_env,  # noqa: F401 - a fixture
    rsa_keypair,  # noqa: F401 - a fixture
)

_OTHER = "https://other-issuer.example.com"
_SUB = "user-1234567890"
_EMAIL = "user@example.com"

pytestmark = pytest.mark.usefixtures("_clean_auth_env")


def _admitted(private_pem: bytes, **claims: object) -> bool:
    result = authenticate(bearer_token=f"Bearer {_make_token(private_pem, **claims)}")
    assert isinstance(result, Principal)
    return result.admitted


@pytest.fixture()
def two_issuers(
    oidc_env: bytes,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> bytes:
    """Both issuers accepted, and no list yet. The one JWKS serves both: what differs
    between the tokens below is only the issuer they state."""
    monkeypatch.setenv("OIDC_ISSUER", f"{_ISSUER},{_OTHER}")
    monkeypatch.delenv("OIDC_ALLOWED_EMAILS", raising=False)
    return oidc_env


@pytest.mark.parametrize(
    ("variable", "value", "claims"),
    [
        ("OIDC_ALLOWED_SUBJECTS", _SUB, {}),
        ("OIDC_ALLOWED_EMAILS", _EMAIL, {}),
        ("OIDC_ALLOWED_HD", "example.com", {"hd": "example.com"}),
    ],
)
def test_an_entry_admits_its_own_issuer_and_not_the_same_name_from_another(
    two_issuers: bytes,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    claims: dict[str, object],
) -> None:
    monkeypatch.setenv(variable, f"{_ISSUER}|{value}")

    assert _admitted(two_issuers, **claims) is True
    # The same subject, e-mail and domain, stated by the other issuer.
    assert _admitted(two_issuers, iss=_OTHER, **claims) is False


def test_the_two_issuers_same_subject_are_two_owners(two_issuers: bytes) -> None:
    first = authenticate(bearer_token=f"Bearer {_make_token(two_issuers)}")
    second = authenticate(bearer_token=f"Bearer {_make_token(two_issuers, iss=_OTHER)}")

    assert isinstance(first, Principal) and isinstance(second, Principal)
    assert first.owner_id != second.owner_id


def test_each_issuer_can_be_listed_for_the_same_name(
    two_issuers: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", f"{_ISSUER}|{_EMAIL}, {_OTHER}|{_EMAIL}")

    assert _admitted(two_issuers) is True
    assert _admitted(two_issuers, iss=_OTHER) is True


def test_with_several_issuers_an_entry_naming_none_admits_nobody(
    two_issuers: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup refuses this configuration; a process that got past it still fails closed."""
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", _EMAIL)
    monkeypatch.setenv("OIDC_ALLOWED_SUBJECTS", _SUB)

    assert _admitted(two_issuers) is False
    assert _admitted(two_issuers, iss=_OTHER) is False


def test_with_one_issuer_an_entry_naming_none_is_that_issuers(
    oidc_env: bytes,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative: every deployment so far has one issuer and plain entries."""
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", _EMAIL)
    assert _admitted(oidc_env) is True

    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", f"{_ISSUER}|{_EMAIL}")
    assert _admitted(oidc_env) is True

    validate_oidc_config()


def test_a_subject_that_holds_a_bar_is_not_split_into_an_issuer(
    oidc_env: bytes,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some IdPs write subjects as ``<connection>|<id>``."""
    monkeypatch.delenv("OIDC_ALLOWED_EMAILS", raising=False)
    monkeypatch.setenv("OIDC_ALLOWED_SUBJECTS", "google-oauth2|1234567890")

    assert _admitted(oidc_env, sub="google-oauth2|1234567890") is True
    assert _admitted(oidc_env, sub="1234567890") is False


def test_an_entry_for_an_issuer_that_is_not_configured_admits_nobody(
    oidc_env: bytes,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", f"{_OTHER}|{_EMAIL}")

    assert _admitted(oidc_env) is False


# ----------------------------------------------------------------------- startup


def test_startup_refuses_several_issuers_with_an_entry_naming_none(
    two_issuers: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", f"{_ISSUER}|{_EMAIL},victim@example.com")
    monkeypatch.setenv("OIDC_ALLOWED_SUBJECTS", f"{_OTHER}|{_SUB}")

    with pytest.raises(RuntimeError) as raised:
        validate_oidc_config()

    message = str(raised.value)
    assert "OIDC_ALLOWED_EMAILS" in message
    assert "OIDC_ALLOWED_SUBJECTS" not in message  # every entry there names its issuer
    assert "<issuer>|<value>" in message
    # The entries are people's addresses; the message names the variable only.
    assert "victim@example.com" not in message and _EMAIL not in message


def test_startup_accepts_several_issuers_when_every_entry_names_one(
    two_issuers: bytes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OIDC_ALLOWED_EMAILS", f"{_ISSUER}|{_EMAIL}")
    monkeypatch.setenv("OIDC_ALLOWED_HD", f"{_OTHER}|example.com")

    validate_oidc_config()

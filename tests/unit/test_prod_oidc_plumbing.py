"""The production image and compose can run OIDC (#992).

The default image had no ``pyjwt`` and the production compose passed no ``OIDC_*``
variable, so a deployment Studio could sign in to could not be configured at all. These
hold the plumbing; they do not start a container.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_OIDC = (
    "OIDC_ISSUER",
    "OIDC_AUDIENCE",
    "OIDC_ALLOWED_HD",
    "OIDC_ALLOWED_SUBJECTS",
    "OIDC_ALLOWED_EMAILS",
    "KPUBDATA_BUILDER_ADMIN_SUBJECTS",
    "KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT",
)


def test_the_default_image_installs_the_auth_extra() -> None:
    text = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r'^ARG EXTRAS="?([^"\n]*)"?$', text, re.MULTILINE)

    assert match
    assert "auth" in match.group(1).split()
    assert "publish" in match.group(1).split()


def test_every_image_variant_keeps_the_default_images_extras() -> None:
    """A build-arg replaces the Dockerfile's default whole, so a variant must repeat it.

    The CUBRID image was built with ``EXTRAS=publish cubrid``: no ``auth``, so the one
    image a CUBRID deployment can pull could not start with OIDC configured (#992).
    """
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    default = re.search(r'^ARG EXTRAS="?([^"\n]*)"?$', dockerfile, re.MULTILINE)
    assert default
    workflow = (_ROOT / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    variants = re.findall(r"^\s*EXTRAS=(.*)$", workflow, re.MULTILINE)

    assert variants
    missing = [
        (variant, extra)
        for variant in variants
        for extra in default.group(1).split()
        if extra not in variant.split()
    ]
    assert missing == []


def test_the_auth_extra_is_what_provides_pyjwt() -> None:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert re.search(r'^auth = \[\s*"pyjwt', text, re.MULTILINE)


@pytest.mark.parametrize("name", _OIDC)
def test_the_production_compose_passes_each_variable_through_and_defaults_to_unset(
    name: str,
) -> None:
    compose = yaml.safe_load((_ROOT / "docker-compose.prod.app.yml").read_text("utf-8"))
    environment = compose["services"]["builder"]["environment"]

    # ``${VAR:-}``: taken from .env when set, empty otherwise — and Builder reads an
    # empty value as unset, so a deployment that sets none stays API-key only.
    assert environment[name] == f"${{{name}:-}}"


def test_empty_values_leave_oidc_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from kpubdata_builder.service import auth, ownership

    for name in _OIDC:
        monkeypatch.setenv(name, "")

    assert auth.oidc_enabled() is False
    assert ownership.multi_user_mode() is False


def test_the_env_example_names_every_variable() -> None:
    text = (_ROOT / ".env.app.example").read_text(encoding="utf-8")

    assert [name for name in _OIDC if name not in text] == []

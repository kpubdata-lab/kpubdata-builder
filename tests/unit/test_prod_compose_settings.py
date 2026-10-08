"""Which settings the production compose passes to the container (#1108).

``docker-compose.prod.app.yml`` names each variable it hands the Builder container. A
setting that is not named there does nothing when an operator writes it in ``.env``: the
value never reaches the process, and nothing says so. This holds the list of what is
passed against the settings catalog, so a new setting is either passed or put on the
list of those that are not — and the deployment guide tells operators which those are.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from kpubdata_builder import settings_catalog as catalog
from kpubdata_builder.service.startup_settings import check_settings

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = _ROOT / "docker-compose.prod.app.yml"
_ENV_EXAMPLE = _ROOT / ".env.app.example"
_GUIDE = _ROOT / "docs" / "deploy.md"

#: Settings the production compose does not pass. Writing one of these in ``.env``
#: changes nothing until it is added to the compose file's ``environment``.
NOT_PASSED: frozenset[str] = frozenset(
    {
        # Skips authentication. Not something a production stack should be one line
        # of ``.env`` away from.
        "KPUBDATA_BUILDER_DEV_MODE",
        "KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS",
        "OIDC_JWKS_URL",
        "OIDC_JWKS_TTL",
        "ENFORCE_OWNERSHIP",
        "KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT",
        "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL",
        "KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS",
        "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL",
        "KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS",
        "KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS",
        "KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS",
        "KPUBDATA_BUILDER_CHECKPOINT_MAX_AGE_SECONDS",
        "KPUBDATA_BUILDER_MAX_UPLOAD_BYTES",
        "KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES",
        "KPUBDATA_BUILDER_STORAGE_BACKEND",
        "KPUBDATA_BUILDER_CUBRID_URL",
        "KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT",
        "HF_TOKEN",
        "KAGGLE_USERNAME",
        "KAGGLE_KEY",
    }
)

_SUBSTITUTION = re.compile(r"^\$\{(?P<name>[A-Z0-9_]+):-(?P<default>[^}]*)\}$")


def _environment() -> dict[str, Any]:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    return dict(compose["services"]["builder"]["environment"])


def _settings() -> set[str]:
    return {setting.name for setting in catalog.SETTINGS}


def test_every_setting_is_passed_or_listed_as_not_passed() -> None:
    """A new setting cannot go undecided: the stack passes it, or says it does not."""
    passed = set(_environment())

    assert passed.isdisjoint(NOT_PASSED)
    assert passed | NOT_PASSED == _settings()


def test_each_variable_is_passed_under_its_own_name() -> None:
    """``FOO: ${BAR:-}`` would hand one setting another's value."""
    for name, value in _environment().items():
        match = _SUBSTITUTION.match(str(value))
        if match:
            assert match["name"] == name


def test_a_variable_left_out_of_dotenv_reaches_the_process_as_one_it_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose passes ``${VAR:-default}`` whether or not ``.env`` sets VAR.

    Unset, the process gets the default — the empty string for most. Every reader has
    to take that as "not set": one that tried to parse it would stop the stack on a
    variable the operator never wrote.
    """
    for setting in catalog.SETTINGS:
        monkeypatch.delenv(setting.name, raising=False)
    for name, value in _environment().items():
        match = _SUBSTITUTION.match(str(value))
        monkeypatch.setenv(name, match["default"] if match else str(value))

    report = check_settings()

    assert report.problems == []
    assert report.warnings == []


def test_the_example_dotenv_names_only_what_the_stack_reads() -> None:
    """A line in the template that nothing passes on would be a setting that does nothing."""
    template = _ENV_EXAMPLE.read_text(encoding="utf-8")
    named = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", template, re.MULTILINE))
    substituted = set(re.findall(r"\$\{([A-Z0-9_]+)", _COMPOSE.read_text(encoding="utf-8")))

    assert named <= substituted


def test_the_guide_lists_the_settings_that_are_not_passed() -> None:
    guide = _GUIDE.read_text(encoding="utf-8")
    start = guide.index("### compose 가 컨테이너에 넘기지 않는 설정")
    section = guide[start : guide.index("\n### ", start + 1)]

    listed = set(re.findall(r"`([A-Z][A-Z0-9_]+)`", section)) & _settings()

    assert listed == NOT_PASSED

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

#: ``${VAR}``, ``${VAR:-default}``, ``${VAR-default}``, ``${VAR:?message}``, ``${VAR?message}``.
#: A default holds no ``$`` of its own: ``${FOO:-$BAR}`` would be another variable's value.
_BRACED = re.compile(r"^\$\{(?P<name>\w+)(?:(?P<op>:?[-?])(?P<default>[^}$]*))?\}$")
#: ``$VAR``, which compose reads as ``${VAR}``.
_BARE = re.compile(r"^\$(?P<name>[A-Za-z_]\w*)$")

#: A variable compose refuses to start without (``${VAR:?message}``).
REQUIRED = object()


def _builder() -> dict[str, object]:
    compose = yaml.safe_load(_COMPOSE.read_text(encoding="utf-8"))
    return dict(compose["services"]["builder"])


def _environment() -> dict[str, object]:
    """The variables the compose names for the container, by name.

    The file writes them as a mapping. A list (``- FOO=bar``) or an ``env_file`` would
    pass variables these tests do not read, so either is refused here rather than
    looked through.
    """
    builder = _builder()
    assert "env_file" not in builder, "an env_file passes variables this test cannot see"
    environment = builder["environment"]
    assert isinstance(environment, dict), "environment must be a mapping, not a list"
    return dict(environment)


def _literal(value: object) -> str:
    """A value compose passes as written, as the process receives it."""
    # YAML reads ``true`` and ``8000`` as a bool and an int; compose passes them as the
    # text that was written. ``str(True)`` is "True", which is not what was written.
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _substituted(value: object) -> tuple[str, object] | None:
    """``(name, what the process gets when .env does not set it)``, or None for a literal.

    What the process gets is the default, the empty string where there is none, or
    ``REQUIRED`` for ``${VAR:?…}`` — compose does not start without that one.

    Raises:
        AssertionError: The value is one this cannot follow: a ``$`` that is not the
            whole of the value, a default with a ``$`` in it, or no value at all.
    """
    # ``FOO:`` with nothing after it hands the container whatever the host has by that
    # name: a variable this file does not show.
    assert value is not None, "a variable with no value is passed through from the host"
    text = _literal(value)
    # ``$$`` is how a literal dollar is written; what is left of ``$`` is a substitution.
    if "$" not in text.replace("$$", ""):
        return None
    bare = _BARE.match(text)
    if bare:
        return bare["name"], ""
    match = _BRACED.match(text)
    assert match, f"a substitution this test does not understand: {text!r}"
    if match["op"] in (":?", "?"):
        return match["name"], REQUIRED
    return match["name"], match["default"] or ""


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
        substituted = _substituted(value)
        if substituted:
            assert substituted[0] == name


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
        substituted = _substituted(value)
        if substituted is None:
            monkeypatch.setenv(name, _literal(value))
        elif isinstance(substituted[1], str):
            monkeypatch.setenv(name, substituted[1])
        # A required variable has no value when it is left out: the stack does not
        # start, and there is no process to hand anything to.

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


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.0.0.0", None),
        (8000, None),
        (True, None),
        ("costs $$5", None),
        ("${FOO:-}", ("FOO", "")),
        ("${FOO:-128MB}", ("FOO", "128MB")),
        ("${FOO-x}", ("FOO", "x")),
        ("${FOO}", ("FOO", "")),
        ("$FOO", ("FOO", "")),
        ("${FOO:?must be set}", ("FOO", REQUIRED)),
        ("${FOO?x}", ("FOO", REQUIRED)),
    ],
)
def test_substitutions_are_read_as_compose_reads_them(
    value: object, expected: tuple[str, object] | None
) -> None:
    assert _substituted(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "prefix-${FOO}",
        "prefix-$FOO",
        "${FOO:+x}",
        "${FOO:-$BAR}",
        "${FOO:-${BAR}}",
        "$FOO$BAR",
        None,
    ],
)
def test_a_substitution_the_test_cannot_follow_is_refused(value: object) -> None:
    with pytest.raises(AssertionError):
        _substituted(value)


def test_a_literal_is_passed_as_it_was_written() -> None:
    """Not as Python spells what YAML made of it."""
    assert _literal(True) == "true"
    assert _literal(False) == "false"
    assert _literal(8000) == "8000"
    assert _literal("0.0.0.0") == "0.0.0.0"

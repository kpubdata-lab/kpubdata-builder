"""Which settings the production compose passes to the container (#1108).

``docker-compose.prod.app.yml`` names each variable it hands the Builder container. A
setting that is not named there does nothing when an operator writes it in ``.env``: the
value never reaches the process, and nothing says so. This holds the list of what is
passed against the settings catalog, so a new setting is either passed or put on the
list of those that are not — and the deployment guide tells operators which those are.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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

#: Settings some deployments are told to leave empty, so the stack must not require a
#: value of them. An OIDC-only deployment sets no API key (#1122), and a multi-user one
#: is told not to set the credential master key it does not use (#990). ``${VAR:?…}``
#: on one of these would stop exactly those deployments.
MAY_BE_EMPTY: frozenset[str] = frozenset(
    {
        "KPUBDATA_BUILDER_API_KEY",
        "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY",
    }
)

#: ``${VAR}``, ``${VAR:-default}``, ``${VAR-default}``, ``${VAR:?message}``, ``${VAR?message}``.
#: A default holds no ``$`` of its own: ``${FOO:-$BAR}`` would be another variable's value.
_BRACED = re.compile(r"^\$\{(?P<name>\w+)(?:(?P<op>:?[-?])(?P<default>[^}$]*))?\}$")
#: ``$VAR``, which compose reads as ``${VAR}``.
_BARE = re.compile(r"^\$(?P<name>[A-Za-z_]\w*)$")
#: Any variable a text names, in either form, once ``$$`` is out of the way.
_ANY_VARIABLE = re.compile(r"\$(?:\{(\w+)|([A-Za-z_]\w*))")


@dataclass(frozen=True)
class Substitution:
    """A value compose fills in from ``.env`` or the host."""

    name: str
    #: What the process gets when the variable is not set; None when it is required.
    default: str | None
    #: ``${VAR:?…}`` requires a value that is not empty; ``${VAR?…}`` only that it is set.
    required: Literal["non-empty", "set"] | None = None
    #: ``${VAR-x}`` uses the default only when VAR is unset: an empty ``VAR=`` stays empty.
    #: ``${VAR:-x}`` uses it for an empty one too.
    keeps_empty: bool = False


def _compose_text() -> str:
    return _COMPOSE.read_text(encoding="utf-8")


def _environment() -> dict[str, str | None]:
    """The variables the compose names for the container: each name and its text.

    Read from the YAML nodes, not from loaded values. Compose passes a plain scalar as
    the text that was written — ``yes``, ``on``, ``010`` — where PyYAML would make a
    bool or a number of it; and a variable written with no value is passed through
    from the host, which is ``None`` here.

    The file writes them as a mapping. A list (``- FOO=bar``) or an ``env_file`` would
    pass variables these tests do not read, so either is refused rather than looked
    through.
    """
    root = yaml.compose(_compose_text(), Loader=yaml.SafeLoader)
    builder = _child(_child(root, "services"), "builder")
    assert isinstance(builder, yaml.MappingNode)
    keys = {key.value for key, _ in builder.value}
    assert "env_file" not in keys, "an env_file passes variables this test cannot see"
    environment = _child(builder, "environment")
    assert isinstance(environment, yaml.MappingNode), "environment must be a mapping, not a list"
    values: dict[str, str | None] = {}
    for key, value in environment.value:
        assert isinstance(value, yaml.ScalarNode), f"{key.value}: not a single value"
        is_null = value.tag == "tag:yaml.org,2002:null" and value.style is None
        values[str(key.value)] = None if is_null else str(value.value)
    return values


def _child(node: yaml.Node | None, name: str) -> yaml.Node:
    assert isinstance(node, yaml.MappingNode), f"no mapping to look up {name!r} in"
    for key, value in node.value:
        if key.value == name:
            return value
    raise AssertionError(f"the compose file has no {name!r}")


def _literal(text: str) -> str:
    """A value compose passes as written, as the process receives it: ``$$`` is ``$``."""
    return text.replace("$$", "$")


def _substituted(value: str | None) -> Substitution | None:
    """How compose fills ``value`` in, or None when it is a literal.

    Raises:
        AssertionError: The value is one this cannot follow: a ``$`` that is not the
            whole of the value, a default with a ``$`` in it, or no value at all.
    """
    # ``FOO:`` with nothing after it hands the container whatever the host has by that
    # name: a variable this file does not show.
    assert value is not None, "a variable with no value is passed through from the host"
    # ``$$`` is how a literal dollar is written; what is left of ``$`` is a substitution.
    if "$" not in value.replace("$$", ""):
        return None
    bare = _BARE.match(value)
    if bare:
        return Substitution(bare["name"], "")
    match = _BRACED.match(value)
    assert match, f"a substitution this test does not understand: {value!r}"
    if match["op"] == ":?":
        return Substitution(match["name"], None, "non-empty")
    if match["op"] == "?":
        return Substitution(match["name"], None, "set")
    return Substitution(match["name"], match["default"] or "", keeps_empty=match["op"] == "-")


def _template() -> dict[str, str]:
    """The variables ``.env.app.example`` sets — the lines that are not commented out."""
    lines = _ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    pairs = (line.split("=", 1) for line in lines if re.match(r"^[A-Z][A-Z0-9_]*=", line))
    return {name: _dotenv_value(value) for name, value in pairs}


def _dotenv_value(text: str) -> str:
    """The value of a ``.env`` line as compose reads it: quotes off, a trailing comment off."""
    text = text.strip()
    if text[:1] in ("'", '"'):
        closing = text.find(text[0], 1)
        return text[1:closing] if closing > 0 else text[1:]
    # Unquoted, a ``#`` after whitespace starts a comment.
    return re.split(r"\s+#", text, maxsplit=1)[0].strip()


def _template_names() -> set[str]:
    """Every variable the template names, set or commented out."""
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    return set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))


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
        substitution = _substituted(value)
        if substitution:
            assert substitution.name == name


def test_a_required_variable_is_one_the_template_sets() -> None:
    """``${VAR:?…}`` stops the stack when VAR is missing, so the template must set it.

    Requiring a variable the template leaves commented out — an optional setting —
    would make the stack refuse to start for an operator who followed the template.

    The template cannot say everything, though: it sets an API key, and a comment
    beside it tells an OIDC-only deployment to leave it empty. So the settings that a
    supported deployment leaves empty are named in ``MAY_BE_EMPTY``, and ``:?`` — which
    refuses an empty value — is refused on them whatever the template holds.
    """
    template = _template()
    for name, value in _environment().items():
        substitution = _substituted(value)
        if substitution is None or substitution.required is None:
            continue
        if name in MAY_BE_EMPTY:
            assert substitution.required != "non-empty", (
                f"{name} is required to be non-empty, but some deployments leave it empty"
            )
        assert name in template, f"{name} is required but the template does not set it"
        if substitution.required == "non-empty":
            assert template[name], (
                f"{name} is required to be non-empty; the template leaves it empty"
            )


def test_a_variable_reaches_the_process_as_one_it_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compose hands the process a value for every variable it names.

    Left out of ``.env``, that is the default — the empty string for most. Every reader
    has to take that as "not set": one that tried to parse it would stop the stack on
    a variable the operator never wrote. A required variable has no default; it is
    given what the template sets, since the stack does not start without one — and
    what the template sets is a placeholder, so this says only that the placeholder is
    a value the reader takes, not that a real one would be.
    """
    template = _template()
    for setting in catalog.SETTINGS:
        monkeypatch.delenv(setting.name, raising=False)
    for name, value in _environment().items():
        substitution = _substituted(value)
        if substitution is None:
            assert value is not None
            monkeypatch.setenv(name, _literal(value))
        elif substitution.default is not None:
            # The template followed as it is: where it sets the variable to nothing and
            # the substitution keeps an empty value, the process gets nothing.
            kept_empty = substitution.keeps_empty and template.get(name) == ""
            monkeypatch.setenv(name, "" if kept_empty else substitution.default)
        elif name in MAY_BE_EMPTY:
            # Left empty, as the deployments this name is listed for leave it: what is
            # checked is that an empty one is accepted, not the template's placeholder.
            # The compose file does not come here today — both names have a ``:-``
            # default — but ``${VAR?…}`` on one of them would, and the test of that
            # form below runs this with such a file.
            monkeypatch.setenv(name, "")
        else:
            assert name in template, f"{name} is required but the template does not set it"
            monkeypatch.setenv(name, template[name])

    report = check_settings()

    assert report.problems == []
    assert report.warnings == []


def test_the_example_dotenv_names_only_what_the_stack_reads() -> None:
    """A line in the template that nothing passes on would be a setting that does nothing."""
    substituted: set[str] = set()
    for token in yaml.scan(_compose_text(), Loader=yaml.SafeLoader):
        # Scalars only: a ``$FOO`` in a comment is not read by anything.
        if isinstance(token, yaml.ScalarToken):
            text = str(token.value).replace("$$", "")
            substituted |= {braced or bare for braced, bare in _ANY_VARIABLE.findall(text)}

    assert _template_names() <= substituted


def test_the_guide_lists_the_settings_that_are_not_passed() -> None:
    guide = _GUIDE.read_text(encoding="utf-8")
    start = guide.index("### compose 가 컨테이너에 넘기지 않는 설정")
    section = guide[start : guide.index("\n### ", start + 1)]

    # The list items, one setting each; the prose around them names other variables.
    listed = set(re.findall(r"^- `([A-Z][A-Z0-9_]+)`", section, re.MULTILINE))

    assert listed == NOT_PASSED


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0.0.0.0", None),
        ("8000", None),
        ("true", None),
        ("costs $$5", None),
        ("${FOO:-}", Substitution("FOO", "")),
        ("${FOO:-128MB}", Substitution("FOO", "128MB")),
        ("${FOO-x}", Substitution("FOO", "x", keeps_empty=True)),
        ("${FOO}", Substitution("FOO", "")),
        ("$FOO", Substitution("FOO", "")),
        ("${FOO:?must be set}", Substitution("FOO", None, "non-empty")),
        ("${FOO?x}", Substitution("FOO", None, "set")),
    ],
)
def test_substitutions_are_read_as_compose_reads_them(
    value: str, expected: Substitution | None
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
def test_a_substitution_the_test_cannot_follow_is_refused(value: str | None) -> None:
    with pytest.raises(AssertionError):
        _substituted(value)


def test_a_literal_reaches_the_process_with_its_dollars_unescaped() -> None:
    assert _literal("costs $$5") == "costs $5"
    assert _literal("0.0.0.0") == "0.0.0.0"


def test_values_are_read_as_the_text_that_was_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not as PyYAML's idea of them: compose passes ``yes`` as ``yes``, not as a bool."""
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n"
        "  builder:\n"
        "    environment:\n"
        "      A: yes\n"
        "      B: on\n"
        "      C: 010\n"
        "      D: 1_000\n"
        '      E: "8000"\n'
        "      F: ''\n"
        "      G:\n"
        "      H: ~\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSE", compose)

    assert _environment() == {
        "A": "yes",
        "B": "on",
        "C": "010",
        "D": "1_000",
        "E": "8000",
        "F": "",
        "G": None,
        "H": None,
    }


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ("    environment:\n      - FOO=bar\n", "mapping"),
        ("    env_file: .env\n    environment:\n      FOO: bar\n", "env_file"),
    ],
)
def test_a_form_that_hides_variables_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str, message: str
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(f"services:\n  builder:\n{environment}", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSE", compose)

    with pytest.raises(AssertionError, match=message):
        _environment()


@pytest.mark.parametrize(
    ("line", "template", "accepted"),
    [
        # An optional setting the template leaves commented out.
        ("FOO: ${FOO:?needed}", "# FOO=\n", False),
        # Set, but empty: ``:?`` refuses it, ``?`` does not.
        ("FOO: ${FOO:?needed}", "FOO=\n", False),
        ("FOO: ${FOO?needed}", "FOO=\n", True),
        ("FOO: ${FOO:?needed}", "FOO=value\n", True),
        ("FOO: $FOO", "# FOO=\n", True),
    ],
)
def test_requiring_a_variable_the_template_does_not_set_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str, template: str, accepted: bool
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(f"services:\n  builder:\n    environment:\n      {line}\n", "utf-8")
    example = tmp_path / ".env.app.example"
    example.write_text(template, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSE", compose)
    monkeypatch.setattr(sys.modules[__name__], "_ENV_EXAMPLE", example)

    if accepted:
        test_a_required_variable_is_one_the_template_sets()
    else:
        with pytest.raises(AssertionError, match="FOO is required"):
            test_a_required_variable_is_one_the_template_sets()


@pytest.mark.parametrize("name", sorted(MAY_BE_EMPTY))
def test_requiring_a_value_of_a_setting_some_deployments_leave_empty_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """With the real template, which sets both: it is the name that refuses, not the value."""
    assert _template().get(name), f"the template is expected to set {name}"
    real = _compose_text()
    line = re.search(rf"^      {name}: .*$", real, re.MULTILINE)
    assert line, f"the compose file is expected to pass {name}"
    compose = tmp_path / "compose.yml"
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSE", compose)

    compose.write_text(real.replace(line[0], f"      {name}: ${{{name}:?needed}}"), "utf-8")
    with pytest.raises(AssertionError, match="some deployments leave it empty"):
        test_a_required_variable_is_one_the_template_sets()

    # Required to be set, but allowed to be empty, is another matter — and the value
    # the process is then checked with is the empty one, not the template's placeholder.
    compose.write_text(real.replace(line[0], f"      {name}: ${{{name}?needed}}"), "utf-8")
    test_a_required_variable_is_one_the_template_sets()
    test_a_variable_reaches_the_process_as_one_it_accepts(monkeypatch)
    assert os.environ[name] == ""


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ("plain", "plain"),
        ("  spaced  ", "spaced"),
        ('"quoted # not a comment"', "quoted # not a comment"),
        ("'single'", "single"),
        ("value # a comment", "value"),
        ("https://example.com/#fragment", "https://example.com/#fragment"),
        ("", ""),
    ],
)
def test_a_dotenv_value_is_read_as_compose_reads_it(line: str, value: str) -> None:
    assert _dotenv_value(line) == value


def test_a_variable_named_only_in_a_comment_is_not_read_by_the_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  builder:\n    # see $COMMENTED and ${ALSO_COMMENTED}\n"
        "    environment:\n      FOO: ${FOO:-}  # and $TRAILING\n",
        encoding="utf-8",
    )
    example = tmp_path / ".env.app.example"
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSE", compose)
    monkeypatch.setattr(sys.modules[__name__], "_ENV_EXAMPLE", example)

    example.write_text("# FOO=1\n", encoding="utf-8")
    test_the_example_dotenv_names_only_what_the_stack_reads()

    for name in ("COMMENTED", "ALSO_COMMENTED", "TRAILING"):
        example.write_text(f"{name}=1\n", encoding="utf-8")
        with pytest.raises(AssertionError):
            test_the_example_dotenv_names_only_what_the_stack_reads()


def test_a_bare_variable_counts_as_read_by_the_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``FOO: $FOO`` reads FOO as much as ``FOO: ${FOO}`` does."""
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  builder:\n    image: ${IMAGE:-x}\n    environment:\n"
        "      FOO: $FOO\n      PRICE: costs $$NOTAVARIABLE\n",
        encoding="utf-8",
    )
    example = tmp_path / ".env.app.example"
    example.write_text("IMAGE=y\n# FOO=1\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "_COMPOSE", compose)
    monkeypatch.setattr(sys.modules[__name__], "_ENV_EXAMPLE", example)

    test_the_example_dotenv_names_only_what_the_stack_reads()

    example.write_text("NOTAVARIABLE=1\n", encoding="utf-8")
    with pytest.raises(AssertionError):
        test_the_example_dotenv_names_only_what_the_stack_reads()

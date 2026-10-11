"""How settings are named, and what happens to a name that changes (#1108).

The catalog's names came from four prefixes and four names with none. Nothing said
which a new setting should take, and nothing said what a deployment would see if one
were renamed. The rule is now in the catalog (``NAME_FAMILIES``, ``NAMING_POLICY``) and
printed in the deployment guide; these tests are what holds the code to it.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path

import pytest

from kpubdata_builder import settings_catalog as catalog
from kpubdata_builder import settings_env
from kpubdata_builder.cli import main
from kpubdata_builder.service.build_limits import resolve_max_queued_builds
from kpubdata_builder.settings_catalog import EarlierName, Setting
from kpubdata_builder.settings_env import EarlierNameUse, apply_earlier_names

_ROOT = Path(__file__).resolve().parents[2]
_ENTRYPOINT = _ROOT / "docker-entrypoint.sh"

#: The settings whose names do not start with the prefix. They are kept as they are;
#: this list is not to grow. A new setting is named ``KPUBDATA_BUILDER_…``.
_NAMED_BEFORE_THE_RULE: frozenset[str] = frozenset(
    {
        "KPUBDATA_DUCKDB_THREADS",
        "KPUBDATA_DUCKDB_MEMORY_LIMIT",
        "KPUBDATA_DUCKDB_MAX_TEMP_SIZE",
        "KPUBDATA_QUERY_MAX_CONCURRENCY",
        "KPUBDATA_QUERY_MAX_MEMORY_MB",
        "KPUBDATA_QUERY_MEMORY_BUDGET_MB",
        "OIDC_JWKS_URL",
        "OIDC_JWKS_TTL",
        "OIDC_ISSUER",
        "OIDC_AUDIENCE",
        "OIDC_ALLOWED_HD",
        "OIDC_ALLOWED_SUBJECTS",
        "OIDC_ALLOWED_EMAILS",
        "ENFORCE_OWNERSHIP",
        "HF_TOKEN",
        "KAGGLE_USERNAME",
        "KAGGLE_KEY",
    }
)


def _setting(name: str, *earlier: str, secret: bool = False) -> Setting:
    return Setting(
        name=name,
        group="builds",
        description="d",
        default="d",
        required="d",
        secret=secret,
        earlier_names=tuple(EarlierName(old, "0.5.0") for old in earlier),
    )


def _families(name: str) -> list[str]:
    return [family.label for family in catalog.NAME_FAMILIES if family.holds(name)]


# ------------------------------------------------------------------ how a name looks


def test_every_setting_is_named_in_exactly_one_documented_way() -> None:
    for setting in catalog.SETTINGS:
        assert len(_families(setting.name)) == 1, setting.name


def test_no_setting_is_added_under_a_name_without_the_prefix() -> None:
    """Add ``KPUBDATA_QUERY_SOMETHING`` and this fails: the name starts with the prefix."""
    outside = {s.name for s in catalog.SETTINGS if not s.name.startswith(catalog.PREFIX)}

    assert outside == _NAMED_BEFORE_THE_RULE


def test_a_name_no_family_holds_is_seen_as_such() -> None:
    """The first test above can fail: a name of another shape belongs to no family."""
    assert _families("KPUBDATA_SOMETHING_NEW") == []
    assert _families("KAGGLE_KEY_FILE") == []
    assert _families("KPUBDATA_BUILDER_ANYTHING") == ["`KPUBDATA_BUILDER_*`"]


def test_the_guide_prints_the_policy_and_every_family() -> None:
    guide = (_ROOT / "docs" / "deployment.md").read_text(encoding="utf-8")

    for line in catalog.NAMING_POLICY:
        assert line in guide
    for family in catalog.NAME_FAMILIES:
        assert f"| {family.label} |" in guide


# -------------------------------------------------------------- the catalog's entries


def _entrypoint_names() -> set[str]:
    return set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", _ENTRYPOINT.read_text(encoding="utf-8")))


def _earlier_name_problems(settings: tuple[Setting, ...], entrypoint: set[str]) -> list[str]:
    """What is wrong with the earlier names ``settings`` declare."""
    problems: list[str] = []
    current = {setting.name for setting in settings} | catalog.internal_names()
    seen: set[str] = set()
    for setting in settings:
        for earlier in setting.earlier_names:
            if earlier.name in current:
                problems.append(f"{earlier.name} is the name of a setting in use")
            if earlier.name in seen:
                problems.append(f"{earlier.name} is an earlier name twice")
            seen.add(earlier.name)
            if not re.fullmatch(r"\d+\.\d+\.\d+", earlier.renamed_in):
                problems.append(f"{earlier.name}: renamed_in is not a release")
            if setting.name in entrypoint:
                problems.append(
                    f"{setting.name} is read by the entrypoint, which does not read {earlier.name}"
                )
    return problems


def test_the_catalog_declares_no_earlier_name_that_cannot_work() -> None:
    assert _earlier_name_problems(catalog.SETTINGS, _entrypoint_names()) == []
    # The entrypoint is read: it names the settings it is known to expand.
    assert {"KPUBDATA_BUILDER_PORT", "KPUBDATA_BUILDER_API_KEY"} <= _entrypoint_names()


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ((_setting("NEW_A", "NEW_B"), _setting("NEW_B")), "NEW_B is the name of a setting in use"),
        ((_setting("NEW_A", "KPUBDATA_QUERY_TEMP_DIR"),), "is the name of a setting in use"),
        ((_setting("NEW_A", "OLD"), _setting("NEW_B", "OLD")), "OLD is an earlier name twice"),
        ((_setting("KPUBDATA_BUILDER_PORT", "OLD_PORT"),), "read by the entrypoint"),
        (
            (replace(_setting("NEW_A"), earlier_names=(EarlierName("OLD", "next"),)),),
            "renamed_in is not a release",
        ),
    ],
)
def test_an_earlier_name_that_cannot_work_is_refused(
    settings: tuple[Setting, ...], message: str
) -> None:
    problems = _earlier_name_problems(settings, {"KPUBDATA_BUILDER_PORT"})

    assert any(message in problem for problem in problems), problems


# --------------------------------------------------------------- reading an old name


def test_a_value_under_an_earlier_name_is_read_as_the_setting() -> None:
    environ = {"OLD": "5"}

    uses = apply_earlier_names(environ, [_setting("NEW", "OLD")])

    assert environ == {"OLD": "5", "NEW": "5"}
    assert uses == [EarlierNameUse(earlier="OLD", current="NEW", renamed_in="0.5.0", used=True)]


def test_the_current_name_wins_and_the_earlier_one_is_still_reported() -> None:
    environ = {"OLD": "5", "NEW": "7"}

    uses = apply_earlier_names(environ, [_setting("NEW", "OLD")])

    assert environ == {"OLD": "5", "NEW": "7"}
    assert [(use.earlier, use.used) for use in uses] == [("OLD", False)]


@pytest.mark.parametrize("current", [None, ""])
def test_an_unset_or_empty_current_name_gives_way(current: str | None) -> None:
    """Compose hands the process an empty string for a setting left out of ``.env``."""
    environ = {"OLD": "5"} if current is None else {"OLD": "5", "NEW": current}

    apply_earlier_names(environ, [_setting("NEW", "OLD")])

    assert environ["NEW"] == "5"


@pytest.mark.parametrize("earlier", [None, ""])
def test_an_unset_or_empty_earlier_name_is_nothing_to_report(earlier: str | None) -> None:
    environ = {} if earlier is None else {"OLD": earlier}

    assert apply_earlier_names(environ, [_setting("NEW", "OLD")]) == []
    assert "NEW" not in environ


def test_of_several_earlier_names_the_most_recent_one_is_used() -> None:
    environ = {"OLDEST": "1", "OLDER": "2"}

    uses = apply_earlier_names(environ, [_setting("NEW", "OLDER", "OLDEST")])

    assert environ["NEW"] == "2"
    assert [(use.earlier, use.used) for use in uses] == [("OLDER", True), ("OLDEST", False)]


def test_a_setting_with_no_earlier_name_is_left_alone() -> None:
    environ = {"NEW": "7", "UNRELATED": "x"}

    assert apply_earlier_names(environ, [_setting("NEW")]) == []
    assert environ == {"NEW": "7", "UNRELATED": "x"}


@pytest.mark.parametrize("current", [None, "another-value"])
def test_the_warning_names_both_variables_and_never_a_value(current: str | None) -> None:
    canary = "canary-9f3c1e-do-not-print"
    environ = {"OLD_TOKEN": canary}
    if current is not None:
        environ["NEW_TOKEN"] = current

    (use,) = apply_earlier_names(environ, [_setting("NEW_TOKEN", "OLD_TOKEN", secret=True)])
    warning = use.warning()

    assert "OLD_TOKEN" in warning
    assert "NEW_TOKEN" in warning
    assert "0.5.0" in warning
    assert canary not in warning
    assert "another-value" not in warning
    assert canary not in repr(use)


def test_the_catalog_as_it_is_has_nothing_to_move(monkeypatch: pytest.MonkeyPatch) -> None:
    """No setting has been renamed, so a real environment is left exactly as it was."""
    assert catalog.earlier_names() == {}
    for setting in catalog.SETTINGS:
        monkeypatch.setenv(setting.name, "x")
    before = dict(os.environ)

    assert apply_earlier_names() == []
    assert dict(os.environ) == before


# ------------------------------------------------------- through the command, for real

_QUEUE = "KPUBDATA_BUILDER_MAX_QUEUED_BUILDS"
_OLD_QUEUE = "KPUBDATA_BUILDER_ASYNC_QUEUE_SIZE"


@pytest.fixture()
def renamed_queue_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog as it would be had the queue setting been renamed."""
    renamed = tuple(
        replace(setting, earlier_names=(EarlierName(_OLD_QUEUE, "0.5.0"),))
        if setting.name == _QUEUE
        else setting
        for setting in catalog.SETTINGS
    )
    monkeypatch.setattr(catalog, "SETTINGS", renamed)
    assert settings_env.settings_catalog is catalog
    for setting in catalog.SETTINGS:
        monkeypatch.delenv(setting.name, raising=False)
    # Registered with monkeypatch so that what the command writes is undone afterwards.
    monkeypatch.setenv(_QUEUE, "")


def _serve_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    import kpubdata_builder.service.http as http_module

    calls: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: calls.append(service))
    return calls


@pytest.mark.usefixtures("renamed_queue_setting")
def test_serve_reads_a_setting_written_under_its_earlier_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv(_OLD_QUEUE, "37")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert len(calls) == 1
    # The reader, which knows only the current name, got the value.
    assert resolve_max_queued_builds() == 37
    assert f"warning: {_OLD_QUEUE} was renamed to {_QUEUE} in 0.5.0" in captured.err
    assert "37" not in captured.err
    assert f"{_QUEUE} = 37  [earlier name]" in captured.out


@pytest.mark.usefixtures("renamed_queue_setting")
def test_serve_prefers_the_current_name_and_says_the_earlier_one_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve_calls(monkeypatch)
    monkeypatch.setenv(_OLD_QUEUE, "37")
    monkeypatch.setenv(_QUEUE, "12")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert resolve_max_queued_builds() == 12
    assert f"{_OLD_QUEUE} was renamed to {_QUEUE}" in captured.err
    assert "it is ignored" in captured.err
    assert f"{_QUEUE} = 12  [environment]" in captured.out


@pytest.mark.usefixtures("renamed_queue_setting")
def test_a_bad_value_under_an_earlier_name_is_refused_under_the_current_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The start-up check judges the value wherever it was written."""
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv(_OLD_QUEUE, "plenty")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 1

    assert f"error: {_QUEUE} must be an integer from 1 to 1000" in capsys.readouterr().err
    assert calls == []

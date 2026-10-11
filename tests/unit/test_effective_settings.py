"""The settings a process is running with can be read, and no secret is in them (#1108).

``effective_settings`` holds every setting of the catalog in one object, and
``kpubdata-builder settings`` and the first lines of ``serve`` print it. Most of these
tests are about what must never be in that output: each setting the catalog marks
``secret`` is given a value that appears nowhere else, and every way of printing the
object is searched for it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kpubdata_builder import settings_catalog as catalog
from kpubdata_builder.cli import main
from kpubdata_builder.service import startup_settings
from kpubdata_builder.service.effective_settings import (
    REDACTED,
    EffectiveSettings,
    effective_settings,
)
from kpubdata_builder.settings_env import EarlierNameUse

#: A master key the cipher accepts — 32 bytes, URL-safe base64 — so ``serve`` starts.
_MASTER_KEY = "Q0FOQVJZ" * 5 + "Q0E="
#: One value per secret setting, each found nowhere else in the repository's output.
_CANARIES: dict[str, str] = {
    "KPUBDATA_BUILDER_API_KEY": "canary-api-key-7c1f0b2e9d4a4c6f8e3b5a1d0c9f2e7b",
    "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY": _MASTER_KEY,
    "KPUBDATA_BUILDER_CUBRID_URL": "cubrid+pycubrid://dba:canary-db-password@db:33000/kpub",
    "HF_TOKEN": "hf_canaryTokenValue0123456789",
    "KAGGLE_USERNAME": "canary-kaggle-account",
    "KAGGLE_KEY": "canarykagglekey0123456789abcdef",
}
#: The parts of a canary that must not appear either: a password inside a URL.
_FRAGMENTS = [*_CANARIES.values(), "canary-db-password"]


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for setting in catalog.SETTINGS:
        monkeypatch.delenv(setting.name, raising=False)


@pytest.fixture()
def secrets_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _CANARIES.items():
        monkeypatch.setenv(name, value)


def _assert_no_secret(text: str) -> None:
    for fragment in _FRAGMENTS:
        assert fragment not in text


def _serve_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    import kpubdata_builder.service.http as http_module

    calls: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: calls.append(service))
    return calls


# ----------------------------------------------------------------- which are secrets


def test_every_secret_setting_has_a_canary_here() -> None:
    """Mark a setting secret and this fails until the tests below search for its value."""
    assert {setting.name for setting in catalog.SETTINGS if setting.secret} == set(_CANARIES)


def test_a_setting_named_like_a_credential_is_marked_secret() -> None:
    """A key, a token, a password or a secret by name is one until someone says otherwise."""
    looks_secret = re.compile(r"(?:^|_)(KEY|TOKEN|SECRET|PASSWORD|PASSWD)(?:_|$)")
    for setting in catalog.SETTINGS:
        if looks_secret.search(setting.name):
            assert setting.secret, setting.name
    assert looks_secret.search("KPUBDATA_BUILDER_API_KEY")
    assert looks_secret.search("HF_TOKEN")
    assert not looks_secret.search("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL")


# ------------------------------------------------------------------ no secret, anywhere


@pytest.mark.usefixtures("secrets_set")
def test_the_object_does_not_hold_a_secret() -> None:
    settings = effective_settings()

    _assert_no_secret(repr(settings))
    _assert_no_secret("\n".join(settings.lines()))
    _assert_no_secret(json.dumps(settings.as_list()))
    for name in _CANARIES:
        entry = settings[name]
        assert entry.value is None
        assert entry.is_set
        assert entry.line() == f"{name} = {REDACTED}  [environment]"


@pytest.mark.usefixtures("secrets_set")
@pytest.mark.parametrize("arguments", [["settings"], ["settings", "--json"]])
def test_the_settings_command_prints_no_secret(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(arguments) == 0

    captured = capsys.readouterr()
    _assert_no_secret(captured.out)
    _assert_no_secret(captured.err)
    # It did print the settings: the search above was of the real output.
    for name in _CANARIES:
        assert name in captured.out


@pytest.mark.usefixtures("secrets_set")
def test_serve_prints_no_secret_when_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _serve_calls(monkeypatch)

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert len(calls) == 1
    _assert_no_secret(captured.out)
    _assert_no_secret(captured.err)
    assert "settings in effect" in captured.out
    for name in _CANARIES:
        assert f"  {name} = {REDACTED}  [environment]" in captured.out


@pytest.mark.usefixtures("secrets_set")
def test_serve_prints_no_secret_when_it_refuses_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_QUEUED_BUILDS", "0")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert calls == []
    assert "error: KPUBDATA_BUILDER_MAX_QUEUED_BUILDS" in captured.err
    _assert_no_secret(captured.out)
    _assert_no_secret(captured.err)


def test_a_separate_process_prints_no_secret_from_its_own_environment() -> None:
    """The command as an operator runs it: a new process, given the values by its parent."""
    environment = {
        name: value for name, value in os.environ.items() if name not in catalog.setting_names()
    }
    environment.update(_CANARIES)
    environment["KPUBDATA_BUILDER_MAX_BUILDS"] = "3"

    done = subprocess.run(
        [sys.executable, "-c", "from kpubdata_builder.cli import main; raise SystemExit(main())"]
        + ["settings"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    _assert_no_secret(done.stdout)
    _assert_no_secret(done.stderr)
    assert "KPUBDATA_BUILDER_MAX_BUILDS = 3  [environment]" in done.stdout
    assert f"HF_TOKEN = {REDACTED}  [environment]" in done.stdout


def test_a_secret_that_is_not_set_says_so() -> None:
    assert effective_settings()["HF_TOKEN"].line() == "HF_TOKEN = (not set)"


# ---------------------------------------------------------------- what the object says


def test_there_is_one_entry_per_setting_in_catalog_order() -> None:
    settings = effective_settings()

    assert isinstance(settings, EffectiveSettings)
    assert [entry.name for entry in settings.entries] == [s.name for s in catalog.SETTINGS]
    assert len(settings.lines()) == len(catalog.SETTINGS)
    with pytest.raises(KeyError):
        settings["NOT_A_SETTING"]


def test_nothing_set_is_every_setting_at_its_default() -> None:
    settings = effective_settings()

    assert [entry.name for entry in settings.entries if entry.is_set] == []
    assert settings.lines(only_set=True) == []
    assert settings["KPUBDATA_BUILDER_MAX_QUEUED_BUILDS"].line() == (
        "KPUBDATA_BUILDER_MAX_QUEUED_BUILDS = (default: 10)"
    )


@pytest.mark.parametrize(
    ("name", "written", "value"),
    [
        ("KPUBDATA_BUILDER_MAX_BUILDS", "4", 4),
        ("KPUBDATA_BUILDER_MAX_BUILDS", " 4 ", 4),
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "2.5", 2.5),
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "30", 30.0),
        ("KPUBDATA_DUCKDB_MEMORY_LIMIT", "256MB", "256MB"),
        ("OIDC_ISSUER", "https://id.example/realms/kpub", "https://id.example/realms/kpub"),
    ],
)
def test_a_value_has_the_type_its_kind_names(
    monkeypatch: pytest.MonkeyPatch, name: str, written: str, value: object
) -> None:
    monkeypatch.setenv(name, written)

    entry = effective_settings()[name]

    assert entry.value == value
    assert type(entry.value) is type(value)
    assert entry.source == "environment"
    assert entry.line() == f"{name} = {value}  [environment]"


def test_a_flag_takes_the_place_of_its_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_BUILDS", "not read")

    entry = effective_settings(flags={"KPUBDATA_BUILDER_MAX_BUILDS": 6})[
        "KPUBDATA_BUILDER_MAX_BUILDS"
    ]

    assert (entry.source, entry.value) == ("flag", 6)
    assert entry.line() == "KPUBDATA_BUILDER_MAX_BUILDS = 6  [flag]"


def test_a_value_that_came_under_an_earlier_name_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_BUILDS", "4")
    use = EarlierNameUse("OLD_MAX_BUILDS", "KPUBDATA_BUILDER_MAX_BUILDS", "0.5.0", used=True)
    ignored = EarlierNameUse("OLD_WORKERS", "KPUBDATA_BUILDER_MAX_WORKERS", "0.5.0", used=False)
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_WORKERS", "8")

    settings = effective_settings(earlier=[use, ignored])

    assert settings["KPUBDATA_BUILDER_MAX_BUILDS"].source == "earlier name"
    assert settings["KPUBDATA_BUILDER_MAX_WORKERS"].source == "environment"


@pytest.mark.parametrize("name", sorted(startup_settings.FALLS_BACK))
def test_a_value_its_reader_ignores_is_shown_as_the_default_in_use(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The same rule as the start-up warning, so the two cannot say different things."""
    monkeypatch.setenv(name, "unreadable")

    entry = effective_settings()[name]

    assert (entry.source, entry.value) == ("default", None)
    assert "ignored" in entry.note
    assert "unreadable" not in entry.line()
    assert any(name in warning for warning in startup_settings.check_settings().warnings)
    # Not at its default as far as the operator is concerned: the start log shows it.
    assert entry.line() in effective_settings().lines(only_set=True)


@pytest.mark.parametrize(
    ("name", "written", "on"),
    [
        ("KPUBDATA_BUILDER_DEV_MODE", "true", True),
        ("KPUBDATA_BUILDER_DEV_MODE", "1", True),
        ("KPUBDATA_BUILDER_DEV_MODE", "yes", False),
        ("KPUBDATA_BUILDER_DEV_MODE", "false", False),
        ("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", "yes", True),
        ("ENFORCE_OWNERSHIP", "TRUE", True),
    ],
)
def test_a_switch_is_shown_as_on_or_off_as_its_reader_takes_it(
    monkeypatch: pytest.MonkeyPatch, name: str, written: str, on: bool
) -> None:
    monkeypatch.setenv(name, written)

    entry = effective_settings()[name]

    assert entry.value is on
    assert entry.line().startswith(f"{name} = {'on' if on else 'off'}  [environment]")


def test_a_switch_the_deployment_turns_on_is_shown_as_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "false")

    settings = effective_settings()
    written = settings["KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL"]
    unwritten = settings["KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL"]

    assert written.value is True
    assert unwritten.value is True
    assert "more than one user" in written.note
    # Shown at start though its variable is empty: it is not what the guide's default says.
    assert unwritten.line() in settings.lines(only_set=True)


def test_every_setting_of_kind_flag_is_one_the_start_up_check_reads_as_a_flag() -> None:
    assert {s.name for s in catalog.SETTINGS if s.kind == "flag"} == set(startup_settings.FLAGS)


@pytest.mark.parametrize("name", sorted(startup_settings.FALLS_BACK))
def test_a_kind_agrees_with_what_the_start_up_check_accepts(name: str) -> None:
    """A setting that takes ``1.5`` is a number; one that does not is an integer."""
    (setting,) = [s for s in catalog.SETTINGS if s.name == name]
    accepts, _ = startup_settings.FALLS_BACK[name]

    assert setting.kind == ("number" if accepts("1.5") else "integer")


def test_json_is_valid_when_a_value_is_not_a_finite_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``inf`` is a credential lifetime its reader takes, and JSON has no number for it."""
    monkeypatch.setenv("KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS", "inf")

    assert main(["settings", "--json"]) == 0

    entries = json.loads(capsys.readouterr().out, parse_constant=pytest.fail)
    (entry,) = [e for e in entries if e["name"] == "KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS"]
    assert entry["value"] == "inf"
    assert [e["name"] for e in entries] == [s.name for s in catalog.SETTINGS]


# ------------------------------------------------------------------------ the command


def test_the_settings_command_fails_where_serve_would_refuse(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KPUBDATA_DUCKDB_MEMORY_LIMIT", "lots")
    monkeypatch.setenv("KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS", "soon")

    assert main(["settings"]) == 1

    captured = capsys.readouterr()
    assert "error: KPUBDATA_DUCKDB_MEMORY_LIMIT" in captured.err
    assert "warning: KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS" in captured.err
    assert "KPUBDATA_DUCKDB_MEMORY_LIMIT = lots  [environment]" in captured.out


def test_serve_prints_what_its_flags_and_the_environment_gave_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _serve_calls(monkeypatch)
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_QUEUED_BUILDS", "25")
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_BUILDS", "9")

    assert main(["serve", "--output-dir", str(tmp_path), "--port", "0", "--max-builds", "2"]) == 0

    out = capsys.readouterr().out
    start = out.index("settings in effect")
    shown = out[start : out.index("serving kpubdata-builder", start)].splitlines()[1:]
    assert shown == [
        "  KPUBDATA_BUILDER_MAX_BUILDS = 2  [flag]",
        "  KPUBDATA_BUILDER_MAX_QUEUED_BUILDS = 25  [environment]",
        "  KPUBDATA_BUILDER_PORT = 0  [flag]",
        f"  KPUBDATA_BUILDER_OUTPUT_DIR = {tmp_path}  [flag]",
        "  KPUBDATA_BUILDER_HOST = 127.0.0.1  [flag]",
    ]

"""A setting that cannot be used is found before the server takes a request (#1108).

Most of these give the check a bad value and expect it to say so: a check nobody has
seen refuse is not known to work. The rest hold the check to the readers — a warning
that the default is in use has to be true of what the reader does with that value.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from kpubdata_builder import settings_catalog as catalog
from kpubdata_builder.cli import main
from kpubdata_builder.ingestion.url_fetch import default_max_fetch_bytes
from kpubdata_builder.service import startup_settings
from kpubdata_builder.service.auth_throttle import AuthFailureThrottle
from kpubdata_builder.service.probe_limit import probe_interval_seconds
from kpubdata_builder.service.request_credentials import _ttl_seconds
from kpubdata_builder.service.startup_settings import check_settings
from kpubdata_builder.service.upload_limits import resolve_upload_limits
from kpubdata_builder.stages.bronze.checkpoint import checkpoint_max_age_seconds
from kpubdata_builder.uploads.store import resolve_max_upload_bytes

#: Settings with nothing to check as text, and why.
_NOT_CHECKED: dict[str, str] = {
    "KPUBDATA_BUILDER_API_KEY": "any text is a key",
    "KPUBDATA_BUILDER_ALLOWED_ORIGINS": "a list of origins, compared as text",
    "KPUBDATA_BUILDER_TRUSTED_PROXIES": "its reader warns about each entry it drops",
    "KPUBDATA_BUILDER_ADMIN_SUBJECTS": "validate_oidc_config",
    "OIDC_ISSUER": "validate_oidc_config",
    "OIDC_AUDIENCE": "validate_oidc_config",
    "OIDC_JWKS_URL": "a URL, fetched on the first sign-in",
    "OIDC_ALLOWED_HD": "validate_oidc_config",
    "OIDC_ALLOWED_SUBJECTS": "validate_oidc_config",
    "OIDC_ALLOWED_EMAILS": "validate_oidc_config",
    "KPUBDATA_BUILDER_CANCELLED_RUN_TTL_HOURS": "read by prune-cancelled, not by serve",
    "KPUBDATA_BUILDER_WAREHOUSE": "a path, created on first use",
    "KPUBDATA_BUILDER_LOCAL_PUBLISH_ROOT": "a path, resolved on publish",
    "HF_TOKEN": "a secret",
    "KAGGLE_USERNAME": "an account name",
    "KAGGLE_KEY": "a secret",
    "KPUBDATA_BUILDER_OUTPUT_DIR": "read by the container entrypoint",
    "KPUBDATA_BUILDER_HOST": "read by the container entrypoint",
}

_VARIABLES = [setting.name for setting in catalog.SETTINGS]


@pytest.fixture(autouse=True)
def _no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _VARIABLES:
        monkeypatch.delenv(name, raising=False)


def test_every_setting_is_checked_or_says_why_not() -> None:
    """A new setting has to land in one of the groups, so it cannot go unconsidered."""
    groups = [
        set(startup_settings.REFUSED),
        set(startup_settings.FALLS_BACK),
        set(startup_settings.FLAGS),
        set(startup_settings.READ_BY_SERVE),
        set(startup_settings.READ_BY_ENTRYPOINT),
        set(_NOT_CHECKED),
    ]
    seen: set[str] = set()
    for group in groups:
        assert not seen & group
        seen |= group

    assert seen == set(_VARIABLES)


def test_nothing_set_is_nothing_to_report() -> None:
    report = check_settings()

    assert report.problems == []
    assert report.warnings == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KPUBDATA_DUCKDB_MEMORY_LIMIT", "lots"),
        ("KPUBDATA_DUCKDB_MAX_TEMP_SIZE", "10"),
        ("KPUBDATA_DUCKDB_THREADS", "0"),
        ("KPUBDATA_DUCKDB_THREADS", "two"),
        ("KPUBDATA_QUERY_MAX_CONCURRENCY", "0"),
        ("KPUBDATA_QUERY_MAX_CONCURRENCY", ""),
        ("KPUBDATA_QUERY_MAX_MEMORY_MB", "1GB"),
        ("KPUBDATA_QUERY_MEMORY_BUDGET_MB", "-1"),
        ("KPUBDATA_BUILDER_STORAGE_BACKEND", "postgres"),
        ("KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS", "inf"),
        ("KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT", "0"),
        ("KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT", "nan"),
        ("KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT", "ten"),
        ("OIDC_JWKS_TTL", "1h"),
        ("OIDC_JWKS_TTL", "3600.0"),
    ],
)
def test_unusable_value_is_a_problem_that_names_the_variable(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    report = check_settings()

    assert len(report.problems) == 1
    assert name in report.problems[0]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KPUBDATA_DUCKDB_MEMORY_LIMIT", "512MB"),
        ("KPUBDATA_DUCKDB_MAX_TEMP_SIZE", "2GiB"),
        ("KPUBDATA_DUCKDB_THREADS", "4"),
        ("KPUBDATA_QUERY_MAX_CONCURRENCY", "1"),
        ("KPUBDATA_QUERY_MAX_MEMORY_MB", "512"),
        ("KPUBDATA_QUERY_MEMORY_BUDGET_MB", "2048"),
        ("KPUBDATA_BUILDER_STORAGE_BACKEND", "SQLite"),
        ("KPUBDATA_BUILDER_SHUTDOWN_GRACE_SECONDS", "0"),
        ("KPUBDATA_BUILDER_PROVIDER_TEST_TIMEOUT", "2.5"),
        ("OIDC_JWKS_TTL", "300"),
        ("KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY", "QUJD" * 8 + "QUJDQUJDQUI="),
    ],
)
def test_usable_value_is_not_reported(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    report = check_settings()

    assert report.problems == []
    assert report.warnings == []


def test_cubrid_backend_without_a_url_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")

    assert any("KPUBDATA_BUILDER_CUBRID_URL" in problem for problem in check_settings().problems)


@pytest.mark.parametrize("value", ["not base64!", "c2hvcnQ="])
def test_master_key_problem_does_not_repeat_the_key(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY", value)

    (problem,) = check_settings().problems

    assert "KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY" in problem
    assert value not in problem


@pytest.mark.parametrize(
    "url",
    [
        # The scheme left out, and mistyped: the whole value is then user:pass@host.
        "dba:fake-password-123@db.internal:33000/builder",
        "cubrid+pycubrid:/dba:fake-password-123@db.internal:33000/builder",
        # "://" later in the value does not make what is before it a scheme.
        "dba:fake-password-123@db.internal:33000/builder?next=http://x",
        # A scheme that is refused, with the password after it.
        "postgresql://dba:fake-password-123@db.internal:5432/builder",
        "cubrid+cubriddb://dba:fake-password-123@db.internal:33000/builder",
        "cubrid+aiopycubrid://dba:fake-password-123@db.internal:33000/builder",
    ],
)
def test_cubrid_url_problem_does_not_repeat_the_password(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv("KPUBDATA_BUILDER_CUBRID_URL", url)

    (problem,) = check_settings().problems

    assert "KPUBDATA_BUILDER_CUBRID_URL" in problem
    assert "fake-password-123" not in problem
    assert "db.internal" not in problem


def test_every_problem_is_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_DUCKDB_MEMORY_LIMIT", "lots")
    monkeypatch.setenv("KPUBDATA_QUERY_MAX_CONCURRENCY", "0")
    monkeypatch.setenv("OIDC_JWKS_TTL", "1h")
    monkeypatch.setenv("KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS", "soon")

    report = check_settings()

    assert len(report.problems) == 3
    assert len(report.warnings) == 1


def _upload_limit(field: str) -> Callable[[], object]:
    def read() -> object:
        limits = resolve_upload_limits()
        assert limits is not None
        return getattr(limits, field)

    return read


def _throttle(field: str) -> Callable[[], object]:
    return lambda: getattr(AuthFailureThrottle(), field)


#: Each setting whose reader falls back, with that reader and values on either side.
_FALLBACK_CASES: list[tuple[str, Callable[[], object], list[str], list[str]]] = [
    (
        "KPUBDATA_BUILDER_AUTH_FAILURE_LIMIT",
        _throttle("_limit"),
        ["many", "1.5"],
        ["7", "0", "-1"],
    ),
    (
        "KPUBDATA_BUILDER_AUTH_FAILURE_WINDOW_SECONDS",
        _throttle("_window"),
        ["soon", "0", "-5", "nan"],
        ["7", "0.5"],
    ),
    (
        "KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS",
        probe_interval_seconds,
        ["soon", "-1", "inf", "nan"],
        ["7", "0"],
    ),
    (
        "KPUBDATA_BUILDER_JOB_CREDENTIAL_TTL_SECONDS",
        _ttl_seconds,
        ["soon", "0", "-1", "nan"],
        ["7", "0.5"],
    ),
    (
        "KPUBDATA_BUILDER_CHECKPOINT_MAX_AGE_SECONDS",
        checkpoint_max_age_seconds,
        ["soon", "-1", "inf", "nan"],
        ["7", "0"],
    ),
    (
        "KPUBDATA_BUILDER_MAX_UPLOAD_BYTES",
        resolve_max_upload_bytes,
        ["big", "0", "-1", "1.5"],
        ["7"],
    ),
    (
        "KPUBDATA_BUILDER_UPLOAD_MAX_FILES",
        _upload_limit("max_files"),
        ["many", "-1", "1.5"],
        ["7"],
    ),
    (
        "KPUBDATA_BUILDER_UPLOAD_MAX_TOTAL_BYTES",
        _upload_limit("max_total_bytes"),
        ["big", "-1"],
        ["7"],
    ),
    (
        "KPUBDATA_BUILDER_UPLOAD_RETENTION_DAYS",
        _upload_limit("retention_days"),
        ["long", "-1"],
        ["7"],
    ),
    (
        "KPUBDATA_BUILDER_URL_FETCH_MAX_BYTES",
        default_max_fetch_bytes,
        ["big", "0", "-1"],
        ["7"],
    ),
]


def test_every_fallback_setting_has_a_case() -> None:
    assert {case[0] for case in _FALLBACK_CASES} == set(startup_settings.FALLS_BACK)


@pytest.mark.parametrize(
    ("name", "read", "ignored", "taken"), _FALLBACK_CASES, ids=[c[0] for c in _FALLBACK_CASES]
)
def test_warning_is_true_of_what_the_reader_does(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    read: Callable[[], object],
    ignored: list[str],
    taken: list[str],
) -> None:
    """Warned exactly when the reader used its default instead of the value."""
    # The upload limits are only in force in a multi-user deployment.
    monkeypatch.setenv("ENFORCE_OWNERSHIP", "true")
    default = read()

    for value in ignored:
        monkeypatch.setenv(name, value)
        report = check_settings()
        assert read() == default, value
        assert [w for w in report.warnings if name in w], value
        assert report.problems == []

    for value in taken:
        monkeypatch.setenv(name, value)
        assert read() != default, value
        assert check_settings().warnings == [], value


def _flag_readers() -> dict[str, Callable[[], bool]]:
    """Each flag's own reader, as "is it on"."""
    from kpubdata_builder.service import publish_credentials
    from kpubdata_builder.service.auth import _is_dev_mode, _ownership_enforced
    from kpubdata_builder.service.providers import require_own_provider_credential

    return {
        "KPUBDATA_BUILDER_DEV_MODE": _is_dev_mode,
        "ENFORCE_OWNERSHIP": _ownership_enforced,
        "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL": require_own_provider_credential,
        # Its reader answers the opposite question: may the server's own token be used.
        "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL": lambda: (
            not publish_credentials.server_fallback_allowed()
        ),
    }


def test_every_flag_has_its_reader_here() -> None:
    assert set(_flag_readers()) == set(startup_settings.FLAGS)


@pytest.mark.parametrize("name", sorted(startup_settings.FLAGS))
@pytest.mark.parametrize("value", ["yes", "on", "enabled", "2", "true", "TRUE", "1", "false", "0"])
def test_flag_warning_is_true_of_what_its_reader_does(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """Warned as "read as off" exactly when the reader reads it as off and it was not
    written as off on purpose.

    Held to each reader, because they differ: one of them takes ``yes`` as on, and the
    check said of that one too that ``yes`` is read as off.
    """
    monkeypatch.setenv(name, value)

    on = _flag_readers()[name]()
    warnings = check_settings().warnings

    if on or value.lower() in ("false", "0"):
        assert warnings == []
    else:
        (warning,) = warnings
        assert name in warning
        assert "read as off" in warning


@pytest.mark.parametrize("name", sorted(startup_settings.FLAGS))
@pytest.mark.parametrize("value", ["true", "TRUE", "1", "false", "0"])
def test_flag_written_as_documented_is_not_warned(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    assert check_settings().warnings == []


def test_flag_with_a_space_is_warned_only_where_the_reader_keeps_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kpubdata_builder.service.auth import _is_dev_mode
    from kpubdata_builder.service.providers import require_own_provider_credential

    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true ")
    monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", " true ")

    (warning,) = check_settings().warnings

    assert not _is_dev_mode()
    assert require_own_provider_credential()
    assert "KPUBDATA_BUILDER_DEV_MODE" in warning


def _serve_calls(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    import kpubdata_builder.service.http as http_module

    calls: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **kwargs: calls.append(service))
    return calls


def test_serve_refuses_to_start_and_prints_every_problem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv("KPUBDATA_DUCKDB_MEMORY_LIMIT", "lots")
    monkeypatch.setenv("KPUBDATA_QUERY_MAX_CONCURRENCY", "0")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert "error: KPUBDATA_DUCKDB_MEMORY_LIMIT" in err
    assert "error: KPUBDATA_QUERY_MAX_CONCURRENCY" in err
    assert "Traceback" not in err
    assert calls == []
    # Nothing was created for a server that did not start.
    assert list(tmp_path.iterdir()) == []


def test_serve_starts_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv("KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS", "soon")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0

    assert "warning: KPUBDATA_BUILDER_PROBE_INTERVAL_SECONDS" in capsys.readouterr().err
    assert len(calls) == 1


_COUNTS = [
    "KPUBDATA_BUILDER_MAX_WORKERS",
    "KPUBDATA_BUILDER_MAX_BUILDS",
    "KPUBDATA_BUILDER_MAX_PREVIEWS",
]
_FLAG_OF = {
    "KPUBDATA_BUILDER_MAX_WORKERS": "--max-workers",
    "KPUBDATA_BUILDER_MAX_BUILDS": "--max-builds",
    "KPUBDATA_BUILDER_MAX_PREVIEWS": "--max-previews",
}


@pytest.mark.parametrize("name", _COUNTS)
@pytest.mark.parametrize("value", ["many", "0", "-1", "1.5"])
def test_a_count_that_cannot_be_used_is_a_problem(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    (problem,) = check_settings().problems

    assert f"{name} must be an integer >= 1" in problem


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "soon"),
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "inf"),
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "-1"),
        ("KPUBDATA_BUILDER_PORT", "abc"),
        # Nothing but spaces: the entrypoint passes it to --port, which refuses it.
        ("KPUBDATA_BUILDER_PORT", "  "),
        ("KPUBDATA_BUILDER_PORT", "65536"),
        ("KPUBDATA_BUILDER_PORT", "-1"),
    ],
)
def test_a_serve_value_that_cannot_be_used_is_a_problem(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    (problem,) = check_settings().problems

    assert name in problem


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KPUBDATA_BUILDER_MAX_WORKERS", "10"),
        ("KPUBDATA_BUILDER_MAX_PREVIEWS", "1"),
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "0"),
        ("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "2.5"),
        ("KPUBDATA_BUILDER_PORT", "0"),
        ("KPUBDATA_BUILDER_PORT", "8000"),
        # argparse's int() takes spaces around a number, so this is a usable port.
        ("KPUBDATA_BUILDER_PORT", " 8000 "),
    ],
)
def test_a_serve_value_that_can_be_used_is_not_reported(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    assert check_settings().problems == []


def test_a_value_a_flag_takes_the_place_of_is_not_judged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KPUBDATA_BUILDER_MAX_BUILDS", "many")

    assert check_settings(overridden={"KPUBDATA_BUILDER_MAX_BUILDS"}).problems == []
    assert len(check_settings().problems) == 1


@pytest.mark.parametrize("name", _COUNTS)
def test_serve_says_the_count_that_is_not_an_integer_in_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv(name, "many")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 1

    err = capsys.readouterr().err
    assert f"error: {name} must be an integer >= 1" in err
    assert "Traceback" not in err
    assert calls == []


@pytest.mark.parametrize("name", _COUNTS)
def test_serve_starts_when_a_flag_takes_the_place_of_a_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """As before: the variable is not read when its flag is given."""
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv(name, "many")

    assert main(["serve", "--output-dir", str(tmp_path), _FLAG_OF[name], "3"]) == 0
    assert len(calls) == 1


def test_serve_does_not_judge_the_port_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``serve`` takes its port from ``--port``; the variable is the entrypoint's."""
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv("KPUBDATA_BUILDER_PORT", "abc")

    assert main(["serve", "--output-dir", str(tmp_path)]) == 0
    assert len(calls) == 1


def _effective() -> dict[str, Callable[[], bool]]:
    """Each flag as the deployment ends up treating it: on or off, whoever decided."""
    from kpubdata_builder.service import publish_credentials
    from kpubdata_builder.service.auth import _is_dev_mode
    from kpubdata_builder.service.ownership import enforce_ownership
    from kpubdata_builder.service.providers import require_own_provider_credential

    return {
        "KPUBDATA_BUILDER_DEV_MODE": _is_dev_mode,
        "ENFORCE_OWNERSHIP": enforce_ownership,
        "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL": require_own_provider_credential,
        "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL": lambda: (
            not publish_credentials.server_fallback_allowed()
        ),
    }


_MODES: dict[str, dict[str, str]] = {
    "single-user": {},
    "OIDC": {"OIDC_ISSUER": "https://id.example.com/realms/x"},
    "ENFORCE_OWNERSHIP": {"ENFORCE_OWNERSHIP": "true"},
}


@pytest.mark.parametrize("mode", sorted(_MODES))
@pytest.mark.parametrize("name", sorted(startup_settings.FLAGS))
@pytest.mark.parametrize("value", ["yes", "on", "enabled", "true", "1", "false", "0"])
def test_no_flag_that_is_on_is_said_to_be_off(
    monkeypatch: pytest.MonkeyPatch, mode: str, name: str, value: str
) -> None:
    """In every kind of deployment: "read as off" is said only of a flag that is off.

    A multi-user deployment turns three of them on whatever they say (ADR 0012). The
    check compared each value with the words its reader takes and said "read as off"
    of ``ENFORCE_OWNERSHIP=yes`` under OIDC — where ownership is enforced.
    """
    for variable, setting in _MODES[mode].items():
        monkeypatch.setenv(variable, setting)
    if _MODES[mode].get(name):
        pytest.skip("the mode is this flag, written as on")
    monkeypatch.setenv(name, value)

    on = _effective()[name]()
    about = [warning for warning in check_settings().warnings if warning.startswith(f"{name} ")]

    for warning in about:
        assert ("read as off" in warning) == (not on), warning
        assert ("turns it on whatever it says" in warning) == on, warning
    if not on and value not in ("false", "0"):
        assert len(about) == 1


@pytest.mark.parametrize(
    ("mode", "name", "value"),
    [
        ("OIDC", "ENFORCE_OWNERSHIP", "yes"),
        ("OIDC", "ENFORCE_OWNERSHIP", "false"),
        ("OIDC", "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "yes"),
        ("ENFORCE_OWNERSHIP", "KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "yes"),
        ("ENFORCE_OWNERSHIP", "KPUBDATA_BUILDER_REQUIRE_OWN_PROVIDER_CREDENTIAL", "0"),
    ],
)
def test_a_switch_the_deployment_forces_on_is_said_to_be_ignored(
    monkeypatch: pytest.MonkeyPatch, mode: str, name: str, value: str
) -> None:
    for variable, setting in _MODES[mode].items():
        monkeypatch.setenv(variable, setting)
    monkeypatch.setenv(name, value)

    assert _effective()[name]() is True
    (warning,) = [w for w in check_settings().warnings if w.startswith(f"{name} ")]

    assert "is ignored" in warning
    assert "read as off" not in warning


@pytest.mark.parametrize("name", _COUNTS)
def test_a_count_of_nothing_but_spaces_is_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """To the check and to ``serve`` alike: the check passed it and ``serve`` then stopped."""
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv(name, "   ")

    assert check_settings().problems == []
    assert main(["serve", "--output-dir", str(tmp_path)]) == 0
    assert len(calls) == 1


def test_a_wait_bound_of_nothing_but_spaces_is_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _serve_calls(monkeypatch)
    monkeypatch.setenv("KPUBDATA_BUILDER_BUILD_WAIT_SECONDS", "  ")

    assert check_settings().problems == []
    assert main(["serve", "--output-dir", str(tmp_path)]) == 0
    assert len(calls) == 1

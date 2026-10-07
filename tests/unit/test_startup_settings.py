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
    "KPUBDATA_BUILDER_PORT": "read by the container entrypoint",
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


@pytest.mark.parametrize("name", sorted(startup_settings.FLAGS))
@pytest.mark.parametrize("value", ["yes", "on", "enabled", "2"])
def test_flag_written_another_way_is_warned_as_off(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    (warning,) = check_settings().warnings

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


@pytest.mark.parametrize(
    "name",
    [
        "KPUBDATA_BUILDER_MAX_WORKERS",
        "KPUBDATA_BUILDER_MAX_BUILDS",
        "KPUBDATA_BUILDER_MAX_PREVIEWS",
    ],
)
def test_serve_names_the_count_that_is_not_an_integer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _serve_calls(monkeypatch)
    monkeypatch.setenv(name, "many")

    with pytest.raises(SystemExit, match=f"{name} must be an integer"):
        main(["serve", "--output-dir", str(tmp_path)])

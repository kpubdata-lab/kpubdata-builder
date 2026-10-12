"""A CUBRID setting left behind refuses to start; SQLite state is read as it was (#1094).

ADR 0022 removed the CUBRID state backend. Its two settings are now on the catalog's
list of removed settings, and the contract is: ``KPUBDATA_BUILDER_STORAGE_BACKEND=cubrid``
or any ``KPUBDATA_BUILDER_CUBRID_URL`` stops every command with one line that names the
setting, says the backend was removed, names the ADR and what to do, and never repeats
the URL. No silent fallback to SQLite, no warning-only start.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpubdata_builder import settings_catalog as catalog
from kpubdata_builder.cli import main
from kpubdata_builder.service.app import _credential_repository_from_env
from kpubdata_builder.service.startup_settings import check_settings
from kpubdata_builder.store import (
    SqliteBuildIndex,
    bring_index_up_to_date,
    make_build_index,
    rebuild_index,
)
from kpubdata_builder.store.artifacts import LocalArtifactStore, make_artifact_store
from kpubdata_builder.store.backend import (
    RemovedStorageSettingError,
    refuse_removed_storage_settings,
    validate_storage_config,
)

_BACKEND = "KPUBDATA_BUILDER_STORAGE_BACKEND"
_URL = "KPUBDATA_BUILDER_CUBRID_URL"
_PASSWORD = "canary-cubrid-password-5d1e"
_HOST = "cubrid-host.internal"
_CUBRID_URL = f"cubrid+pycubrid://dba:{_PASSWORD}@{_HOST}:33000/kpub?charset=utf8"
#: One environment per way a CUBRID setting can be left behind.
_LEFT_BEHIND: dict[str, dict[str, str]] = {
    "backend": {_BACKEND: "cubrid"},
    "backend, other case": {_BACKEND: " CUBRID "},
    "url": {_URL: _CUBRID_URL},
    "url with sqlite": {_BACKEND: "sqlite", _URL: _CUBRID_URL},
    "both": {_BACKEND: "cubrid", _URL: _CUBRID_URL},
    "url without a scheme": {_URL: f"dba:{_PASSWORD}@{_HOST}:33000/kpub"},
}


@pytest.fixture(autouse=True)
def _no_storage_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (_BACKEND, _URL):
        monkeypatch.delenv(name, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]) -> None:
    for name, value in environment.items():
        monkeypatch.setenv(name, value)


def _assert_says_how_to_recover(message: str, names: list[str]) -> None:
    assert "\n" not in message
    for name in names:
        assert name in message
    assert "CUBRID state backend was removed" in message
    assert "ADR 0022" in message
    # The two things to do: reconcile first if that was not done, otherwise unset both.
    assert "section 3" in message
    assert f"unset {_BACKEND}, {_URL}" in message
    assert _PASSWORD not in message
    assert _HOST not in message
    assert "pycubrid" not in message


def _write_manifest(output: Path, run_id: str) -> None:
    (output / run_id).mkdir(parents=True)
    (output / run_id / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "ok",
                "started_at": "2026-10-01T10:00:00Z",
                "finished_at": "2026-10-01T10:05:00Z",
            }
        ),
        encoding="utf-8",
    )


# ------------------------------------------------------------------- the settings


def test_both_settings_are_removed_not_settings() -> None:
    assert catalog.removed_names() == {_BACKEND, _URL}
    assert not catalog.removed_names() & catalog.setting_names()


@pytest.mark.parametrize("value", ["", "sqlite", " SQLite "])
def test_nothing_or_sqlite_is_no_change(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(_BACKEND, value)

    refuse_removed_storage_settings()
    validate_storage_config()
    assert check_settings().problems == []


@pytest.mark.parametrize("case", sorted(_LEFT_BEHIND))
def test_a_setting_left_behind_is_refused_in_one_line_without_the_url(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    environment = _LEFT_BEHIND[case]
    _set(monkeypatch, environment)
    named = [
        name
        for name in (_BACKEND, _URL)
        if environment.get(name, "").strip().lower() not in ("", "sqlite")
    ]

    with pytest.raises(RemovedStorageSettingError) as refused:
        refuse_removed_storage_settings()

    assert list(refused.value.names) == named
    _assert_says_how_to_recover(str(refused.value), named)


def test_another_backend_is_refused_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_BACKEND, "postgres")

    with pytest.raises(RemovedStorageSettingError):
        refuse_removed_storage_settings()


def test_the_settings_check_reports_it_as_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    _set(monkeypatch, _LEFT_BEHIND["both"])

    (problem,) = check_settings().problems

    _assert_says_how_to_recover(problem, [_BACKEND, _URL])


# --------------------------------------------------------------- every way in


@pytest.mark.parametrize(
    "open_state",
    [
        make_build_index,
        make_artifact_store,
        rebuild_index,
        bring_index_up_to_date,
        _credential_repository_from_env,
        lambda _output: validate_storage_config(),
    ],
    ids=[
        "build index",
        "artifact store",
        "rebuild index",
        "index brought up to date",
        "credential repository",
        "serve validation",
    ],
)
def test_no_store_opens_with_a_setting_left_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, open_state: object
) -> None:
    _set(monkeypatch, _LEFT_BEHIND["url"])
    monkeypatch.setenv("KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY", "QUJD" * 8 + "QUJDQUJDQUI=")
    assert callable(open_state)

    with pytest.raises(RemovedStorageSettingError):
        open_state(tmp_path)

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["serve"],
        ["rebuild-index"],
        ["prune-cancelled"],
        ["prune-runs"],
        ["build", "spec.yaml"],
        ["settings"],
    ],
    ids=lambda arguments: arguments[0],
)
@pytest.mark.parametrize("case", ["backend", "url"])
def test_every_command_exits_non_zero_with_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    case: str,
) -> None:
    import kpubdata_builder.service.http as http_module

    served: list[object] = []
    monkeypatch.setattr(http_module, "serve", lambda service, **_: served.append(service))
    _set(monkeypatch, _LEFT_BEHIND[case])
    output = tmp_path / "data"
    output.mkdir()
    argv = arguments if arguments[0] == "settings" else [*arguments, "--output-dir", str(output)]

    assert main(argv) == 1

    captured = capsys.readouterr()
    (line,) = captured.err.splitlines()
    assert line.startswith("error: ")
    named = [name for name in (_BACKEND, _URL) if name in _LEFT_BEHIND[case]]
    _assert_says_how_to_recover(line, named)
    assert _PASSWORD not in captured.out
    assert served == []
    # Nothing was opened or written: not the index, not a lock, not a run.
    assert list(output.iterdir()) == []


# ------------------------------------------------------- SQLite state, as it was


def test_existing_sqlite_state_is_read_unchanged(tmp_path: Path) -> None:
    """An index and manifests an earlier release wrote are read as they are."""
    _write_manifest(tmp_path, "run-a")
    _write_manifest(tmp_path, "run-b")
    assert rebuild_index(tmp_path) == 2
    index_bytes = (tmp_path / "_builds.sqlite").read_bytes()
    manifest_bytes = (tmp_path / "run-a" / "manifest.json").read_bytes()

    index = make_build_index(tmp_path)
    store = make_artifact_store(tmp_path)

    assert isinstance(index, SqliteBuildIndex)
    assert isinstance(store, LocalArtifactStore)
    assert sorted(entry.run_id for entry in index.list_builds()) == ["run-a", "run-b"]
    assert store.get_manifest("run-a") == json.loads(manifest_bytes)
    assert bring_index_up_to_date(tmp_path) is None
    assert (tmp_path / "run-a" / "manifest.json").read_bytes() == manifest_bytes
    index.close()
    assert (tmp_path / "_builds.sqlite").read_bytes() == index_bytes


def test_rebuild_index_command_still_works_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(_BACKEND, "sqlite")
    _write_manifest(tmp_path, "run-a")

    assert main(["rebuild-index", "--output-dir", str(tmp_path)]) == 0

    assert "rebuilt index with 1 build(s)" in capsys.readouterr().out
    assert [entry.run_id for entry in SqliteBuildIndex(tmp_path).list_builds()] == ["run-a"]

"""CI tests Builder against every released kpubdata in the declared range (#832).

The matrix is only as good as this list: a version it drops silently is a version
nobody tests. Mostly negative tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from packaging.specifiers import SpecifierSet

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "kpubdata_release_matrix.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_kpubdata_release_matrix", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load()

_FILE = [{"yanked": False}]
_RANGE = SpecifierSet(">=0.7.0,<0.8")


def test_every_final_release_in_range_is_listed_oldest_first() -> None:
    releases = {"0.7.2": _FILE, "0.6.0": _FILE, "0.7.0": _FILE, "0.8.0": _FILE, "0.7.1": _FILE}

    assert matrix.supported_releases(releases, _RANGE) == ["0.7.0", "0.7.1", "0.7.2"]


@pytest.mark.parametrize(
    ("version", "files"),
    [
        ("0.7.1rc1", _FILE),
        ("0.7.1.dev3", _FILE),
        ("0.7.1", [{"yanked": True}]),
        ("0.7.1", []),
        ("not-a-version", _FILE),
    ],
    ids=["prerelease", "dev", "yanked", "no-files", "invalid"],
)
def test_an_uninstallable_or_unreleased_version_is_left_out(
    version: str, files: list[dict[str, Any]]
) -> None:
    releases = {"0.7.0": _FILE, version: files}

    assert matrix.supported_releases(releases, _RANGE) == ["0.7.0"]


def test_a_floor_that_is_not_released_fails_rather_than_shrinking() -> None:
    with pytest.raises(SystemExit, match="floor 0.7.0"):
        matrix.supported_releases({"0.7.1": _FILE}, _RANGE)


def test_an_empty_range_fails() -> None:
    with pytest.raises(SystemExit, match="no kpubdata release"):
        # No floor in the range, and the only release sits above it.
        matrix.supported_releases({"0.7.0": _FILE}, SpecifierSet("<0.7"))


def test_the_range_is_read_from_pyproject() -> None:
    specifier = matrix.declared_range(_ROOT / "pyproject.toml")

    assert str(specifier), "pyproject.toml declares no kpubdata range"
    assert any(s.operator == ">=" for s in specifier)


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _run(tmp_path: Path, releases: dict[str, Any], *argv: str) -> Any:
    pyproject = _write(
        tmp_path / "pyproject.toml",
        '[project]\nname = "x"\ndependencies = ["kpubdata>=0.7.0,<0.8"]\n',
    )
    releases_file = _write(tmp_path / "releases.json", json.dumps(releases))
    return matrix.main(["--pyproject", str(pyproject), "--releases", str(releases_file), *argv])


_LOCK = (
    '[[package]]\nname = "other"\nversion = "1.0"\n\n'
    '[[package]]\nname = "kpubdata"\nversion = "{}"\n'
)


def test_the_locked_version_is_left_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lock = _write(tmp_path / "uv.lock", _LOCK.format("0.7.1"))
    releases = {"0.7.0": _FILE, "0.7.1": _FILE, "0.7.2": _FILE}

    assert _run(tmp_path, releases, "--exclude-locked", str(lock)) == 0
    assert json.loads(capsys.readouterr().out) == ["0.7.0", "0.7.2"]


def test_only_the_locked_version_leaves_an_empty_matrix(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = _write(tmp_path / "uv.lock", _LOCK.format("0.7.0"))

    assert _run(tmp_path, {"0.7.0": _FILE}, "--exclude-locked", str(lock)) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_without_the_option_the_locked_version_stays(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(tmp_path, {"0.7.0": _FILE}) == 0
    assert json.loads(capsys.readouterr().out) == ["0.7.0"]


def test_the_floor_is_checked_before_the_locked_version_is_left_out(tmp_path: Path) -> None:
    lock = _write(tmp_path / "uv.lock", _LOCK.format("0.7.1"))

    with pytest.raises(SystemExit, match="floor 0.7.0"):
        _run(tmp_path, {"0.7.1": _FILE}, "--exclude-locked", str(lock))


def test_a_lock_without_kpubdata_fails(tmp_path: Path) -> None:
    lock = _write(tmp_path / "uv.lock", '[[package]]\nname = "other"\nversion = "1.0"\n')

    with pytest.raises(SystemExit, match="no kpubdata package"):
        matrix.locked_version(lock)


def test_the_repository_lock_resolves_a_version_inside_the_declared_range() -> None:
    # The test matrix is what covers the locked version once it is left out here, so it
    # must be one the range admits.
    locked = matrix.locked_version(_ROOT / "uv.lock")

    assert locked in matrix.declared_range(_ROOT / "pyproject.toml")

"""CI tests Builder against every released kpubdata in the declared range (#832).

The matrix is only as good as this list: a version it drops silently is a version
nobody tests. Mostly negative tests.
"""

from __future__ import annotations

import importlib.util
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

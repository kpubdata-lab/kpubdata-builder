"""The version gate has to actually refuse (#690).

Measured 2026-09-27: pyproject.toml declared 0.4.0.dev0 while the newest tag was
v0.1.0. The image tag comes from the git tag and the version inside the image comes
from the package metadata, so an image labelled v0.1.0 could contain 0.4.0.dev0 and
nothing objected. These are mostly negative tests: a gate nobody has watched fail is
a gate nobody knows works.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_version_consistency.py"


def _load(repo_root: Path) -> Any:
    """Load the checker pointed at a temporary repository."""
    spec = importlib.util.spec_from_file_location("_version_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO_ROOT = repo_root
    return module


def _write(repo: Path, *, version: str, changelog: str) -> None:
    """Write the two files the checker reads."""
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "{version}"\n', encoding="utf-8"
    )
    (repo / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## v{changelog}\n\n- something\n", encoding="utf-8"
    )


def test_matching_sources_pass(tmp_path: Path) -> None:
    """The case the gate must not break."""
    _write(tmp_path, version="0.4.0", changelog="0.4")
    module = _load(tmp_path)

    assert module.main([]) == 0


def test_a_changelog_behind_the_version_fails(tmp_path: Path) -> None:
    """A stale changelog heading is a disagreement, not a detail."""
    _write(tmp_path, version="0.4.0", changelog="0.1")
    module = _load(tmp_path)

    assert module.main([]) == 1


def test_a_tag_that_does_not_match_the_version_fails(tmp_path: Path) -> None:
    """The exact defect: tag v0.1.0 while the package says 0.4.0."""
    _write(tmp_path, version="0.4.0", changelog="0.4")
    module = _load(tmp_path)

    assert module.main(["--tag", "v0.1.0"]) == 1


def test_a_matching_tag_passes(tmp_path: Path) -> None:
    """A release that agrees with itself is allowed through."""
    _write(tmp_path, version="0.4.0", changelog="0.4")
    module = _load(tmp_path)

    assert module.main(["--tag", "v0.4.0"]) == 0


@pytest.mark.parametrize("version", ["0.4.0.dev0", "0.4.0a1", "0.4.0b2", "0.4.0rc1"])
def test_a_development_version_cannot_be_tagged(tmp_path: Path, version: str) -> None:
    """Publishing a version that says it is unfinished contradicts its declaration.

    This is the state the repository is in right now, so the gate refuses every tag
    until the version is finalised — which is the correct answer, not an obstacle.
    """
    _write(tmp_path, version=version, changelog="0.4")
    module = _load(tmp_path)

    assert module.main(["--tag", f"v{version}"]) == 1
    # Without a tag it still passes: developing on a dev version is normal.
    assert module.main([]) == 0


def test_a_missing_version_fails_rather_than_defaulting(tmp_path: Path) -> None:
    """A pyproject without a version is a broken repository, not version zero."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## v0.4\n", encoding="utf-8")
    module = _load(tmp_path)

    assert module.main([]) == 1


def test_a_missing_changelog_heading_fails(tmp_path: Path) -> None:
    """Nothing to compare against is a failure, not a pass."""
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.4.0"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nnothing yet\n", encoding="utf-8")
    module = _load(tmp_path)

    assert module.main([]) == 1


def test_the_real_repository_is_self_consistent() -> None:
    """The check passes against this repository as it stands.

    The second assertion used to be `is_prerelease(...)`, pinning "this repository
    declares a development version" as though it were an invariant. It was a
    description of one moment — the state #690 was filed about — and it failed the
    first time that moment ended, which is the commit that declares a real version
    (#744). A test that has to be deleted before the thing it guards can happen is
    not guarding it.

    What is actually invariant is the agreement between the sources, which is what
    `main([])` checks. Whether the declared version is finished is the *subject* of
    the gate, not a fact about this repository, and every spelling of it is already
    covered against fixtures above.
    """
    module = _load(Path(__file__).resolve().parents[2])

    assert module.main([]) == 0

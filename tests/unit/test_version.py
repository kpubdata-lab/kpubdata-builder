"""Version metadata coherence regression test (#592).

`version` in `pyproject.toml` is the source of truth, and
`kpubdata_builder.__version__` derives from installed distribution metadata.
The CHANGELOG top section must describe that version line — if these three
diverge, distribution artifacts (GHCR image tags, `--version` output, manifest
`builder_version`) will claim different versions from documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kpubdata_builder import __version__

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _pyproject_version() -> str:
    # tomllib is 3.11+, but this project supports 3.10, so read via regex.
    text = (_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"', text, flags=re.MULTILINE)
    assert match is not None, "pyproject.toml 에 [project] version 이 없다"
    return match.group(1)


def _changelog_latest_version() -> str:
    text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(r"^## v(\d+\.\d+)", text, flags=re.MULTILINE)
    assert match is not None, "CHANGELOG.md 에 '## vX.Y' 절이 없다"
    return match.group(1)


def test_package_version_matches_pyproject() -> None:
    """``__version__`` must come from distribution metadata, not hardcoded."""
    if __version__ == "0.0.0+unknown":
        pytest.skip("패키지가 설치되지 않은 소스 트리 — 메타데이터를 읽을 수 없다")
    expected = _pyproject_version()
    assert __version__ == expected, (
        f"설치된 배포판 메타데이터는 {__version__}, pyproject.toml 은 {expected} 다. "
        "버전을 올린 뒤 `uv sync` 로 editable 설치 메타데이터를 갱신하라."
    )


def test_changelog_head_matches_package_version_line() -> None:
    """CHANGELOG top section and package version major.minor must match."""
    major_minor = ".".join(_pyproject_version().split(".")[:2])
    assert _changelog_latest_version() == major_minor

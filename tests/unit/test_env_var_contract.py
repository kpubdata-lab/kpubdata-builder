"""README environment variable ↔ code constant intersection test (#424).

Verifies that environment variable names appearing in README match code constants.
If a variable exists in code but not in README, new contributors won't discover it.
If a variable exists in README but not in code, it provides incorrect guidance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Scan all src/. Previously file list was maintained manually, but that list itself
# drifted — env vars of uploads/store.py and ingestion/url_fetch.py
# weren't scanned, so README comparison passed falsely both ways. What this test prevents
# is exactly that, so targets are not manually selected.
_CODE_ENV_SOURCES = [
    *sorted((_REPO_ROOT / "src" / "kpubdata_builder").rglob("*.py")),
    _REPO_ROOT / "docker-entrypoint.sh",
    _REPO_ROOT / "Dockerfile",
]

_README = _REPO_ROOT / "README.md"
_DOCS = _REPO_ROOT / "docs" / "deployment.md"

_ENV_PATTERN = re.compile(r"KPUBDATA_BUILDER_[A-Z_]+")
_QUOTED_PATTERN = re.compile(r'"(KPUBDATA_BUILDER_[A-Z_]+)"')


def _readme_env_vars() -> set[str]:
    text = _README.read_text(encoding="utf-8") + "\n" + _DOCS.read_text(encoding="utf-8")
    return set(_ENV_PATTERN.findall(text))


def _code_env_vars() -> set[str]:
    found: set[str] = set()
    for source in _CODE_ENV_SOURCES:
        if not source.exists():
            continue
        text = source.read_text(encoding="utf-8")
        found.update(_QUOTED_PATTERN.findall(text))
        found.update(_ENV_PATTERN.findall(text))
    return found


class TestEnvVarContract:
    """Verify consistency between README environment variable table and code constants (#424)."""

    def test_all_code_env_vars_are_in_readme(self) -> None:
        code_vars = _code_env_vars()
        readme_vars = _readme_env_vars()
        missing = code_vars - readme_vars
        assert not missing, (
            f"코드에 있지만 README에 없는 환경변수 (신규 기여자가 발견 불가): {sorted(missing)}"
        )

    def test_all_readme_env_vars_exist_in_code(self) -> None:
        code_vars = _code_env_vars()
        readme_vars = _readme_env_vars()
        stale = readme_vars - code_vars
        if stale:
            pytest.fail(f"README에 있지만 코드에 없는 환경변수 (잘못된 안내): {sorted(stale)}")

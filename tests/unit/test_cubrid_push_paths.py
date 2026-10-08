"""CUBRID runs only when CUBRID-touched code lands (#1182).

The pull request trigger has always been path-filtered; the main push trigger
was not, so every merge started a privileged CUBRID container even when nothing
it tests had changed. Until the backend is retired (#1094), the push filter is
the same list as the pull request filter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]

_TRIGGER_PATHS = (
    ".github/workflows/cubrid.yml",
    "pyproject.toml",
    "src/kpubdata_builder/credentials/**",
    "src/kpubdata_builder/service/app.py",
    "src/kpubdata_builder/store/**",
    "tests/cubrid/**",
    "uv.lock",
)


def _triggers() -> dict[str, Any]:
    path = _ROOT / ".github/workflows/cubrid.yml"
    # ``on`` parses as the boolean True under YAML 1.1, which is what
    # yaml.safe_load reads a workflow's trigger key as.
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    return doc[True]


def test_main_pushes_use_the_same_paths_as_pull_requests() -> None:
    assert sorted(_triggers()["push"]["paths"]) == list(_TRIGGER_PATHS)
    assert sorted(_triggers()["pull_request"]["paths"]) == list(_TRIGGER_PATHS)

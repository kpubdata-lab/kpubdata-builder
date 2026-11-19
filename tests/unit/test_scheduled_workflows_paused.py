"""Guard the owner-directed pause of all GitHub Actions cron triggers.

The Builder automation schedules were paused on 2026-11-16. Keep manual
recovery and pull-request checks available without starting background jobs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

# Every workflow must be checked, including security and monitoring workflows.
_ALL_WORKFLOWS = sorted(_WORKFLOWS.glob("*.yml"))
_PAUSED_WORKFLOWS = [
    "scheduled-weather.yml",
    "scheduled-dur.yml",
    "scheduled-real-estate.yml",
    "scheduled-tourism.yml",
    "cloudflare-ranges.yml",
    "image-rescan.yml",
    "cross-repo-contract.yml",
    "security.yml",
]


def _events(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document.get("on", document.get(True))


@pytest.mark.parametrize("path", _ALL_WORKFLOWS, ids=lambda path: path.name)
def test_no_workflow_has_an_automatic_cron_trigger(path: Path) -> None:
    assert "schedule" not in _events(path), path.name


@pytest.mark.parametrize("name", _PAUSED_WORKFLOWS)
def test_paused_workflow_still_supports_manual_dispatch(name: str) -> None:
    assert "workflow_dispatch" in _events(_WORKFLOWS / name), name

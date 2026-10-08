"""The release job may only tag what main declared (#1188).

`release.yml` can be dispatched from any branch, and its checkout pins
`github.ref` on that path. Without a main guard, a dispatch from a feature
branch would tag that branch's tree as if it were the release. kpubdata's
release workflow already carries this guard; this holds Builder's in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _release_job() -> Any:
    doc = yaml.safe_load((_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    return doc["jobs"]["release"]


def test_a_manual_release_must_stand_on_main() -> None:
    condition = str(_release_job()["if"])
    dispatch_clause = condition.split("||")[0]
    assert "github.event_name == 'workflow_dispatch'" in dispatch_clause
    assert "github.ref == 'refs/heads/main'" in dispatch_clause


def test_a_merged_release_pull_request_still_releases() -> None:
    condition = str(_release_job()["if"])
    merge_clause = condition.split("||")[1]
    assert "github.event.pull_request.merged == true" in merge_clause
    assert "startsWith(github.event.pull_request.head.ref, 'release/')" in merge_clause

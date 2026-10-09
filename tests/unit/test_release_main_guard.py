"""The release job may only tag what main declared (#1188).

`release.yml` can be dispatched from any branch, and its checkout pins
`github.ref` on that path. Without a main guard, a dispatch from a feature
branch would tag that branch's tree as if it were the release. The pull request
path checks out the merge commit, so a `release/*` pull request merged into some
other branch would be tagged the same way unless its base is checked too.
kpubdata's release workflow carries both guards; this holds Builder's in place.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _release_job() -> Any:
    doc = yaml.safe_load((_ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    return doc["jobs"]["release"]


def _clause(event: str) -> str:
    """The one alternative of the job's condition that is about ``event``.

    Found by the event it names, not by its position, so reordering the
    alternatives cannot make a test read the wrong one.
    """
    alternatives = str(_release_job()["if"]).split("||")
    (clause,) = [part for part in alternatives if f"github.event_name == '{event}'" in part]
    return clause


def test_the_job_starts_from_a_dispatch_or_a_merged_pull_request_only() -> None:
    assert len(str(_release_job()["if"]).split("||")) == 2


def test_a_manual_release_must_stand_on_main() -> None:
    dispatch_clause = _clause("workflow_dispatch")
    assert "inputs.mode == 'release'" in dispatch_clause
    assert "github.ref == 'refs/heads/main'" in dispatch_clause


def test_a_merged_release_pull_request_still_releases() -> None:
    merge_clause = _clause("pull_request")
    assert "github.event.pull_request.merged == true" in merge_clause
    assert "startsWith(github.event.pull_request.head.ref, 'release/')" in merge_clause


def test_a_release_pull_request_must_have_merged_into_main() -> None:
    assert "github.event.pull_request.base.ref == 'main'" in _clause("pull_request")

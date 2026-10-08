"""Every workflow job declares how long it may hold a runner (#1178).

A job without ``timeout-minutes`` can sit on a runner and a concurrency slot for
the default six hours. These checks sweep the workflows CI runs and hold the
policy in place, together with the cancellation rule: only a pull request's
newer push replaces that pull request's earlier run — a merge to main must
finish and leave its result behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]

WORKFLOWS = (
    "ci.yml",
    "docker.yml",
    "cubrid.yml",
    "cross-repo-contract.yml",
    "security.yml",
    "docs.yml",
)

PR_ONLY_CANCEL = "${{ github.event_name == 'pull_request' }}"


def _load(name: str) -> dict[str, Any]:
    path = _ROOT / ".github/workflows" / name
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_every_job_declares_a_timeout() -> None:
    for name in WORKFLOWS:
        doc = _load(name)
        missing = [job for job, spec in doc["jobs"].items() if "timeout-minutes" not in spec]
        assert missing == [], f"{name}: jobs without timeout-minutes: {missing}"


def test_only_a_pull_requests_own_run_is_cancelled() -> None:
    # docker.yml already guards its release group by event, and docs.yml never
    # cancels a Pages deploy; the four ref-group workflows cancel pull requests
    # only.
    for name in ("ci.yml", "cubrid.yml", "cross-repo-contract.yml", "security.yml"):
        concurrency = _load(name)["concurrency"]
        assert concurrency["cancel-in-progress"] == PR_ONLY_CANCEL, name

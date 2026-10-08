"""Every job a workflow runs itself states how long it may take (#1178).

A job without ``timeout-minutes`` may run for GitHub's default of six hours, so one
hung test held a runner and a concurrency slot for that long. A job that calls a
reusable workflow cannot carry the key; the jobs of the workflow it calls do.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _without_timeout(workflow: dict[Any, Any]) -> list[str]:
    jobs = cast(dict[str, dict[str, Any]], workflow.get("jobs", {}))
    return [
        name for name, job in jobs.items() if "uses" not in job and "timeout-minutes" not in job
    ]


def test_every_job_has_a_timeout() -> None:
    files = sorted(_WORKFLOWS.glob("*.yml")) + sorted(_WORKFLOWS.glob("*.yaml"))
    assert files, "no workflow was read"

    missing = {
        path.name: names
        for path in files
        if (names := _without_timeout(yaml.safe_load(path.read_text(encoding="utf-8"))))
    }

    assert missing == {}


def test_the_check_sees_a_job_without_one_and_passes_a_reusable_call() -> None:
    workflow = {
        "jobs": {
            "bounded": {"runs-on": "ubuntu-latest", "timeout-minutes": 5},
            "unbounded": {"runs-on": "ubuntu-latest"},
            "called": {"uses": "./.github/workflows/publish-dataset.yml"},
        }
    }

    assert _without_timeout(workflow) == ["unbounded"]

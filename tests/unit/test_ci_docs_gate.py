"""A pull request builds the documentation site in strict mode (#1026).

``mkdocs build --strict`` ran only in ``docs.yml``, on a push to ``main``: a link that
could not be resolved aborted the deploy after it had merged. The build is a CI job now,
and the ``CI gate`` waits for it — a job the gate does not need can fail without
stopping a merge.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

_CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict[str, dict[str, object]]:
    workflow = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    return cast(dict[str, dict[str, object]], workflow["jobs"])


def _commands(job: dict[str, object]) -> list[str]:
    steps = cast(list[dict[str, object]], job["steps"])
    return [str(step["run"]) for step in steps if "run" in step]


def test_ci_builds_the_docs_in_strict_mode() -> None:
    commands = _commands(_jobs()["docs"])

    assert any("mkdocs build --strict" in command for command in commands)


def test_the_gate_waits_for_the_docs_build() -> None:
    assert "docs" in cast(list[str], _jobs()["gate"]["needs"])


def test_the_ci_job_and_the_deploy_build_the_same_way() -> None:
    """The deploy must not pass a build the pull request would have failed, or the reverse."""
    deploy = yaml.safe_load((_CI.parent / "docs.yml").read_text(encoding="utf-8"))
    deployed = [command for command in _commands(deploy["jobs"]["deploy"]) if "mkdocs" in command]
    checked = [command for command in _commands(_jobs()["docs"]) if "mkdocs" in command]

    assert checked == deployed

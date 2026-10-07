"""A failed scheduled publish is reported in this repository, once per workflow (#1143).

The five scheduled workflows each ran an inline ``gh issue create`` that named no
repository, on a runner without a checkout, so ``gh`` had no repository to file in and
the notice itself could fail. Every failure also opened a new issue, and the body
printed ``\\n`` literally. The notice is now one reusable workflow; its script runs here
against a ``gh`` that records what it was asked to do.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_WORKFLOWS = Path(__file__).parents[2] / ".github" / "workflows"
_SCHEDULED = sorted(_WORKFLOWS.glob("scheduled-*.yml"))
_INCIDENT = _WORKFLOWS / "freshness-incident.yml"


def _load(path: Path) -> dict[Any, Any]:
    return cast(dict[Any, Any], yaml.safe_load(path.read_text(encoding="utf-8")))


def test_there_are_five_scheduled_workflows() -> None:
    assert [p.name for p in _SCHEDULED] == [
        "scheduled-air-quality.yml",
        "scheduled-dur.yml",
        "scheduled-real-estate.yml",
        "scheduled-tourism.yml",
        "scheduled-weather.yml",
    ]


@pytest.mark.parametrize("path", _SCHEDULED, ids=lambda p: p.stem)
def test_each_scheduled_workflow_reports_a_failure_of_any_job(path: Path) -> None:
    jobs = _load(path)["jobs"]
    notify = jobs["notify-failure"]

    assert notify["uses"] == "./.github/workflows/freshness-incident.yml"
    assert notify["if"] == "${{ failure() }}"
    assert notify["with"] == {"workflow": "${{ github.workflow }}"}
    assert notify["permissions"] == {"contents": "read", "issues": "write"}
    needs = notify["needs"] if isinstance(notify["needs"], list) else [notify["needs"]]
    assert sorted(needs) == sorted(name for name in jobs if name != "notify-failure")


def test_the_incident_job_names_the_repository() -> None:
    (step,) = _load(_INCIDENT)["jobs"]["report"]["steps"]

    assert step["env"]["GH_REPO"] == "${{ github.repository }}"
    assert "actions/checkout" not in _INCIDENT.read_text(encoding="utf-8")


# ----------------------------------------------------------------- the script, run


_FAKE_GH = """#!/usr/bin/env bash
# Records each call; answers `issue list` from $FAKE_ISSUES; fails when asked to.
printf '%s\\n' "$*" >> "$FAKE_LOG"
printf 'GH_REPO=%s\\n' "${GH_REPO:-}" >> "$FAKE_LOG"
if [ -n "${FAKE_FAIL:-}" ] && [[ "$*" == *"$FAKE_FAIL"* ]]; then
  echo "gh: HTTP 403" >&2
  exit 1
fi
case "$1 $2" in
  "issue list") cat "$FAKE_ISSUES" ;;
  "issue create"|"issue comment")
    while [ $# -gt 0 ]; do
      if [ "$1" = "--body-file" ]; then cp "$2" "$FAKE_BODY"; fi
      shift
    done ;;
esac
"""


def _script() -> str:
    (step,) = _load(_INCIDENT)["jobs"]["report"]["steps"]
    return cast(str, step["run"])


def _run(
    tmp_path: Path, issues: list[dict[str, object]], fail: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(_FAKE_GH, encoding="utf-8")
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(tmp_path / "log"),
        "FAKE_ISSUES": str(tmp_path / "issues.json"),
        "FAKE_BODY": str(tmp_path / "body"),
        "FAKE_FAIL": fail,
        "GH_TOKEN": "token",
        "GH_REPO": "kpubdata-lab/kpubdata-builder",
        "WORKFLOW": "Scheduled: Air Quality",
        "RUN_URL": "https://github.com/kpubdata-lab/kpubdata-builder/actions/runs/1",
    }
    result = subprocess.run(
        ["bash", "-c", _script()], env=env, capture_output=True, text=True, check=False
    )
    log = (tmp_path / "log").read_text(encoding="utf-8").splitlines()
    body = (tmp_path / "body").read_text(encoding="utf-8") if (tmp_path / "body").exists() else ""
    return result, log, body


pytestmark_needs_tools = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None, reason="needs bash and jq"
)

_TITLE = "fix(freshness): Scheduled: Air Quality failed"


@pytestmark_needs_tools
def test_a_first_failure_opens_an_issue_with_real_line_breaks(tmp_path: Path) -> None:
    result, log, body = _run(tmp_path, [])

    assert result.returncode == 0, result.stderr
    assert log[2].startswith(f"issue create --title {_TITLE} --body-file ")
    assert all(line == "GH_REPO=kpubdata-lab/kpubdata-builder" for line in log[1::2])
    assert body == (
        "Scheduled dataset publish failed.\n\n"
        "Run: https://github.com/kpubdata-lab/kpubdata-builder/actions/runs/1\n\n"
        "Follow DATA_FRESHNESS.md escalation policy before re-running or backfilling.\n"
    )


@pytestmark_needs_tools
def test_a_repeated_failure_comments_on_the_open_issue(tmp_path: Path) -> None:
    open_issues = [
        {"number": 7, "title": _TITLE + " again"},  # a near miss is not the same issue
        {"number": 12, "title": _TITLE},
    ]

    result, log, body = _run(tmp_path, open_issues)

    assert result.returncode == 0, result.stderr
    assert log[2].startswith("issue comment 12 --body-file ")
    assert "Run: https://github.com" in body
    assert not any(line.startswith("issue create") for line in log)


@pytestmark_needs_tools
@pytest.mark.parametrize("failing", ["issue list", "issue create"])
def test_a_notice_that_cannot_be_filed_fails_the_job(tmp_path: Path, failing: str) -> None:
    result, _, _ = _run(tmp_path, [], fail=failing)

    assert result.returncode != 0
    assert "HTTP 403" in result.stderr

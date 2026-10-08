"""`CI gate` passes only when every job it covers succeeded, with one named skip.

The gate is the one required check (studio#416), so whatever it lets through merges.
min-deps is skipped when no released kpubdata is left beside the locked one — the
Tests matrix runs that one — and the gate accepts exactly that skip. This runs the
gate's own shell, in bash with jq as on the runner, with the ``needs`` context GitHub
would hand it. Mostly negative tests.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

_CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _jobs() -> dict[str, Any]:
    workflow = cast(dict[Any, Any], yaml.safe_load(_CI.read_text(encoding="utf-8")))
    return cast(dict[str, Any], workflow["jobs"])


def _gate_script() -> str:
    (step,) = [s for s in _jobs()["gate"]["steps"] if "run" in s]
    return cast(str, step["run"])


def _needs(**overrides: str) -> dict[str, Any]:
    needs: dict[str, Any] = {
        job: {"result": "success", "outputs": {}} for job in _jobs()["gate"]["needs"]
    }
    needs["kpubdata-releases"]["outputs"] = {"versions": '["0.9.0"]'}
    for job, result in overrides.items():
        needs[job.replace("_", "-")]["result"] = result
    return needs


def _run(needs: dict[str, Any]) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "NEEDS": json.dumps(needs)}
    return subprocess.run(
        ["bash", "-e", "-c", _gate_script()],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _error(completed: subprocess.CompletedProcess[str]) -> str:
    errors = [line for line in completed.stdout.splitlines() if line.startswith("::error::")]
    assert len(errors) == 1, completed.stdout
    return errors[0]


def test_every_success_passes() -> None:
    assert _run(_needs()).returncode == 0


@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped"])
@pytest.mark.parametrize("job", ["lint", "test", "min-deps", "kpubdata-releases"])
def test_anything_but_success_fails(job: str, result: str) -> None:
    completed = _run(_needs(**{job: result}))

    assert completed.returncode == 1
    assert f"{job}={result}" in _error(completed)


def test_min_deps_skipped_for_an_empty_release_list_passes() -> None:
    needs = _needs(min_deps="skipped")
    needs["kpubdata-releases"]["outputs"] = {"versions": "[]"}

    completed = _run(needs)

    assert completed.returncode == 0
    assert "min-deps skipped" in completed.stdout


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_min_deps_failing_on_an_empty_release_list_still_fails(result: str) -> None:
    needs = _needs(min_deps=result)
    needs["kpubdata-releases"]["outputs"] = {"versions": "[]"}

    assert _run(needs).returncode == 1


def test_min_deps_skipped_because_the_list_failed_fails() -> None:
    needs = _needs(kpubdata_releases="failure", min_deps="skipped")

    completed = _run(needs)

    assert completed.returncode == 1
    assert "min-deps=skipped" in _error(completed)


def test_another_job_skipped_on_an_empty_release_list_fails() -> None:
    needs = _needs(lint="skipped", min_deps="skipped")
    needs["kpubdata-releases"]["outputs"] = {"versions": "[]"}

    completed = _run(needs)

    assert completed.returncode == 1
    error = _error(completed)
    assert "lint=skipped" in error
    assert "min-deps" not in error


@pytest.mark.parametrize("needs", ["", "{}", "null"], ids=["empty", "no-jobs", "null"])
def test_no_upstream_results_fails(needs: str) -> None:
    env = {**os.environ, "NEEDS": needs}
    completed = subprocess.run(
        ["bash", "-e", "-c", _gate_script()], env=env, capture_output=True, text=True, check=False
    )

    assert completed.returncode != 0
    assert "every upstream job succeeded" not in completed.stdout


def test_min_deps_is_skipped_only_for_an_empty_list() -> None:
    assert _jobs()["min-deps"]["if"] == "needs.kpubdata-releases.outputs.versions != '[]'"


def test_the_coverage_gate_runs_on_one_test_leg() -> None:
    # The separate coverage job ran the whole suite a second time; the 3.12 leg measures
    # it now, and the floor still comes from pyproject alone.
    jobs = _jobs()
    commands = [s for s in jobs["test"]["steps"] if "--cov" in str(s.get("run", ""))]

    assert "coverage" not in jobs
    assert len(commands) == 1
    assert commands[0]["if"] == "matrix.python-version == '3.12'"
    assert "3.12" in jobs["test"]["strategy"]["matrix"]["python-version"]
    assert "--cov-fail-under" not in commands[0]["run"]

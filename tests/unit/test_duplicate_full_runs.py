"""One pull request stops paying for full suite runs it already bought (#1179).

Three repeats carried no new information: the Coverage gate re-ran the whole
suite to add --cov (it rides the 3.12 leg now), min-deps tested the version the
lock already pins into the plain test matrix, and the cross-repo sweep ran
tests/integration twice in one job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[2]


def _ci() -> dict[str, Any]:
    return yaml.safe_load((_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))


def _ci_text() -> str:
    return (_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_coverage_rides_the_312_leg() -> None:
    doc = _ci()
    assert "coverage" not in doc["jobs"]
    matrix = doc["jobs"]["test"]["strategy"]["matrix"]
    assert {"python-version": "3.12", "coverage": "--cov --cov-report=term-missing"} in matrix["include"]
    assert "COVERAGE_ARGS: ${{ matrix.coverage }}" in _ci_text()
    assert "coverage" not in doc["jobs"]["gate"]["needs"]


def test_min_deps_skips_the_locked_version() -> None:
    doc = _ci()
    job = doc["jobs"]["min-deps"]
    assert job["if"] == "needs.kpubdata-releases.outputs.versions != '[]'"
    text = _ci_text()
    assert 'locked=$(awk' in text and "if v != sys.argv[1]" in text


def test_the_cross_repo_sweep_runs_integration_once() -> None:
    text = (_ROOT / ".github/workflows/cross-repo-contract.yml").read_text(encoding="utf-8")
    assert "pytest tests/integration" in text
    assert "pytest -q --ignore=tests/integration" in text

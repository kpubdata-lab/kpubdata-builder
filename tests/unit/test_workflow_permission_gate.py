"""The workflow permission gate has to actually refuse (#1336).

A pull request job runs code nobody has reviewed, so its token must not write. A gate
nobody has watched fail is a gate nobody knows works: most of these hand it a workflow
with a right too many and expect a problem.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_workflow_permissions.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_workflow_permission_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load()

#: docker.yml before #1336: the write scopes sat on the workflow, so the build had them.
_CREEPING = """\
on:
  pull_request:
  push:
permissions:
  contents: read
  packages: write
  id-token: write
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: docker build .
  publish:
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - run: docker push
"""

_CLEAN = """\
on:
  pull_request:
  push:
permissions:
  contents: read
jobs:
  build:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - run: docker build .
  publish:
    if: github.event_name != 'pull_request'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
      id-token: write
    steps:
      - run: docker push
"""

_PUBLISH_ALLOWED = {"wf.yml:publish": (frozenset({"packages", "id-token"}), "publishes")}


def _jobs(tmp_path: Path, **files: str) -> list[Any]:
    paths = []
    for stem, text in files.items():
        path = tmp_path / f"{stem}.yml"
        path.write_text(text, encoding="utf-8")
        paths.append(path)
    return list(gate.effective_jobs(gate.load(paths)))


def _by_key(jobs: list[Any]) -> dict[str, Any]:
    return {job.key: job for job in jobs}


def test_a_build_job_that_inherits_write_scopes_is_refused(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path, wf=_CREEPING)

    problems = gate.check(jobs, _PUBLISH_ALLOWED)

    assert problems == [
        "wf.yml:build: a pull request can start this job with id-token: write, packages: write"
    ]


def test_the_same_workflow_with_the_writes_in_the_publish_job_passes(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path, wf=_CLEAN)

    assert _by_key(jobs)["wf.yml:build"].permissions == {"contents": "read"}
    assert gate.check(jobs, _PUBLISH_ALLOWED) == []


def test_the_command_exits_1_on_the_creeping_file_and_names_the_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "wf.yml"
    path.write_text(_CREEPING, encoding="utf-8")

    assert gate.main([str(path)]) == 1
    assert "wf.yml:build" in capsys.readouterr().err


def test_a_job_level_block_replaces_the_workflow_block(tmp_path: Path) -> None:
    text = _CREEPING.replace(
        "  build:\n    runs-on: ubuntu-latest\n",
        "  build:\n    runs-on: ubuntu-latest\n    permissions:\n      packages: read\n",
    )

    build = _by_key(_jobs(tmp_path, wf=text))["wf.yml:build"]

    # `contents` is not inherited: what a job-level block leaves out is `none`.
    assert build.permissions == {"packages": "read"}
    assert build.writes == frozenset()


@pytest.mark.parametrize(
    ("permissions", "problem"),
    [
        ("permissions: write-all\n", "contents: write"),
        ("permissions:\n  id-token: write\n", "id-token: write"),
        ("permissions:\n  contents: read\n  pull-requests: write\n", "pull-requests: write"),
        ("", "no `permissions` on the job or the workflow"),
    ],
)
def test_each_kind_of_excess_is_refused(tmp_path: Path, permissions: str, problem: str) -> None:
    text = f"on: [pull_request]\n{permissions}jobs:\n  a:\n    steps:\n      - run: make\n"

    problems = gate.check(_jobs(tmp_path, wf=text), {})

    assert len(problems) == 1
    assert problem in problems[0]


@pytest.mark.parametrize("permissions", ["read-all", "{}", "{contents: read, packages: read}"])
def test_read_only_grants_pass(tmp_path: Path, permissions: str) -> None:
    text = f"on: pull_request\npermissions: {permissions}\njobs:\n  a:\n    steps: []\n"

    assert gate.check(_jobs(tmp_path, wf=text), {}) == []


def test_a_workflow_no_pull_request_starts_may_write(tmp_path: Path) -> None:
    text = "on:\n  push:\n  schedule:\n    - cron: '0 0 * * *'\npermissions: write-all\n"
    text += "jobs:\n  a:\n    steps: []\n"

    jobs = _jobs(tmp_path, wf=text)

    assert [job.pull_request for job in jobs] == [False]
    assert gate.check(jobs, {}) == []


_CALLER = """\
on:
  pull_request:
    types: [closed]
permissions:
  contents: read
jobs:
  image:
    uses: ./.github/workflows/called.yml
    permissions:
{grants}
"""

_CALLED = """\
on:
  workflow_call:
permissions:
  contents: read
  packages: write
jobs:
  build:
    steps: []
"""


def test_a_called_workflow_is_reached_through_its_caller(tmp_path: Path) -> None:
    caller = _CALLER.format(grants="      contents: read\n      packages: write")

    jobs = _by_key(_jobs(tmp_path, caller=caller, called=_CALLED))

    assert jobs["called.yml:build"].pull_request is True
    assert gate.check(list(jobs.values()), {}) == [
        "called.yml:build: a pull request can start this job with packages: write",
        "caller.yml:image: a pull request can start this job with packages: write",
    ]


def test_a_called_job_holds_no_more_than_its_caller_passes(tmp_path: Path) -> None:
    caller = _CALLER.format(grants="      contents: read")

    jobs = _by_key(_jobs(tmp_path, caller=caller, called=_CALLED))

    # The called workflow asks for packages: write; the caller does not hold it.
    assert jobs["called.yml:build"].permissions == {"contents": "read", "packages": "none"}
    assert gate.check(list(jobs.values()), {}) == []


def test_an_allowance_covers_only_the_scopes_it_names(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path, wf=_CLEAN)

    problems = gate.check(jobs, {"wf.yml:publish": (frozenset({"packages"}), "publishes")})

    assert problems == ["wf.yml:publish: a pull request can start this job with id-token: write"]


def test_a_stale_allowance_is_refused(tmp_path: Path) -> None:
    jobs = _jobs(tmp_path, wf=_CLEAN)
    allowed = {
        "wf.yml:publish": (frozenset({"packages", "id-token", "contents"}), "publishes"),
        "wf.yml:gone": (frozenset({"issues"}), "a job that was removed"),
    }

    assert gate.check(jobs, allowed) == [
        "wf.yml:gone: allowed, but no pull request job has that name",
        "wf.yml:publish: allowed contents, which the job no longer holds",
    ]


# --------------------------------------------------------------- the real workflows


def _real() -> dict[str, Any]:
    return _by_key(gate.effective_jobs(gate.load(gate.default_paths())))


def test_the_repository_s_workflows_pass() -> None:
    assert gate.check(list(_real().values())) == []


def test_every_allowance_has_a_reason() -> None:
    assert all(reason.strip() for _scopes, reason in gate.ALLOWED.values())


def test_only_the_publish_job_of_the_docker_workflow_writes() -> None:
    jobs = {key: job for key, job in _real().items() if key.startswith("docker.yml:")}

    assert jobs["docker.yml:build"].permissions == {"contents": "read"}
    assert jobs["docker.yml:published-images"].writes == frozenset()
    assert {key for key, job in jobs.items() if job.writes} == {"docker.yml:publish"}
    assert jobs["docker.yml:publish"].writes == {"packages", "id-token"}


def test_release_passes_the_publish_job_what_it_needs() -> None:
    """A called workflow cannot be granted more than its caller holds."""
    jobs = _real()

    assert jobs["docker.yml:publish"].writes <= jobs["release.yml:image"].writes

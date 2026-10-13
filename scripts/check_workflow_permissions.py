#!/usr/bin/env python3
"""Refuse a pull request job whose token can write (#1336).

A workflow started by a pull request checks out and runs code nobody has reviewed yet.
The ``GITHUB_TOKEN`` that job holds should therefore be read-only: a ``write`` scope, or
``id-token: write`` (an OIDC token that other systems trust), turns a build or test step
into a way to publish, tag or comment.

What is computed, for every job of every workflow under ``.github/workflows``:

- the job's effective permissions: its own ``permissions`` block, or else the
  workflow's. A job-level block replaces the workflow's; it does not add to it.
- for a job of a reusable workflow, the same, capped by what the calling job holds: a
  called workflow is never granted more than its caller passes.
- whether a pull request event can start the job: its workflow names one under ``on``,
  or a job that such a workflow runs calls it with ``uses: ./.github/workflows/...``.

What fails:

- a job a pull request can start that holds any ``write`` scope and is not in
  ``ALLOWED`` with exactly those scopes;
- a job a pull request can start with no ``permissions`` at either level, because it
  then gets the repository's default, which this file cannot read;
- an ``ALLOWED`` entry that names a scope the job no longer holds, or a job that no
  longer exists — the allowlist only shrinks.

What is not computed: a job's ``if``. A job that skips itself on a pull request still
holds its rights if the condition is ever changed, so it is listed in ``ALLOWED`` with
the reason instead of being believed.

Usage:
    python scripts/check_workflow_permissions.py
    python scripts/check_workflow_permissions.py --show
    python scripts/check_workflow_permissions.py FILE...
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

#: Events that run a pull request's code or act for a pull request.
PULL_REQUEST_EVENTS = frozenset(
    {
        "pull_request",
        "pull_request_target",
        "pull_request_review",
        "pull_request_review_comment",
    }
)

#: Every scope a token can carry, for ``read-all`` and ``write-all``.
SCOPES = (
    "actions",
    "attestations",
    "checks",
    "contents",
    "deployments",
    "discussions",
    "id-token",
    "issues",
    "models",
    "packages",
    "pages",
    "pull-requests",
    "repository-projects",
    "security-events",
    "statuses",
)

_LEVEL = {"none": 0, "read": 1, "write": 2}
_LOCAL_PREFIX = "./.github/workflows/"

#: Pull request jobs that may hold a write scope: ``"<file>:<job>"`` to the exact
#: scopes and the reason. Add an entry only with a reason a reviewer can check, and
#: remove it when the job loses the scope — a stale entry fails the gate.
ALLOWED: dict[str, tuple[frozenset[str], str]] = {
    # --- publishing jobs that a pull request event reaches but must not run for one
    "docker.yml:publish": (
        frozenset({"packages", "id-token"}),
        "pushes the image; skipped on a pull request unless release.yml passes publish",
    ),
    "release.yml:image": (
        frozenset({"packages", "id-token"}),
        "calls docker.yml to publish, only after `release` ran for a merged release PR",
    ),
    "release.yml:release": (
        frozenset({"contents"}),
        "tags and creates the release; on a pull request only for a merged release PR",
    ),
    "release.yml:prepare": (
        frozenset({"contents", "pull-requests"}),
        "workflow_dispatch only (`if`); shares a file with the merged-PR trigger",
    ),
    "release.yml:release-please": (
        frozenset({"contents", "pull-requests"}),
        "workflow_dispatch only (`if`); shares a file with the merged-PR trigger",
    ),
    # --- existing jobs, not rewritten in #1336: follow-up candidates
    "codex-review.yml:request": (
        frozenset({"pull-requests"}),
        "comments on the pull request to ask for a review; same-repository heads only",
    ),
    "security.yml:codeql": (
        frozenset({"security-events"}),
        "uploads CodeQL results, which needs security-events: write",
    ),
    "cloudflare-ranges.yml:compare": (
        frozenset({"issues"}),
        "opens an issue on schedule; a pull request run is a dry run",
    ),
    "dataset-automation.yml:report": (
        frozenset({"issues"}),
        "opens an issue on schedule; a pull request run is a dry run",
    ),
    "image-rescan.yml:rescan": (
        frozenset({"issues"}),
        "opens issues on schedule; a pull request run is a dry run",
    ),
    "titles.yml:issue": (
        frozenset({"issues"}),
        "relabels an issue on the `issues` event; skipped (`if`) on a pull request",
    ),
}

Permissions = dict[str, str]


class Job(NamedTuple):
    """One job with the rights its token holds."""

    workflow: str
    name: str
    #: ``None`` when neither the job nor the workflow declares ``permissions``.
    permissions: Permissions | None
    pull_request: bool

    @property
    def key(self) -> str:
        return f"{self.workflow}:{self.name}"

    @property
    def writes(self) -> frozenset[str]:
        granted = self.permissions or {}
        return frozenset(scope for scope, level in granted.items() if level == "write")

    def describe(self) -> str:
        if self.permissions is None:
            return "(undeclared: repository default)"
        held = {s: v for s, v in self.permissions.items() if v != "none"}
        return ", ".join(f"{s}: {v}" for s, v in sorted(held.items())) or "(none)"


def _normalise(raw: Any) -> Permissions | None:
    """Return a ``permissions`` value as scope -> level, or ``None`` when absent."""
    if raw is None:
        return None
    if isinstance(raw, str):
        level = {"read-all": "read", "write-all": "write"}.get(raw)
        if level is None:
            raise ValueError(f"unknown permissions value: {raw!r}")
        return dict.fromkeys(SCOPES, level)
    if isinstance(raw, Mapping):
        granted = {str(scope): str(level) for scope, level in raw.items()}
        for scope, level in granted.items():
            if level not in _LEVEL:
                raise ValueError(f"unknown level for {scope}: {level!r}")
        return granted
    raise ValueError(f"unknown permissions value: {raw!r}")


def _cap(declared: Permissions | None, cap: Permissions | None) -> Permissions | None:
    """Limit ``declared`` to what a calling job holds; ``cap`` None means no caller."""
    if cap is None:
        return declared
    if declared is None:
        return dict(cap)
    return {
        scope: level if _LEVEL[level] <= _LEVEL[cap.get(scope, "none")] else cap.get(scope, "none")
        for scope, level in declared.items()
    }


def _widest(a: Permissions | None, b: Permissions | None) -> Permissions | None:
    """The larger of two grants, scope by scope; undeclared is wider than anything."""
    if a is None or b is None:
        return None
    return {
        scope: max(a.get(scope, "none"), b.get(scope, "none"), key=_LEVEL.__getitem__)
        for scope in sorted({*a, *b})
    }


def _events(workflow: Mapping[Any, Any]) -> set[str]:
    # YAML 1.1 reads the bare key `on` as the boolean True.
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return {on}
    if isinstance(on, Mapping | list):
        return {str(event) for event in on}
    return set()


def _jobs(workflow: Mapping[Any, Any]) -> dict[str, Mapping[str, Any]]:
    jobs = workflow.get("jobs") or {}
    return {str(name): job for name, job in jobs.items() if isinstance(job, Mapping)}


def _effective(
    workflow: Mapping[Any, Any], job: Mapping[str, Any], cap: Permissions | None
) -> Permissions | None:
    declared = _normalise(job.get("permissions"))
    if declared is None:
        declared = _normalise(workflow.get("permissions"))
    return _cap(declared, cap)


def _called(job: Mapping[str, Any]) -> str | None:
    """The local reusable workflow's file name a job calls, if it calls one."""
    uses = job.get("uses")
    if isinstance(uses, str) and uses.startswith(_LOCAL_PREFIX):
        return uses.removeprefix(_LOCAL_PREFIX)
    return None


def load(paths: Sequence[Path]) -> dict[str, Mapping[Any, Any]]:
    workflows: dict[str, Mapping[Any, Any]] = {}
    for path in paths:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(document, Mapping):
            workflows[path.name] = document
    return workflows


def effective_jobs(workflows: Mapping[str, Mapping[Any, Any]]) -> list[Job]:
    """Every job with its effective permissions.

    A job a pull request can start carries the widest rights any such start gives it;
    any other job carries what it holds when its own workflow is started directly.
    """
    reached: dict[str, Permissions | None] = {}
    seen: set[tuple[str, str]] = set()
    # (workflow file, what the caller holds — None for a direct trigger)
    pending: list[tuple[str, Permissions | None]] = [
        (name, None)
        for name, workflow in sorted(workflows.items())
        if _events(workflow) & PULL_REQUEST_EVENTS
    ]
    while pending:
        name, cap = pending.pop()
        mark = (name, repr(sorted(cap.items())) if cap is not None else "direct")
        workflow = workflows.get(name)
        if workflow is None or mark in seen:
            continue
        seen.add(mark)
        for job_name, job in _jobs(workflow).items():
            held = _effective(workflow, job, cap)
            key = f"{name}:{job_name}"
            reached[key] = _widest(reached[key], held) if key in reached else held
            target = _called(job)
            # An undeclared caller passes the repository default; treat it as no cap.
            if target is not None:
                pending.append((target, held))

    result: list[Job] = []
    for name, workflow in sorted(workflows.items()):
        for job_name, job in _jobs(workflow).items():
            key = f"{name}:{job_name}"
            if key in reached:
                result.append(Job(name, job_name, reached[key], True))
            else:
                result.append(Job(name, job_name, _effective(workflow, job, None), False))
    return result


def check(
    jobs: Sequence[Job],
    allowed: Mapping[str, tuple[frozenset[str], str]] | None = None,
) -> list[str]:
    """Return one line for each job that breaks the rule, and each stale allowance."""
    allowed = ALLOWED if allowed is None else allowed
    problems: list[str] = []
    writes = {job.key: job.writes for job in jobs if job.pull_request}
    for job in jobs:
        if not job.pull_request:
            continue
        if job.permissions is None:
            problems.append(
                f"{job.key}: no `permissions` on the job or the workflow; a pull request "
                "job must declare them"
            )
            continue
        extra = job.writes - allowed.get(job.key, (frozenset(), ""))[0]
        if extra:
            scopes = ", ".join(f"{scope}: write" for scope in sorted(extra))
            problems.append(f"{job.key}: a pull request can start this job with {scopes}")
    for key, (granted, _reason) in sorted(allowed.items()):
        if key not in writes:
            problems.append(f"{key}: allowed, but no pull request job has that name")
            continue
        unused = granted - writes[key]
        if unused:
            problems.append(
                f"{key}: allowed {', '.join(sorted(unused))}, which the job no longer holds"
            )
    return problems


def default_paths(root: Path = WORKFLOWS) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.suffix in {".yml", ".yaml"})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="workflows to read (default: all)")
    parser.add_argument(
        "--show", action="store_true", help="print every job's effective permissions"
    )
    args = parser.parse_args(argv)

    try:
        jobs = effective_jobs(load(list(args.files) or default_paths()))
    except (ValueError, yaml.YAMLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.show:
        for job in jobs:
            start = "pull_request" if job.pull_request else "-"
            print(f"{job.key:<44} {start:<13} {job.describe()}")

    # With named files the allowlist is cut down to them, so one file can be checked.
    names = {job.workflow for job in jobs}
    allowed = {k: v for k, v in ALLOWED.items() if k.split(":", 1)[0] in names}
    problems = check(jobs, allowed)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            f"\n{len(problems)} problem(s). Give the job its own read-only `permissions`, "
            "or move the write into a job a pull request cannot start "
            "(see scripts/check_workflow_permissions.py).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

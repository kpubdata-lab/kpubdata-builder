#!/usr/bin/env python3
"""Guard the stabilization scope of a pull request.

The canonical rule lives in kpubdata/docs/governance/POLICY.md section 1.3.
This check deliberately covers only objective indicators. A reviewer must still
identify semantically new work disguised as a fix or refactor.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

_FEATURE_TITLE = re.compile(r"^feat(?:\([^)\n]+\))?!?:\s")
_EXCEPTION = re.compile(r"(?m)^Scope exception:\s*YES\s*$")
_RISK = re.compile(r"(?m)^Existing flow or risk:[ \t]*(\S[^\r\n]{19,})[ \t]*$")
_ISSUE = re.compile(r"(?im)^(?:Closes|Fixes|Refs|Relates)\s+(?:(?:[\w.-]+/){1,2})?#\d+\b")
_ROUTE = re.compile(r"^\s{2}/\S+:\s*$")
_STUDIO_ROUTE = re.compile(r"^\s*path:\s*['\"]")
_CRON = re.compile(r"^\s*(?:-\s*)?cron\s*:")
_ISSUE_WRITER = re.compile(
    r"\bgh\s+issue\s+create\b"
    r"|[\"']issue[\"']\s*,\s*[\"']create[\"']"
    r"|\b(?:rest\.)?issues\.create\s*\("
)


def scope_indicators(repository: str, title: str, name_status: str, patch: str) -> list[str]:
    """Return objective signs of expansion, not a semantic approval decision."""
    reasons: list[str] = []
    if _FEATURE_TITLE.match(title):
        reasons.append("feat title")

    for line in name_status.splitlines():
        status, _, path = line.partition("\t")
        if status != "A":
            continue
        if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            reasons.append(f"new workflow: {path}")
        if repository.endswith("/kpubdata"):
            if re.fullmatch(r"src/kpubdata/specs/.+\.ya?ml", path):
                reasons.append(f"new dataset spec: {path}")
            if re.fullmatch(r"src/kpubdata/providers/[^/]+/.+\.py", path):
                reasons.append(f"new provider source: {path}")
        elif repository.endswith("/kpubdata-builder"):
            if re.fullmatch(r"src/kpubdata_builder/(?:service/routes|exporters)/[^/]+\.py", path):
                reasons.append(f"new route or exporter module: {path}")
        elif repository.endswith("/kpubdata-studio"):
            if re.fullmatch(r"src/pages/[^/]+\.(?:ts|tsx)", path):
                reasons.append(f"new page: {path}")

    path = ""
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("diff --git "):
            path = ""
        elif line.startswith("+") and not line.startswith("+++"):
            added = line[1:]
            if path.startswith(".github/workflows/") and _CRON.match(added):
                reasons.append(f"new schedule line: {path}")
            if (
                (path.startswith(".github/workflows/") or path.startswith("scripts/"))
                and not Path(path).name.startswith("test_")
                and _ISSUE_WRITER.search(added)
            ):
                reasons.append(f"new automatic issue creator: {path}")
            if path == "contract/builder-api.yaml" and _ROUTE.match(added):
                reasons.append("new Builder HTTP route")
            if path == "src/app/router.tsx" and _STUDIO_ROUTE.match(added):
                reasons.append("new Studio route")
            if path == "src/kpubdata/__init__.py" and re.match(r'^\s*"[^"]+",\s*$', added):
                reasons.append("new SDK public export")

    return sorted(set(reasons))


def check_scope(
    repository: str,
    title: str,
    body: str,
    labels: set[str],
    name_status: str,
    patch: str,
) -> tuple[str, ...]:
    """Pure PR-policy decision; empty tuple means permitted."""
    indicators = scope_indicators(repository, title, name_status, patch)
    if not indicators:
        return ()

    problems = ["Potential new product/operational scope: " + "; ".join(indicators)]
    exception = bool(_EXCEPTION.search(body))
    if not exception:
        problems.append("Set 'Scope exception: YES' only for a necessary existing-flow fix.")
    if "review:R3" not in labels:
        problems.append("Require review:R3 and independent human approval for an exception.")
    risk = _RISK.search(body)
    if not risk or len(risk.group(1).strip()) < 20:
        problems.append(
            "Explain the existing user flow or concrete risk in 'Existing flow or risk:'."
        )
    if not _ISSUE.search(body):
        problems.append(
            "Link the existing defect/stabilization issue with 'Refs #N' or 'Closes #N'."
        )

    return () if exception and len(problems) == 1 else tuple(problems)


def _git(args: list[str]) -> str:
    result = subprocess.run(["git", *args], check=True, text=True, capture_output=True)
    return result.stdout


def main() -> int:
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        print(
            "error: GITHUB_EVENT_PATH is required for the PR scope check",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(Path(event_path).read_text(encoding="utf-8"))
        pull = payload["pull_request"]
        title = pull["title"]
        body = pull.get("body") or ""
        labels = {row["name"] for row in pull.get("labels", [])}
        base = pull["base"]["sha"]
        repository = os.environ.get("GITHUB_REPOSITORY", "")
        if not re.fullmatch(r"[0-9a-fA-F]{40}", base):
            raise ValueError("invalid PR base SHA")
        if repository not in {
            "kpubdata-lab/kpubdata",
            "kpubdata-lab/kpubdata-builder",
            "kpubdata-lab/kpubdata-studio",
        }:
            raise ValueError("unknown repository for stabilization guard")
        _git(["fetch", "--no-tags", "--depth=1", "origin", base])
        changed = _git(["diff", "--name-status", "--no-renames", base, "HEAD"])
        patch = _git(
            [
                "diff",
                "--no-ext-diff",
                "--unified=0",
                base,
                "HEAD",
                "--",
                ".github/workflows",
                "scripts",
                "contract/builder-api.yaml",
                "src/app/router.tsx",
                "src/kpubdata/__init__.py",
            ]
        )
        problems = check_scope(repository, title, body, labels, changed, patch)
    except (KeyError, TypeError, ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: scope verification was not possible: {exc}", file=sys.stderr)
        return 2

    if problems:
        for problem in problems:
            print(f"::error::{problem}")
        print("Stabilization policy: kpubdata/docs/governance/POLICY.md section 1.3.")
        return 1
    print("stabilization scope gate: passed (review still checks semantic scope)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

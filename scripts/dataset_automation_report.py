#!/usr/bin/env python3
"""Run the agent commands on a schedule and keep one report issue (kpubdata#448).

Builder has ``discover``, ``monitor`` and ``pipeline`` commands; nothing ran them.
This runs the two that can run without writing anything, and reports:

- ``monitor --check`` over every dataset kpubdata's catalogue marks
  ``application_required``, from a state file in the work directory. A dataset it
  finds ``HEALTHY`` has had its application approved.
- ``discover`` once per data.go.kr API page those datasets name, printing the draft
  spec rather than writing it.
- ``pipeline`` is not run. It records fixtures against the live API and opens a pull
  request, even with ``--skip-pr`` it records; the report names it as the next step
  for an approved dataset instead.

Without the data.go.kr key (the ``KPUBDATA_DATAGO_API_KEY`` secret) monitor cannot
tell an approval from a refusal, so the run says the secret is not configured and
stops with exit status 0, writing nothing.

The issue follows ``image_rescan.py``: opened when there is something to act on,
left alone when its list is the same as the last run's, rewritten (with a comment)
when the list changes, and never closed here.

Exit status 0 when the commands ran or the run was skipped, 1 when a command failed
or the issue could not be written, 2 when the catalogue could not be read.

Usage:
    python3 scripts/dataset_automation_report.py --dry-run   # run and print
    python3 scripts/dataset_automation_report.py             # run, then file or update
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NamedTuple

import yaml
from image_rescan import Run, _gh, _run, open_issue

TITLE = "chore(datasets): scheduled report of dataset application approvals and spec drafts"
LABELS = ("epic:datasets",)
SECRET = "KPUBDATA_DATAGO_API_KEY"
#: The command the report runs; ``uv run`` puts it on the path.
CLI = ("kpubdata-builder",)
APPROVED = "HEALTHY"

_PAGE = re.compile(r"^https://www\.data\.go\.kr/data/\d+/openapi\.do$")
_ENDPOINT = re.compile(r"^\s*Endpoint:\s*(\S*)\s*$", re.M)
_BEGIN = re.compile(r"<!-- dataset-automation:begin fingerprint=(\w+) -->")
_END = "<!-- dataset-automation:end -->"
_BLOCK = re.compile(
    r"<!-- dataset-automation:begin fingerprint=\w+ -->.*?<!-- dataset-automation:end -->", re.S
)

_FOLLOW_UP = """\
## What to do

For an approved dataset: record its fixtures and open the pull request in kpubdata
(`kpubdata-builder pipeline <dataset>` on a machine holding the key, or
`make record DATASET=<dataset>` there), and change its level in
`dataset_metadata.json`. This workflow does neither: recording needs the live API
and a trusted runner, and opening a pull request needs a token it does not hold.

For a page with a draft: run `kpubdata-builder discover <page> --output <file>` and
review the draft before it becomes a spec.
"""


class Pending(NamedTuple):
    """A dataset the catalogue marks ``application_required``."""

    dataset_id: str
    source_url: str


class Discovery(NamedTuple):
    """What ``discover`` made of one data.go.kr page."""

    url: str
    datasets: tuple[str, ...]
    #: ``endpoint``, ``no endpoint``, ``unreachable`` or ``failed``.
    outcome: str
    detail: str


def pending_datasets() -> list[Pending]:
    """The catalogue's ``application_required`` datasets, through kpubdata's public API."""
    from kpubdata import Client, DatasetStatus

    return sorted(
        Pending(ref.id, ref.source_url or "")
        for ref in Client().datasets.list()
        if ref.status == DatasetStatus.APPLICATION_REQUIRED
    )


def run_monitor(pending: Sequence[Pending], run: Run, directory: Path) -> dict[str, str]:
    """``monitor --check`` over ``pending``; each dataset's status after the check."""
    state_file = directory / "monitor-state.yaml"
    seed = [{"dataset_id": each.dataset_id, "status": "NEEDS_APPLICATION"} for each in pending]
    state_file.write_text(yaml.safe_dump({"pending": seed}), encoding="utf-8")
    result = run([*CLI, "monitor", "--state-file", str(state_file), "--check"])
    if result.returncode != 0:
        tail = " | ".join(result.stderr.strip().splitlines()[-3:])
        raise RuntimeError(f"monitor --check exited {result.returncode}: {tail}")
    checked = yaml.safe_load(state_file.read_text(encoding="utf-8")) or {}
    return {str(item["dataset_id"]): str(item["status"]) for item in checked.get("pending", [])}


def run_discover(pending: Sequence[Pending], run: Run, *, limit: int) -> list[Discovery]:
    """``discover`` once per API page; the draft is printed by the command, not kept."""
    pages: dict[str, list[str]] = {}
    for each in pending:
        if _PAGE.match(each.source_url):
            pages.setdefault(each.source_url, []).append(each.dataset_id)
    found = []
    for url in sorted(pages)[:limit]:
        result = run([*CLI, "discover", url])
        endpoint = _ENDPOINT.search(result.stdout)
        if result.returncode == 3:
            outcome, detail = "unreachable", result.stderr.strip()[:200]
        elif result.returncode != 0:
            outcome, detail = "failed", result.stderr.strip()[:200]
        elif endpoint and endpoint.group(1) not in ("", "/"):
            outcome, detail = "endpoint", endpoint.group(1)
        else:
            outcome, detail = "no endpoint", "the page names no apis.data.go.kr address"
        found.append(Discovery(url, tuple(pages[url]), outcome, detail))
    return found


def approved(statuses: Mapping[str, str]) -> list[str]:
    return sorted(dataset for dataset, status in statuses.items() if status == APPROVED)


def actionable(statuses: Mapping[str, str], discoveries: Sequence[Discovery]) -> bool:
    """Something a person can act on: an approval, or a page with a draft."""
    return bool(approved(statuses)) or any(d.outcome == "endpoint" for d in discoveries)


def fingerprint(statuses: Mapping[str, str], discoveries: Sequence[Discovery]) -> str:
    """One value per report: the approvals and each page's outcome, in any order.

    Other statuses (a rate limit, a timeout) are left out so that a night's noise
    does not rewrite the issue.
    """
    lines = sorted(
        [f"approved {each}" for each in approved(statuses)]
        + [f"page {d.url} {d.outcome}" for d in discoveries]
    )
    if not lines:
        return "none"
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def render_block(
    statuses: Mapping[str, str],
    discoveries: Sequence[Discovery],
    *,
    today: str,
    run_url: str,
) -> str:
    """The part of the issue this script owns, between its two markers."""
    lines = [
        f"<!-- dataset-automation:begin fingerprint={fingerprint(statuses, discoveries)} -->",
        "The scheduled run of `kpubdata-builder monitor --check` and `discover` over the "
        "datasets kpubdata's catalogue marks `application_required`.",
        "",
        f"- Last changed on {today}" + (f" by {run_url}" if run_url else ""),
    ]
    counts = Counter(statuses.values())
    summary = ", ".join(f"{status} {counts[status]}" for status in sorted(counts))
    lines.append(f"- Monitor statuses: {summary or 'no pending dataset'}")
    ready = approved(statuses)
    lines += ["", "### Approved (monitor answered HEALTHY)", ""]
    lines += [f"- `{each}`" for each in ready] or ["None."]
    lines += ["", "### Spec drafts (discover)", ""]
    if discoveries:
        lines += ["| Page | Datasets | Outcome | Detail |", "|---|---|---|---|"]
        for d in discoveries:
            names = ", ".join(f"`{each}`" for each in d.datasets)
            detail = d.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {d.url} | {names} | {d.outcome} | {detail} |")
    else:
        lines.append("No data.go.kr API page to discover.")
    lines.append(_END)
    return "\n".join(lines)


def replace_block(body: str, block: str) -> str:
    """``body`` with this script's part replaced; everything else as it was."""
    if _BLOCK.search(body):
        return _BLOCK.sub(lambda _: block, body, count=1)
    return f"{block}\n\n{body}"


def record(
    statuses: Mapping[str, str],
    discoveries: Sequence[Discovery],
    run: Run,
    *,
    today: str,
    run_url: str,
    directory: Path,
) -> str:
    """File or update the issue; return what was done, in a line."""
    block = render_block(statuses, discoveries, today=today, run_url=run_url)
    issue = open_issue(TITLE, run)
    body_file = directory / "dataset-automation-body.md"
    if issue is None:
        if not actionable(statuses, discoveries):
            return "nothing to act on and no open issue"
        body_file.write_text(f"{block}\n\n{_FOLLOW_UP}", encoding="utf-8")
        labels = [part for label in LABELS for part in ("--label", label)]
        created = _gh(
            run, "issue", "create", "--title", TITLE, "--body-file", str(body_file), *labels
        )
        return f"opened {created.strip()}"
    number = str(issue["number"])
    recorded = _BEGIN.search(issue["body"] or "")
    if recorded and recorded.group(1) == fingerprint(statuses, discoveries):
        return f"#{number} already has this report"
    body_file.write_text(replace_block(issue["body"] or "", block), encoding="utf-8")
    _gh(run, "issue", "edit", number, "--body-file", str(body_file))
    body_file.write_text("The report changed; the lists above are today's.", encoding="utf-8")
    _gh(run, "issue", "comment", number, "--body-file", str(body_file))
    return f"updated #{number}"


def skip_without_secret(env: Mapping[str, str]) -> bool:
    """Say the secret is missing, in the log and the run's summary; ``True`` to stop."""
    if env.get(SECRET, "").strip():
        return False
    message = (
        f"secret {SECRET} is not configured, so monitor cannot tell an approval from a "
        "refusal. Skipped; nothing was run or written. Register the secret to enable "
        "this report (kpubdata#900)."
    )
    print(f"::notice title=Dataset automation report skipped::{message}")
    summary = env.get("GITHUB_STEP_SUMMARY", "")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Dataset automation report skipped\n\n{message}\n")
    return True


def main(
    argv: Sequence[str] | None = None,
    run: Run = _run,
    env: Mapping[str, str] = os.environ,
    catalogue: Callable[[], list[Pending]] = pending_datasets,
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="run and print; write no issue")
    parser.add_argument("--run-url", default="", help="the workflow run, for the issue")
    parser.add_argument("--work-dir", type=Path, default=Path("."), help="where to write files")
    parser.add_argument(
        "--discover-limit", type=int, default=30, help="data.go.kr pages to discover, at most"
    )
    args = parser.parse_args(argv)

    if skip_without_secret(env):
        return 0
    try:
        pending = catalogue()
    except Exception as error:  # noqa: BLE001 - any failure to read is reported the same way
        print(f"::error::could not read kpubdata's catalogue: {error}")
        return 2
    print(f"{len(pending)} dataset(s) marked application_required")

    try:
        statuses = run_monitor(pending, run, args.work_dir) if pending else {}
    except RuntimeError as error:
        print(f"::error::{error}")
        return 1
    discoveries = run_discover(pending, run, limit=args.discover_limit)
    for each in approved(statuses):
        print(f"approved: {each}")
    for d in discoveries:
        print(f"discover {d.url}: {d.outcome}")

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    if args.dry_run:
        print(render_block(statuses, discoveries, today=today, run_url=args.run_url))
        return 0
    try:
        print(
            record(
                statuses,
                discoveries,
                run,
                today=today,
                run_url=args.run_url,
                directory=args.work_dir,
            )
        )
    except RuntimeError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

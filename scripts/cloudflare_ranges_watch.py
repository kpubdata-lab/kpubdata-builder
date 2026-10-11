#!/usr/bin/env python3
"""Keep one issue open while Cloudflare's ranges differ from Caddy's list (#1236).

``check_cloudflare_ranges.py`` compares ``ops/caddy/trusted_proxies.caddy`` with the
ranges Cloudflare publishes. Nobody ran it on a schedule. This runs it and records
the result in one issue:

- no open issue, and a difference: one is opened;
- an open issue whose list is the same as today's: nothing is written;
- an open issue whose list differs: the list is rewritten and a comment says so;
- an open issue and no difference today: the list is rewritten to say so. The issue
  is not closed here: the file is fixed by a pull request and a person closes it.

Exit status 0 when the check ran (a difference is reported, not a failure), 1 when
the issue could not be written, 2 when the published lists could not be read.

Usage:
    python3 scripts/cloudflare_ranges_watch.py --dry-run   # compare and print
    python3 scripts/cloudflare_ranges_watch.py             # compare, then file or update
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import check_cloudflare_ranges as ranges
from image_rescan import Run, _gh, _run, open_issue

TITLE = "chore(deploy): Cloudflare's published ranges differ from the Caddy trusted proxies"
LABELS = ("epic:byok",)

_BEGIN = re.compile(r"<!-- cloudflare-ranges:begin fingerprint=(\w+) -->")
_END = "<!-- cloudflare-ranges:end -->"
_BLOCK = re.compile(
    r"<!-- cloudflare-ranges:begin fingerprint=\w+ -->.*?<!-- cloudflare-ranges:end -->", re.S
)

_FOLLOW_UP = """\
## What to do

1. `python3 scripts/check_cloudflare_ranges.py --write`, then open a pull request with
   the rewritten `ops/caddy/trusted_proxies.caddy`.
2. After it is deployed: `caddy reload` on the VM, and the firewall's allowed ranges
   set to the same list (`docs/deploy.md`, "Cloudflare 대역 갱신과 롤백").
"""


def fingerprint(missing: Sequence[str], extra: Sequence[str]) -> str:
    """One value per difference, whatever order it came in; ``none`` for no difference."""
    if not missing and not extra:
        return "none"
    lines = sorted([*(f"+{each}" for each in missing), *(f"-{each}" for each in extra)])
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def render_block(missing: Sequence[str], extra: Sequence[str], *, today: str, run_url: str) -> str:
    """The part of the issue this script owns, between its two markers."""
    lines = [f"<!-- cloudflare-ranges:begin fingerprint={fingerprint(missing, extra)} -->"]
    if missing or extra:
        lines.append(
            "The scheduled check finds that `ops/caddy/trusted_proxies.caddy` no longer "
            "matches the ranges Cloudflare publishes."
        )
    else:
        lines.append(
            "The scheduled check finds the file matching Cloudflare's published ranges "
            "again. Close this once the deployment runs that file."
        )
    lines += ["", f"- Last changed on {today}" + (f" by {run_url}" if run_url else "")]
    if missing:
        lines += ["", "Published by Cloudflare, not listed:", ""]
        lines += [f"- `{each}`" for each in missing]
    if extra:
        lines += ["", "Listed, no longer published by Cloudflare:", ""]
        lines += [f"- `{each}`" for each in extra]
    lines.append(_END)
    return "\n".join(lines)


def replace_block(body: str, block: str) -> str:
    """``body`` with this script's part replaced; everything else as it was."""
    if _BLOCK.search(body):
        return _BLOCK.sub(lambda _: block, body, count=1)
    return f"{block}\n\n{body}"


def record(
    missing: Sequence[str],
    extra: Sequence[str],
    run: Run,
    *,
    today: str,
    run_url: str,
    directory: Path,
) -> str:
    """File or update the issue; return what was done, in a line."""
    block = render_block(missing, extra, today=today, run_url=run_url)
    issue = open_issue(TITLE, run)
    body_file = directory / "cloudflare-ranges-body.md"
    if issue is None:
        if not missing and not extra:
            return "no difference and no open issue"
        body_file.write_text(f"{block}\n\n{_FOLLOW_UP}", encoding="utf-8")
        labels = [part for label in LABELS for part in ("--label", label)]
        created = _gh(
            run, "issue", "create", "--title", TITLE, "--body-file", str(body_file), *labels
        )
        return f"opened {created.strip()}"
    number = str(issue["number"])
    recorded = _BEGIN.search(issue["body"] or "")
    if recorded and recorded.group(1) == fingerprint(missing, extra):
        return f"#{number} already lists this difference"
    body_file.write_text(replace_block(issue["body"] or "", block), encoding="utf-8")
    _gh(run, "issue", "edit", number, "--body-file", str(body_file))
    note = (
        "The difference changed; the list above is today's."
        if missing or extra
        else ("No difference is reported now.")
    )
    body_file.write_text(note, encoding="utf-8")
    _gh(run, "issue", "comment", number, "--body-file", str(body_file))
    return f"updated #{number}"


def main(argv: Sequence[str] | None = None, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="compare and print; write no issue")
    parser.add_argument("--run-url", default="", help="the workflow run, for the issue")
    parser.add_argument("--work-dir", type=Path, default=Path("."), help="where to write bodies")
    args = parser.parse_args(argv)

    try:
        published = ranges.fetch_published()
    except (OSError, ValueError) as error:
        print(f"::error::could not read Cloudflare's published ranges: {error}")
        return 2
    if not published:
        print("::error::Cloudflare's published lists are empty")
        return 2

    text = ranges.TRUSTED_PROXIES.read_text(encoding="utf-8")
    missing, extra = ranges.difference(ranges.listed_ranges(text), published)
    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    for each in missing:
        print(f"published by Cloudflare, not listed: {each}")
    for each in extra:
        print(f"listed, no longer published by Cloudflare: {each}")
    if not missing and not extra:
        print(f"matches {len(published)} published ranges")
    if args.dry_run:
        print(render_block(missing, extra, today=today, run_url=args.run_url))
        return 0
    try:
        print(
            record(missing, extra, run, today=today, run_url=args.run_url, directory=args.work_dir)
        )
    except RuntimeError as error:
        print(f"::error::{error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

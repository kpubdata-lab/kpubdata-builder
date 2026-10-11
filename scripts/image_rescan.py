#!/usr/bin/env python3
"""Scan the images a deployment runs again, and keep one issue per image (#1107).

``docker.yml`` scans Builder's image when it is built. An image gains advisories
without anyone building it, and the build never scans the images the compose files
pull from elsewhere (Caddy, CUBRID). This reads the image references out of the
deployment's compose files, scans each with Trivy for HIGH and CRITICAL
vulnerabilities — those without a fix included — and records what it finds:

- no open issue for the image, and findings: one is opened;
- an open issue whose list is the same as today's: nothing is written;
- an open issue whose list differs: its list is rewritten and a comment says what
  changed. Only the part between the two markers is rewritten, so what a person wrote
  under "Triage" stays;
- an open issue and no finding today: its list is rewritten to say so. The issue is
  not closed here: whether the deployment runs the digest that was scanned is a
  person's to check.

Whether a fix exists is read from the scan. Whether the vulnerable code can be
reached, what it would cost and whether to act at once cannot be, so the issue has a
"Triage" part saying they were not assessed, for a person to fill in.

Exit status 1 when an image could not be scanned or an issue could not be written.

Usage:
    python3 scripts/image_rescan.py --list                 # the images, nothing else
    python3 scripts/image_rescan.py --dry-run              # scan and print, write no issue
    python3 scripts/image_rescan.py                        # scan, then file or update
    python3 scripts/image_rescan.py --image REF ...        # further images to scan
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[1]
#: The compose files a deployment is started from.
COMPOSE_FILES = ("docker-compose.prod.app.yml", "infra/cubrid/docker-compose.yml")
SEVERITIES = ("CRITICAL", "HIGH")
LABELS = ("epic:distribution", "security")
#: Rows written into an issue; GitHub refuses a body over 65536 characters.
MAX_ROWS = 150

_IMAGE = re.compile(r"^\s*image:\s*[\"']?([^\s\"'#]+)")
_DEFAULT = re.compile(r"\$\{\w+:-([^${}]*)\}")
_BEGIN = re.compile(r"<!-- image-rescan:begin fingerprint=(\w+) -->")
_END = "<!-- image-rescan:end -->"
_BLOCK = re.compile(
    r"<!-- image-rescan:begin fingerprint=\w+ -->.*?<!-- image-rescan:end -->", re.S
)
_ROW_ID = re.compile(r"^\| `([^`]+)` \|", re.M)

_TRIAGE = """\
## Triage

The scan cannot tell these; a person writes them here. The re-scan rewrites only the
list above and leaves this part as it is.

- Reachable in this deployment: not assessed
- Impact: not assessed
- Urgent action needed: not assessed
"""

#: Runs a command and returns it finished; replaced in the tests.
Run = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(command), check=False, capture_output=True, text=True)


class Finding(NamedTuple):
    vulnerability: str
    severity: str
    package: str
    installed: str
    fixed: str  # empty when no fixed version has been published
    target: str

    @property
    def key(self) -> str:
        return f"{self.vulnerability}|{self.package}|{self.installed}|{self.fixed}"


class Scan(NamedTuple):
    image: str
    digest: str
    findings: list[Finding]


def images_in(text: str) -> list[str]:
    """The image of every service in a compose file, in order.

    ``${NAME:-default}`` is read as its default: that is what a deployment which does
    not set the variable pulls. A reference left without a default is not an image
    that can be pulled, and is refused.
    """
    found: list[str] = []
    for line in text.splitlines():
        match = _IMAGE.match(line)
        if match is None:
            continue
        image = _DEFAULT.sub(lambda default: default.group(1), match.group(1))
        if "$" in image:
            raise ValueError(f"an image without a default cannot be scanned: {match.group(1)}")
        found.append(image)
    return found


def deployed_images(root: Path = ROOT) -> list[str]:
    """Every image the compose files name, once each."""
    found: list[str] = []
    for name in COMPOSE_FILES:
        for image in images_in((root / name).read_text(encoding="utf-8")):
            if image not in found:
                found.append(image)
    return found


def parse_report(image: str, report: dict[str, Any]) -> Scan:
    """The HIGH and CRITICAL findings of one Trivy JSON report, in a stable order."""
    metadata = report.get("Metadata") or {}
    digests = metadata.get("RepoDigests") or []
    digest = digests[0] if digests else str(metadata.get("ImageID") or "unknown")
    findings = set()
    for result in report.get("Results") or []:
        for item in result.get("Vulnerabilities") or []:
            if item.get("Severity") not in SEVERITIES:
                continue
            findings.add(
                Finding(
                    vulnerability=str(item.get("VulnerabilityID", "")),
                    severity=str(item["Severity"]),
                    package=str(item.get("PkgName", "")),
                    installed=str(item.get("InstalledVersion", "")),
                    fixed=str(item.get("FixedVersion") or ""),
                    target=str(result.get("Target", "")),
                )
            )
    ordered = sorted(findings, key=lambda f: (SEVERITIES.index(f.severity), f.vulnerability, f))
    return Scan(image=image, digest=digest, findings=ordered)


def fingerprint(findings: Sequence[Finding]) -> str:
    """One value per set of findings, whatever order they came in.

    The fixed version is part of it: a fix that appears for a known vulnerability is
    news, and the issue is rewritten for it.
    """
    if not findings:
        return "none"
    joined = "\n".join(sorted({finding.key for finding in findings}))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def title_for(image: str) -> str:
    return f"fix(security): {image} has HIGH or CRITICAL vulnerabilities"


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_block(scan: Scan, *, today: str, run_url: str) -> str:
    """The part of the issue this script owns, between its two markers."""
    findings = scan.findings
    fixable = sum(1 for finding in findings if finding.fixed)
    lines = [f"<!-- image-rescan:begin fingerprint={fingerprint(findings)} -->"]
    if findings:
        lines.append(
            f"The scheduled re-scan finds {len(findings)} HIGH or CRITICAL "
            f"vulnerabilities in `{scan.image}`: {fixable} with a fixed version "
            f"published, {len(findings) - fixable} without one yet."
        )
    else:
        lines.append(
            f"The scheduled re-scan finds no HIGH or CRITICAL vulnerability in "
            f"`{scan.image}` any more. Close this once the deployment runs the digest below."
        )
    lines += [
        "",
        f"- Digest scanned: `{scan.digest}`",
        f"- This list was last changed on {today}" + (f" by {run_url}" if run_url else ""),
        "- Scanned for the runner's platform (linux/amd64) with the Trivy version "
        "`.github/workflows/image-rescan.yml` installs.",
    ]
    if findings:
        lines += [
            "",
            "| Vulnerability | Severity | Package | Installed | Fixed in | Where |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
        for finding in findings[:MAX_ROWS]:
            lines.append(
                f"| `{_cell(finding.vulnerability)}` | {finding.severity} "
                f"| {_cell(finding.package)} | {_cell(finding.installed)} "
                f"| {_cell(finding.fixed) or 'no fix published'} | {_cell(finding.target)} |"
            )
        if len(findings) > MAX_ROWS:
            lines += ["", f"{len(findings) - MAX_ROWS} more are in the run's log."]
    lines.append(_END)
    return "\n".join(lines)


def render_body(block: str) -> str:
    return f"{block}\n\n{_TRIAGE}"


def replace_block(body: str, block: str) -> str:
    """``body`` with this script's part replaced; everything else as it was."""
    if _BLOCK.search(body):
        return _BLOCK.sub(lambda _: block, body, count=1)
    # The markers were edited away: keep what is there and put the list above it.
    return f"{block}\n\n{body}"


def change_comment(old_body: str, scan: Scan) -> str:
    """What differs between the list an issue had and today's."""
    before = set(_ROW_ID.findall(old_body))
    now = {finding.vulnerability for finding in scan.findings}
    lines = [f"The re-scan's list for `{scan.image}` changed (digest `{scan.digest}`)."]
    for label, ids in (("New", now - before), ("No longer reported", before - now)):
        if ids:
            shown = sorted(ids)[:30]
            more = f" and {len(ids) - len(shown)} more" if len(ids) > len(shown) else ""
            lines.append(f"- {label}: {', '.join(f'`{each}`' for each in shown)}{more}")
    if now == before and now:
        lines.append("- The same vulnerabilities, with a different installed or fixed version.")
    if not now:
        lines.append("- No HIGH or CRITICAL vulnerability is reported now.")
    return "\n".join(lines)


def scan_image(image: str, run: Run) -> Scan:
    """Scan one image with Trivy; a scan that did not finish raises."""
    result = run(
        [
            "trivy",
            "image",
            "--quiet",
            "--format",
            "json",
            "--scanners",
            "vuln",
            "--severity",
            ",".join(SEVERITIES),
            image,
        ]
    )
    if result.returncode != 0:
        raise RuntimeError(f"trivy could not scan {image}: {result.stderr.strip()[-2000:]}")
    return parse_report(image, json.loads(result.stdout))


def _gh(run: Run, *args: str) -> str:
    result = run(["gh", *args])
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout


def open_issue(title: str, run: Run) -> dict[str, Any] | None:
    """The open issue with exactly this title, or ``None``."""
    listed = json.loads(
        _gh(
            run,
            "issue",
            "list",
            "--state",
            "open",
            "--search",
            f'in:title "{title}"',
            "--json",
            "number,title,body",
            "--limit",
            "50",
        )
    )
    matching = [issue for issue in listed if issue["title"] == title]
    return min(matching, key=lambda issue: int(issue["number"])) if matching else None


def record(scan: Scan, run: Run, *, today: str, run_url: str, directory: Path) -> str:
    """File or update the issue for one image; return what was done, in a line."""
    title = title_for(scan.image)
    block = render_block(scan, today=today, run_url=run_url)
    issue = open_issue(title, run)
    body_file = directory / "body.md"
    if issue is None:
        if not scan.findings:
            return f"{scan.image}: no finding and no open issue"
        body_file.write_text(render_body(block), encoding="utf-8")
        labels = [part for label in LABELS for part in ("--label", label)]
        created = _gh(
            run, "issue", "create", "--title", title, "--body-file", str(body_file), *labels
        )
        return f"{scan.image}: opened {created.strip()}"
    number = str(issue["number"])
    recorded = _BEGIN.search(issue["body"] or "")
    if recorded and recorded.group(1) == fingerprint(scan.findings):
        return f"{scan.image}: #{number} already lists these findings"
    body_file.write_text(replace_block(issue["body"] or "", block), encoding="utf-8")
    _gh(run, "issue", "edit", number, "--body-file", str(body_file))
    body_file.write_text(change_comment(issue["body"] or "", scan), encoding="utf-8")
    _gh(run, "issue", "comment", number, "--body-file", str(body_file))
    return f"{scan.image}: updated #{number}"


def main(argv: Sequence[str] | None = None, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--image", action="append", default=[], help="a further image to scan")
    parser.add_argument("--list", action="store_true", help="print the images and stop")
    parser.add_argument("--dry-run", action="store_true", help="scan and print; write no issue")
    parser.add_argument("--run-url", default="", help="the workflow run, for the issue")
    parser.add_argument("--work-dir", type=Path, default=Path("."), help="where to write bodies")
    args = parser.parse_args(argv)

    images = deployed_images()
    images += [image for image in args.image if image not in images]
    if args.list:
        print("\n".join(images))
        return 0

    today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    failed = False
    for image in images:
        try:
            scan = scan_image(image, run)
            # Every finding goes to the log: an issue's table stops at MAX_ROWS.
            print(f"{image} ({scan.digest}): {len(scan.findings)} HIGH or CRITICAL")
            for finding in scan.findings:
                print("  " + " ".join(part or "-" for part in finding))
            if args.dry_run:
                block = render_block(scan, today=today, run_url=args.run_url)
                print(f"would be recorded as: {title_for(image)}\n{block}\n")
                continue
            print(record(scan, run, today=today, run_url=args.run_url, directory=args.work_dir))
        except (RuntimeError, ValueError) as error:
            failed = True
            print(f"::error::{error}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

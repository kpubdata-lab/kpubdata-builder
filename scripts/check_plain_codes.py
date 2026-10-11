#!/usr/bin/env python3
"""Review and verification codes are written meaning first (kpubdata#878).

POLICY 0.1 says an internal code -- a review level `R0`..`R3` or a verification
level `V0`..`V5`, `V5-replay`, `V5-live` -- is never written alone. The meaning
comes first and the code follows in parentheses: "unit tests (V1)", "an approval
from someone other than the author (R3)", or the same in Korean with no space before
the parenthesis. A reader outside the project cannot
decode a bare `V2`, and writers get it wrong too: four issues opened on 2026-10-08
called unit tests V2.

The files checked come from `git ls-files`, not from a list written here: every
tracked Markdown file, plus the issue forms under `.github/ISSUE_TEMPLATE/`, which
seed every new issue.

A code token is accepted when it is

- inside parentheses that follow some text on the same line ("meaning(V1)",
  "meaning (R3)", "(V2 + V3)" after a word);
- inside an inline code span or a fenced code block -- that is a literal name,
  such as the `R3 review` check;
- part of the label `review:R0`..`review:R3`;
- the text of a Markdown link to the glossary (`POLICY.md#codes`);
- between `<!-- plain-codes: off -->` and `<!-- plain-codes: on -->`. POLICY's
  glossary and its two definition tables (sections 18 and 18.1) use this, because
  they are where the codes are defined;
- one of ALLOWED_PHRASES, where the letters are a product name, not a level.

`CHANGELOG.md` is checked in its `## [Unreleased]` section only. The released
sections are history: they record what a release said and are not rewritten.

Only uppercase `R`/`V` count, and not after a letter, digit, `_`, `-` or `.`, so
`v2`, `API v2`, `utf-8` and `kpubdata-R2` are not codes.

Usage:
    python scripts/check_plain_codes.py [--root PATH] [FILE ...]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The glossary anchor in kpubdata's POLICY.md. A link whose target holds it explains itself.
GLOSSARY_ANCHOR = "#codes"

# Phrases where R/V plus a digit is a product name, not a level.
ALLOWED_PHRASES = ("Cloudflare R2",)

_TOKEN = re.compile(r"(?<![A-Za-z0-9_\-.])(?:V5-(?:replay|live)|R[0-3]|V[0-5])(?![A-Za-z0-9_])")
_FENCE = re.compile(r"^\s*(```|~~~)")
_OFF = "<!-- plain-codes: off -->"
_ON = "<!-- plain-codes: on -->"
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]*)\)")
_LABEL = re.compile(r"\breview:R[0-3]\b")
_UNRELEASED = re.compile(r"^## \[Unreleased\]", re.IGNORECASE)
_RELEASE_HEADING = re.compile(r"^## \[")

# A gloss needs something before the parenthesis: a word, or closing markup.
_GLOSS_TAIL = set("*`'\")]")


def tracked_files(root: Path) -> list[str]:
    """Markdown files and issue forms that git tracks under *root*."""
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--",
            "*.md",
            ".github/ISSUE_TEMPLATE/*.yml",
            ".github/ISSUE_TEMPLATE/*.yaml",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(line for line in result.stdout.splitlines() if line)


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _mask(line: str) -> str:
    """Blank what may hold a code legitimately, keeping columns in place."""
    for match in list(_CODE_SPAN.finditer(line)):
        line = _blank(line, match.start(), match.end())
    for match in list(_LINK.finditer(line)):
        if GLOSSARY_ANCHOR in match.group(2):
            line = _blank(line, match.start(), match.end())
        else:
            # The target is a path or URL; only the link text is prose.
            line = _blank(line, match.start(2), match.end(2))
    for match in list(_LABEL.finditer(line)):
        line = _blank(line, match.start(), match.end())
    for phrase in ALLOWED_PHRASES:
        start = line.find(phrase)
        while start != -1:
            line = _blank(line, start, start + len(phrase))
            start = line.find(phrase, start + len(phrase))
    return line


def _glossed(line: str, start: int, end: int) -> bool:
    """True when the token sits in parentheses that follow some text."""
    depth = 0
    opening = -1
    for index in range(start - 1, -1, -1):
        char = line[index]
        if char == ")":
            depth += 1
        elif char == "(":
            if depth == 0:
                opening = index
                break
            depth -= 1
    if opening == -1 or ")" not in line[end:]:
        return False
    before = line[:opening].rstrip()
    if not before:
        return False
    tail = before[-1]
    return tail.isalnum() or tail in _GLOSS_TAIL


def bare_codes(text: str, *, changelog: bool = False) -> list[tuple[int, str, str]]:
    """Every bare code in *text* as (line number, code, line)."""
    found: list[tuple[int, str, str]] = []
    in_fence = False
    fence_marker = ""
    off = False
    # The changelog is checked until its first released section.
    in_scope = not changelog
    for number, line in enumerate(text.splitlines(), start=1):
        if changelog:
            if _UNRELEASED.match(line):
                in_scope = True
                continue
            if in_scope and _RELEASE_HEADING.match(line):
                break
        if not in_scope:
            continue
        fence = _FENCE.match(line)
        if fence:
            if not in_fence:
                in_fence, fence_marker = True, fence.group(1)
            elif fence.group(1) == fence_marker:
                in_fence = False
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if stripped == _OFF:
            off = True
            continue
        if stripped == _ON:
            off = False
            continue
        if off:
            continue
        masked = _mask(line)
        for match in _TOKEN.finditer(masked):
            if not _glossed(masked, match.start(), match.end()):
                found.append((number, match.group(0), line.strip()))
    return found


def main(argv: list[str] | None = None) -> int:
    """Check the tracked documents. Returns 0 when no code stands alone, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("files", nargs="*", help="check these instead of git ls-files")
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    names: list[str] = args.files or tracked_files(root)
    if not names:
        print(f"no tracked Markdown under {root}, so nothing was checked", file=sys.stderr)
        return 1

    problems: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file():
            # A file named on the command line that is not there was not checked,
            # and saying nothing would read as a pass. A tracked file deleted from
            # the working tree has nothing left to check.
            if args.files:
                problems.append(f"{name}: no such file")
            continue
        text = path.read_text(encoding="utf-8")
        for number, code, line in bare_codes(text, changelog=path.name == "CHANGELOG.md"):
            problems.append(f"{name}:{number}: {code}  | {line}")

    if problems:
        print(f"{len(problems)} bare review/verification code(s):\n", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nWrite the meaning first and the code in parentheses: "
            '"단위 테스트(V1)", "an approval from someone other than the author (R3)".'
            "\nThe glossary is kpubdata's docs/governance/POLICY.md#codes (POLICY 0.1,"
            " kpubdata#878).",
            file=sys.stderr,
        )
        return 1

    print(f"{len(names)} files checked, no bare review/verification code.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

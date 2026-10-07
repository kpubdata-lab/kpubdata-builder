#!/usr/bin/env python3
"""Refuse an ADR index whose status column disagrees with the ADRs (#1110).

``docs/adrs/README.md`` lists every ADR with a status. Each ADR also states its own, in
a status line under its title (``STATUS_LINE_LABEL``). The two were edited apart: ADR
0008, 0010 and 0011 were marked accepted in their own files and stayed "proposed" in the
index, so the page people read first said three implemented decisions were still open.

What is compared is the status **word** each side begins with — the Korean words for
proposed, accepted (two spellings are in use) and superseded, listed in
``STATUS_WORDS``. Whatever follows the word (a date, who confirmed it, which ADR
supersedes it) is free text and may differ.

Also refused: an ADR file with no row, a row with no file, a file with no status line,
and a status word this script does not know.

Usage:
    python scripts/check_adr_index.py
    python scripts/check_adr_index.py DIRECTORY
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adrs"

#: The status words an ADR or the index may begin with, and what each means.
STATUS_WORDS = {
    "제안됨": "proposed",
    "승인됨": "accepted",
    "수용됨": "accepted",
    "대체됨": "superseded",
}

_FILE = re.compile(r"^(\d{4})-[a-z0-9-]+\.md$")
_ROW = re.compile(r"^\|\s*\[(\d{4})\]\(\./(?P<file>[^)]+)\)\s*\|[^|]*\|(?P<status>[^|]*)\|")
#: The label of an ADR's own status line: ``- <label>: <status>``.
STATUS_LINE_LABEL = "상태"
_STATUS_LINE = re.compile(rf"^-\s*{STATUS_LINE_LABEL}\s*:\s*(?P<status>.+)$")


def status_word(text: str) -> str | None:
    """The canonical status ``text`` begins with, or None when it begins with no known word."""
    cleaned = text.strip().lstrip("*").strip()
    for word, meaning in STATUS_WORDS.items():
        if cleaned.startswith(word):
            return meaning
    return None


def adr_status(path: Path) -> str | None:
    """The text of the ADR's own status line, or None when it has none."""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _STATUS_LINE.match(line.strip())
        if match:
            return match.group("status")
    return None


def index_rows(readme: Path) -> dict[str, tuple[str, str]]:
    """``{number: (file name, status text)}`` for every ADR row of the index table."""
    rows: dict[str, tuple[str, str]] = {}
    for line in readme.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line.strip())
        if match:
            rows[match.group(1)] = (match.group("file"), match.group("status").strip())
    return rows


def check(directory: Path) -> list[str]:
    """Every disagreement between the index in ``directory`` and its ADR files."""
    readme = directory / "README.md"
    if not readme.is_file():
        return [f"{readme}: the ADR index is missing"]
    rows = index_rows(readme)
    files = {
        match.group(1): path
        for path in sorted(directory.glob("*.md"))
        if (match := _FILE.match(path.name))
    }
    problems: list[str] = []
    for number in sorted(files.keys() - rows.keys()):
        problems.append(f"ADR {number}: {files[number].name} has no row in the index")
    for number in sorted(rows.keys() - files.keys()):
        problems.append(f"ADR {number}: the index lists it but there is no such file")
    for number in sorted(rows.keys() & files.keys()):
        listed_file, listed = rows[number]
        path = files[number]
        if listed_file != path.name:
            problems.append(f"ADR {number}: the index links {listed_file}, the file is {path.name}")
        own = adr_status(path)
        if own is None:
            problems.append(f"ADR {number}: {path.name} has no '- {STATUS_LINE_LABEL}:' line")
            continue
        own_word, listed_word = status_word(own), status_word(listed)
        if own_word is None:
            problems.append(f"ADR {number}: {path.name} states an unknown status: {own!r}")
        if listed_word is None:
            problems.append(f"ADR {number}: the index states an unknown status: {listed!r}")
        if own_word and listed_word and own_word != listed_word:
            problems.append(
                f"ADR {number}: the index says {listed!r} ({listed_word}) "
                f"but {path.name} says {own!r} ({own_word})"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    directory = Path(arguments[0]) if arguments else ADR_DIR
    problems = check(directory)
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(
            f"\n{len(problems)} problem(s). The ADR's own status line is the source: "
            "change the index to match it, or change the ADR if the decision moved.",
            file=sys.stderr,
        )
        return 1
    print(f"ADR index matches its {len(index_rows(directory / 'README.md'))} ADRs")
    return 0


if __name__ == "__main__":
    sys.exit(main())

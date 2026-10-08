#!/usr/bin/env python3
"""What a release was tested with, as one line for its notes (#718).

`docs/compatibility.md` in kpubdata pairs each application version with a kpubdata
version (ADR 0004, section 1). That column has to be a fact, not an intention:
`pyproject.toml` pins kpubdata to a range, while the release gates ran
against the one version `uv.lock` pinned. This prints that version and the Builder API
contract version, so the release notes carry both and the table is copied from them.

Usage:
    python scripts/release_facts.py            # print the line
    python scripts/release_facts.py NOTES.md   # append it to a notes file
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# uv.lock is TOML, but tomllib is 3.11+ and this project supports 3.10. The package
# table is regular enough that the name line is always followed by its version line.
_LOCKED = re.compile(r'^name = "kpubdata"\nversion = "([^"]+)"$', re.MULTILINE)
_CONTRACT = re.compile(r'^  version: "([^"]+)"$', re.MULTILINE)


def tested_kpubdata() -> str:
    """The kpubdata version uv.lock pins — the one the release gates install."""
    match = _LOCKED.search((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    if match is None:
        raise SystemExit("uv.lock pins no kpubdata version")
    return match.group(1)


def contract_version() -> str:
    """info.version of contract/builder-api.yaml."""
    text = (REPO_ROOT / "contract" / "builder-api.yaml").read_text(encoding="utf-8")
    match = _CONTRACT.search(text)
    if match is None:
        raise SystemExit("contract/builder-api.yaml has no info.version")
    return match.group(1)


def facts_line() -> str:
    return (
        f"Tested with kpubdata **{tested_kpubdata()}** (pinned in `uv.lock` while the "
        f"release gates ran) · Builder API contract **{contract_version()}**."
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    line = facts_line()
    if args:
        with Path(args[0]).open("a", encoding="utf-8") as notes:
            notes.write(f"\n---\n\n{line}\n")
    print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

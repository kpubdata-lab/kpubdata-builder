#!/usr/bin/env python3
"""Read the Studio follow-up a contract change names, if any (#1004).

The `Studio drift` job in ci.yml runs Studio ``main``'s contract drift test
(``src/shared/lib/contractDrift.test.ts``) against the contract a pull request carries,
so a Builder change that breaks Studio fails here rather than on Studio's next run.

Some changes have to land in Builder first: Studio develops against Builder ``main``
(ADR 0004), so a field Studio must stop requiring, or a ``KNOWN_DRIFT`` entry a Builder
fix makes stale, can only be fixed in Studio once Builder has merged. For those, the
pull request body names the Studio issue that will follow, on a line of its own::

    Studio-Follow-Up: kpubdata-lab/kpubdata-studio#123

The job then reports the drift as a warning naming that issue instead of failing, after
checking the issue exists and is open. Without the line, drift fails the job. The job
reads the body when it runs, so adding the line and re-running the job is enough.

Usage (the body comes from the environment, never from the command line, so its text
is not interpreted by a shell)::

    PR_BODY="..." python scripts/studio_drift_follow_up.py   # prints 123, or nothing
"""

from __future__ import annotations

import os
import re

STUDIO_REPO = "kpubdata-lab/kpubdata-studio"

_LINE = re.compile(
    rf"^Studio-Follow-Up:[ \t]*{re.escape(STUDIO_REPO)}#(?P<number>[1-9][0-9]*)[ \t]*$",
    re.MULTILINE,
)


def follow_up(body: str | None) -> int | None:
    """The Studio issue number ``body`` names, or ``None``.

    Only the full ``kpubdata-lab/kpubdata-studio#N`` form counts: a bare ``#N`` would be
    read as a Builder issue by everyone else who reads the line.
    """
    if not body:
        return None
    match = _LINE.search(body.replace("\r\n", "\n"))
    return int(match["number"]) if match else None


def main() -> int:
    number = follow_up(os.environ.get("PR_BODY"))
    if number is not None:
        print(number)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Refuse workflow steps that run another repository's code by a movable ref (#1003).

A ``uses:`` reference to another repository runs whatever that ref points at when the
job starts. A branch (``@main``) or a tag (``@v7``) can move without a change in this
repository, so the release, title and review gates would run code nobody here reviewed.
Every such reference must name a full 40-character commit SHA.

What is swept: every ``*.yml``/``*.yaml`` under ``.github/workflows`` and
``.github/actions``. A local reference (``./.github/workflows/x.yml``) runs this
repository's own code at the same commit and passes. A ``docker://`` image must be
pinned by ``@sha256:`` digest.

Keeping the pins current:

- Third-party actions carry a ``# vX.Y.Z`` comment; Dependabot (``github-actions``)
  raises the SHA and the comment together.
- The shared kpubdata actions have no release tags of their own and are ignored by
  Dependabot. Move them together, to one kpubdata commit, with ``--bump-kpubdata``::

      python scripts/check_action_pins.py --bump-kpubdata \\
          "$(gh api repos/kpubdata-lab/kpubdata/commits/main --jq .sha)"

Usage:
    python scripts/check_action_pins.py
    python scripts/check_action_pins.py FILE...
    python scripts/check_action_pins.py --bump-kpubdata SHA
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
SWEPT_DIRS = (".github/workflows", ".github/actions")

#: The repository whose shared actions ``--bump-kpubdata`` moves.
KPUBDATA_REPO = "kpubdata-lab/kpubdata"

_USES = re.compile(
    r"""^(?P<head>\s*(?:-\s+)?uses:\s*)(?P<quote>["']?)(?P<ref>[^\s"'#]+)(?P=quote)(?P<tail>.*)$"""
)
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class Violation(NamedTuple):
    path: Path
    line: int
    ref: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.ref} — {self.reason}"


def _reason(ref: str) -> str | None:
    """Return why ``ref`` is not pinned, or ``None`` when it is."""
    if ref.startswith("./"):
        return None
    if ref.startswith("docker://"):
        image = ref.removeprefix("docker://")
        _, _, digest = image.partition("@")
        if _DIGEST.fullmatch(digest):
            return None
        return "pin the image by @sha256: digest"
    _, sep, version = ref.rpartition("@")
    if not sep:
        return "no ref; pin a full commit SHA"
    if _SHA.fullmatch(version):
        return None
    return f"'{version}' can move; pin a full 40-character commit SHA"


def check(paths: Iterable[Path]) -> list[Violation]:
    violations: list[Violation] = []
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = _USES.match(line)
            if match is None:
                continue
            ref = match["ref"]
            reason = _reason(ref)
            if reason is not None:
                violations.append(Violation(path, number, ref, reason))
    return violations


def bump_kpubdata(paths: Iterable[Path], sha: str) -> list[Path]:
    """Point every shared kpubdata action at ``sha``; return the files changed."""
    if not _SHA.fullmatch(sha):
        raise ValueError(f"not a full 40-character commit SHA: {sha!r}")
    prefix = f"{KPUBDATA_REPO}/"
    changed: list[Path] = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        for index, line in enumerate(lines):
            body = line.rstrip("\r\n")
            match = _USES.match(body)
            if match is None or not match["ref"].startswith(prefix):
                continue
            action, _, _ = match["ref"].rpartition("@")
            quote = match["quote"]
            lines[index] = (
                f"{match['head']}{quote}{action}@{sha}{quote}  # {KPUBDATA_REPO} main"
                + line[len(body) :]
            )
        updated = "".join(lines)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(path)
    return changed


def default_paths(root: Path = ROOT) -> list[Path]:
    paths: list[Path] = []
    for directory in SWEPT_DIRS:
        base = root / directory
        if base.is_dir():
            paths.extend(sorted(p for p in base.rglob("*") if p.suffix in {".yml", ".yaml"}))
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="*", type=Path, help="files to check (default: all)")
    parser.add_argument(
        "--bump-kpubdata",
        metavar="SHA",
        help=f"point every {KPUBDATA_REPO} action at this commit, then check",
    )
    args = parser.parse_args(argv)
    paths = list(args.files) or default_paths()

    if args.bump_kpubdata:
        try:
            changed = bump_kpubdata(paths, args.bump_kpubdata)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for path in changed:
            print(f"updated {path}")

    violations = check(paths)
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        print(
            f"\n{len(violations)} reference(s) can change without a change here. "
            "Pin each to a full commit SHA (see scripts/check_action_pins.py).",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

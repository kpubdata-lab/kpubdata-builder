#!/usr/bin/env python3
"""Fail when a comment or docstring still contains Korean text (builder#710).

The project is published as a global open-source library, so everything a
reader of the source encounters has to be English. Docstrings matter most:
they are what ``help()``, the mkdocs API pages and IDE tooltips show.

Only comments and docstrings are checked. Korean *string literals* are left
alone on purpose -- user-facing messages (the data.go.kr 403 hint that names
the activation request, CLI output) are runtime behaviour, and whether to
translate them is a separate product decision.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from pathlib import Path

HANGUL = re.compile(r"[가-힣]")

DEFAULT_ROOTS = ("src", "tests", "scripts")


def _docstring_nodes(tree: ast.AST) -> list[ast.AST]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def findings(path: Path) -> list[tuple[int, str, str]]:
    """Return ``(line, kind, excerpt)`` for every Korean comment or docstring."""
    source = path.read_text(encoding="utf-8")
    found: list[tuple[int, str, str]] = []

    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for token in tokens:
            if token.type is tokenize.COMMENT and HANGUL.search(token.string):
                found.append((token.start[0], "comment", token.string.strip()))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file that does not tokenise is a separate problem; the syntax
        # checks in CI report it. Do not mask it as a translation finding.
        pass

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return found

    for node in _docstring_nodes(tree):
        text = ast.get_docstring(node, clean=False)
        if text is None or not HANGUL.search(text):
            continue
        line = getattr(node, "lineno", 1)
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        found.append((line, "docstring", first))

    return found


def _python_files(roots: list[str] | tuple[str, ...]) -> set[Path]:
    """Expand ``roots`` -- directories or individual files -- into .py paths."""
    found: set[Path] = set()
    for root in roots:
        path = Path(root)
        if path.is_dir():
            found.update(path.rglob("*.py"))
        elif path.suffix == ".py":
            found.add(path)
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS))
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print per-file counts instead of every finding",
    )
    args = parser.parse_args()

    files = sorted(_python_files(args.roots or DEFAULT_ROOTS))

    total = 0
    for path in files:
        hits = findings(path)
        if not hits:
            continue
        total += len(hits)
        if args.summary:
            print(f"{len(hits):4}  {path}")
            continue
        for line, kind, excerpt in hits:
            print(f"{path}:{line}: {kind}: {excerpt[:100]}")

    if total:
        print(
            f"\n{total} Korean comment(s)/docstring(s) remain. "
            "The source of a global open-source library has to be readable "
            "without Korean (#517).",
            file=sys.stderr,
        )
        return 1

    print(f"checked {len(files)} files: no Korean comments or docstrings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

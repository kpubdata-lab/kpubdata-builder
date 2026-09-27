#!/usr/bin/env python3
"""Fail when Korean text is added to comments or docstrings (#517, ADR 0003).

ADR 0003 puts code identifiers, comments and docstrings in English. That rule was
written into AGENTS.md and never enforced, so the repository accumulated thousands
of Korean comments while the rule sat there being true and ignored.

This is a **ratchet**, not a wall. Translating everything at once is separate work,
so the baseline records how many Korean-bearing comments and docstrings each file
has today, and this check fails only when a count goes up, or when a file absent
from the baseline has any. Existing debt is frozen; new debt is refused.

User-visible strings are out of scope — their language is runtime behaviour, which
ADR 0003 decides separately. Only comments and docstrings are examined.

Usage:
    python scripts/check_korean_comments.py                # check against baseline
    python scripts/check_korean_comments.py --update       # rewrite the baseline
    python scripts/check_korean_comments.py src/pkg/mod.py # check named paths
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tokenize
from collections.abc import Iterable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).resolve().parent / "korean_comment_baseline.json"
DEFAULT_TARGETS = ("src", "scripts", "tests")

# Hangul syllables plus Jamo. Punctuation and full-width forms are deliberately
# excluded: "SQL" inside a Korean sentence is not what makes it Korean, and a stray
# full-width bracket in an otherwise English comment is not a violation.
_HANGUL = re.compile(r"[가-힣ᄀ-ᇿ㄰-㆏]")

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)


def _is_skipped(path: Path) -> bool:
    """Whether any component of the path is a directory this check ignores."""
    return any(part in _SKIP_DIRS for part in path.parts)


def iter_python_files(targets: Iterable[Path]) -> Iterator[Path]:
    """Yield Python files under ``targets``.

    A target may be a file or a directory. Naming a file explicitly has to work: an
    earlier version of this check only walked directories, so a file given on the
    command line was silently examined as nothing.
    """
    for target in targets:
        if target.is_file():
            if target.suffix == ".py" and not _is_skipped(target):
                yield target
        elif target.is_dir():
            for path in sorted(target.rglob("*.py")):
                if not _is_skipped(path):
                    yield path


def korean_comment_lines(source: str) -> list[int]:
    """Line numbers of comments containing Hangul.

    ``tokenize`` is used rather than a line-by-line scan so that a ``#`` inside a
    string literal is not mistaken for a comment.
    """
    lines: list[int] = []
    try:
        tokens = tokenize.generate_tokens(iter(source.splitlines(keepends=True)).__next__)
        for token in tokens:
            if token.type is tokenize.COMMENT and _HANGUL.search(token.string):
                lines.append(token.start[0])
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # A file this tool cannot tokenize is a problem for the linter, not for
        # this check. Report no comments rather than failing the whole run.
        return []
    return lines


def korean_docstring_lines(source: str) -> list[int]:
    """Line numbers of docstrings containing Hangul.

    Only genuine docstrings count — the first statement of a module, class or
    function. A bare string used as a block comment is not a docstring, and a
    Korean string literal may be a user-visible message, which is out of scope.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is not None and _HANGUL.search(doc):
            first = node.body[0]
            lines.append(getattr(first, "lineno", 1))
    return lines


def count_file(path: Path) -> tuple[int, list[str]]:
    """Return the Korean comment/docstring count for ``path`` and where they are."""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{path}: 읽을 수 없다 ({exc})", file=sys.stderr)
        return 0, []
    rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
    hits = [f"{rel}:{line} comment" for line in korean_comment_lines(source)]
    hits += [f"{rel}:{line} docstring" for line in korean_docstring_lines(source)]
    return len(hits), sorted(hits)


def scan(targets: Iterable[Path]) -> dict[str, int]:
    """Count Korean comments and docstrings per file, skipping clean files."""
    counts: dict[str, int] = {}
    for path in iter_python_files(targets):
        total, _ = count_file(path)
        if total:
            rel = path.relative_to(REPO_ROOT) if path.is_absolute() else path
            counts[rel.as_posix()] = total
    return counts


def load_baseline() -> dict[str, int]:
    """Read the baseline, treating an absent file as an empty baseline."""
    if not BASELINE_PATH.exists():
        return {}
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise SystemExit(f"{BASELINE_PATH}: 'counts' 객체가 없다")
    return {str(k): int(v) for k, v in counts.items()}


def write_baseline(counts: dict[str, int]) -> None:
    """Write the baseline sorted, so a diff shows only real movement."""
    payload = {
        "_comment": (
            "Korean comment/docstring debt per file, frozen by "
            "scripts/check_korean_comments.py. Counts may go down, never up. "
            "Regenerate with --update after translating."
        ),
        "total": sum(counts.values()),
        "counts": dict(sorted(counts.items())),
    }
    BASELINE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    """Run the check. Returns 0 when nothing regressed, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="files or directories")
    parser.add_argument("--update", action="store_true", help="rewrite the baseline")
    parser.add_argument("--show", action="store_true", help="list every location")
    args = parser.parse_args(argv)

    targets = args.paths or [REPO_ROOT / name for name in DEFAULT_TARGETS]
    targets = [t for t in targets if t.exists()]
    counts = scan(targets)

    if args.update:
        write_baseline(counts)
        print(f"baseline 갱신: {len(counts)}개 파일, {sum(counts.values())}건")
        return 0

    baseline = load_baseline()
    regressions: list[str] = []
    for name, count in sorted(counts.items()):
        allowed = baseline.get(name, 0)
        if count > allowed:
            regressions.append(
                f"  {name}: {count}건 (허용 {allowed}건) — {count - allowed}건 늘었다"
            )

    improved = sum(max(0, baseline.get(name, 0) - counts.get(name, 0)) for name in baseline)

    if args.show:
        for path in iter_python_files(targets):
            _, hits = count_file(path)
            for hit in hits:
                print(hit)

    if regressions:
        print(
            "한국어 주석·docstring 이 늘었다. ADR 0003 은 코드 주석을 영어로 둔다.\n",
            file=sys.stderr,
        )
        for line in regressions:
            print(line, file=sys.stderr)
        print(
            "\n새로 쓰는 코드는 영어로 적는다. 기존 부채를 번역했다면\n"
            "  python scripts/check_korean_comments.py --update\n"
            "로 baseline 을 내린다 — 올리는 방향으로는 쓰지 않는다.",
            file=sys.stderr,
        )
        return 1

    total = sum(counts.values())
    note = f", {improved}건 줄었다" if improved else ""
    print(f"한국어 주석·docstring {total}건 (baseline {sum(baseline.values())}건{note})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Refuse to publish while the version sources disagree (#690).

Measured 2026-09-27: `pyproject.toml` declared `0.4.0.dev0` while the newest tag
was `v0.1.0`, three minor versions behind. The image tag comes from the git tag
(`type=ref,event=tag` in docker.yml) and the package version inside the image comes
from the distribution metadata, so nothing stopped an image labelled `v0.1.0` from
containing `0.4.0.dev0`. A user reporting a bug then cannot say what they ran.

`pyproject.toml` is the single source of truth. This check compares everything else
against it:

- ``CHANGELOG.md``'s top ``## vX.Y`` heading (major.minor)
- a git tag, when one is given — it must be ``v`` plus the exact version

It also refuses to tag a development version at all. `0.4.0.dev0` is a statement
that the version is not finished; publishing it as a release contradicts the
declaration rather than merely disagreeing with it.

Usage:
    python scripts/check_version_consistency.py                 # sources only
    python scripts/check_version_consistency.py --tag v0.4.0    # and against a tag
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# tomllib is 3.11+ and this project supports 3.10, so the version is read with a
# regex. The same choice is made in tests/unit/test_version.py.
_PYPROJECT_VERSION = re.compile(r'^version = "([^"]+)"', re.MULTILINE)
_CHANGELOG_HEADING = re.compile(r"^## v(\d+\.\d+)", re.MULTILINE)

# A PEP 440 development or pre-release segment. Anything carrying one is by
# definition not a finished version.
_PRERELEASE = re.compile(r"(\.dev\d*|a\d+|b\d+|rc\d+)$")


class VersionMismatch(Exception):
    """A version source disagrees with pyproject.toml."""


def pyproject_version() -> str:
    """The declared package version, which is canonical.

    Raises:
        VersionMismatch: pyproject.toml has no version.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = _PYPROJECT_VERSION.search(text)
    if match is None:
        raise VersionMismatch("pyproject.toml has no [project] version")
    return match.group(1)


def changelog_version() -> str:
    """The major.minor of the newest CHANGELOG section.

    Raises:
        VersionMismatch: CHANGELOG.md has no version heading.
    """
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = _CHANGELOG_HEADING.search(text)
    if match is None:
        raise VersionMismatch("CHANGELOG.md has no '## vX.Y' heading")
    return match.group(1)


def is_prerelease(version: str) -> bool:
    """Whether the version carries a development or pre-release segment."""
    return _PRERELEASE.search(version) is not None


def check_sources() -> str:
    """Compare CHANGELOG against pyproject. Returns the canonical version.

    Raises:
        VersionMismatch: They disagree.
    """
    version = pyproject_version()
    major_minor = ".".join(version.split(".")[:2])
    changelog = changelog_version()
    if changelog != major_minor:
        raise VersionMismatch(
            f"pyproject.toml declares {version} (line {major_minor}) but CHANGELOG.md's "
            f"newest section is v{changelog}. The declaration is canonical — either "
            "bump the changelog heading or correct the version."
        )
    return version


def check_tag(tag: str) -> str:
    """Compare a git tag against pyproject. Returns the canonical version.

    Raises:
        VersionMismatch: The tag does not match, or the version is a pre-release.
    """
    version = check_sources()
    if is_prerelease(version):
        raise VersionMismatch(
            f"refusing to publish tag {tag}: pyproject.toml declares {version}, which is "
            "a development version. Finalise the version before tagging — a release "
            "that says it is unfinished contradicts its own declaration."
        )
    expected = f"v{version}"
    if tag != expected:
        raise VersionMismatch(
            f"tag {tag} does not match the declared version {version} (expected "
            f"{expected}). The image tag comes from the git tag while the version "
            "inside the image comes from the package metadata, so publishing this "
            "would ship an image whose label and contents disagree."
        )
    return version


def main(argv: list[str] | None = None) -> int:
    """Run the check. Returns 0 when the sources agree, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="a git tag to validate as well, for example v0.4.0. Without it only the "
        "in-repository sources are compared.",
    )
    args = parser.parse_args(argv)

    try:
        version = check_tag(args.tag) if args.tag else check_sources()
    except (VersionMismatch, OSError) as exc:
        print(f"version sources disagree: {exc}", file=sys.stderr)
        return 1

    if args.tag:
        print(f"버전 일치: {args.tag} == pyproject {version}")
    else:
        note = "  (개발 버전 — 태그로 발행할 수 없다)" if is_prerelease(version) else ""
        print(f"버전 일치: pyproject {version} == CHANGELOG{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

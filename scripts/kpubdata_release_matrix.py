#!/usr/bin/env python3
"""List every released kpubdata version Builder declares support for (#832).

Compatibility is a claim about released versions (Independence Rules 11 and 12), so CI
tests Builder against each one inside the range ``pyproject.toml`` declares — not only
the floor, and not kpubdata ``main``. This reads the range from ``pyproject.toml`` and
the releases from PyPI's JSON API, drops yanked and pre-release versions, and prints a
JSON list for a GitHub Actions matrix.

It fails rather than shrink the matrix: when PyPI cannot be read, when the floor is
not among the releases, or when nothing is left.

``--exclude-locked uv.lock`` then leaves out the version ``uv.lock`` resolves. The
ordinary test matrix already installs that one, so testing it here as well runs the
same suite against the same packages twice. What remains may be empty — the matrix job
is then skipped — but the checks above still see the whole list first.

Usage:
    python scripts/kpubdata_release_matrix.py            # prints ["0.7.0", ...]
    python scripts/kpubdata_release_matrix.py --exclude-locked uv.lock
    python scripts/kpubdata_release_matrix.py --releases releases.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 has tomli through pytest
    import tomli as tomllib

PYPI_URL = "https://pypi.org/pypi/kpubdata/json"


def declared_range(pyproject: Path) -> SpecifierSet:
    """The ``kpubdata`` specifier from ``[project].dependencies``."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for raw in data["project"]["dependencies"]:
        requirement = Requirement(raw)
        if requirement.name == "kpubdata":
            return requirement.specifier
    raise SystemExit(f"error: no kpubdata dependency in {pyproject}")


def _floor(specifier: SpecifierSet) -> Version | None:
    floors = [Version(s.version) for s in specifier if s.operator in (">=", "==", "~=")]
    return max(floors) if floors else None


def supported_releases(
    releases: Mapping[str, list[Mapping[str, Any]]], specifier: SpecifierSet
) -> list[str]:
    """Released, unyanked, final versions inside ``specifier``, oldest first."""
    found: list[Version] = []
    for raw, files in releases.items():
        try:
            version = Version(raw)
        except InvalidVersion:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        # A release with no files, or only yanked ones, cannot be installed.
        if not files or all(f.get("yanked", False) for f in files):
            continue
        if version in specifier:
            found.append(version)
    found.sort()

    floor = _floor(specifier)
    if floor is not None and floor not in found:
        raise SystemExit(f"error: the declared floor {floor} is not an installable release")
    if not found:
        raise SystemExit(f"error: no kpubdata release satisfies {specifier}")
    return [str(v) for v in found]


def locked_version(lock: Path) -> str:
    """The ``kpubdata`` version ``uv.lock`` resolves."""
    data = tomllib.loads(lock.read_text(encoding="utf-8"))
    for package in data.get("package", []):
        if package.get("name") == "kpubdata":
            return str(Version(package["version"]))
    raise SystemExit(f"error: no kpubdata package in {lock}")


def _fetch() -> dict[str, list[dict[str, Any]]]:
    with urllib.request.urlopen(PYPI_URL, timeout=30) as response:  # noqa: S310 - fixed https URL
        payload = json.load(response)
    return dict(payload["releases"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--releases", type=Path, help="PyPI 'releases' mapping as JSON (tests)")
    parser.add_argument(
        "--exclude-locked",
        type=Path,
        metavar="UV_LOCK",
        help="leave out the version this lock file resolves (the test matrix runs it)",
    )
    args = parser.parse_args(argv)

    specifier = declared_range(args.pyproject)
    releases = json.loads(args.releases.read_text(encoding="utf-8")) if args.releases else _fetch()
    versions = supported_releases(releases, specifier)
    if args.exclude_locked:
        locked = locked_version(args.exclude_locked)
        versions = [v for v in versions if v != locked]
    print(json.dumps(versions))
    return 0


if __name__ == "__main__":
    sys.exit(main())

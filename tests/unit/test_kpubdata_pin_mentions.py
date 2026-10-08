"""A kpubdata version range written outside ``pyproject.toml`` is the one in it (#1193).

The pin moved from 0.6 to 0.9 while comments in the Dockerfile and a script still gave
the range it had left. A range repeated in prose is right on the day it is written;
this fails the day the pin moves and the prose does not.

Covered: the files an operator or a contributor reads to find out what is installed.
Not covered: the history (``CHANGELOG.md``, ``ROADMAP.md``, the ADRs), where an old
range is what was true then; and ``.github/workflows``, which are listed in the issue
and left out here until they are corrected — see the test at the bottom.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

#: ``>=0.9.0,<0.10``, with or without ``kpubdata`` in front.
_RANGE = re.compile(r">=\s*\d+\.\d+(?:\.\d+)?\s*,\s*<\s*\d+\.\d+(?:\.\d+)?")
#: A line is about the kpubdata pin when it says so.
_ABOUT_THE_PIN = re.compile(r"kpubdata|핀|\bpin", re.IGNORECASE)

_CURRENT = [
    "Dockerfile",
    "docker-entrypoint.sh",
    "docker-compose.prod.app.yml",
    "README.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "docs/deploy.md",
    "docs/deployment.md",
    *sorted(str(path.relative_to(_ROOT)) for path in (_ROOT / "scripts").glob("*.py")),
    *sorted(str(path.relative_to(_ROOT)) for path in (_ROOT / "scripts").glob("*.sh")),
]


def _pinned_range() -> str:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    (requirement,) = [
        dependency
        for dependency in project["dependencies"]
        if re.match(r"kpubdata\s*[<>=!~]", dependency)
    ]
    return _normal(requirement.removeprefix("kpubdata"))


def _normal(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _mentions(path: Path) -> list[tuple[int, str]]:
    """Each kpubdata range a file names, with the line it is on."""
    found: list[tuple[int, str]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if _ABOUT_THE_PIN.search(line):
            found += [(number, _normal(match)) for match in _RANGE.findall(line)]
    return found


def test_the_pin_is_one_range() -> None:
    assert _RANGE.fullmatch(_pinned_range())


@pytest.mark.parametrize("name", _CURRENT)
def test_a_range_named_in_a_current_file_is_the_pinned_one(name: str) -> None:
    path = _ROOT / name
    if not path.exists():
        pytest.skip(f"{name} is not in this repository")
    pinned = _pinned_range()

    stale = [f"{name}:{line}: {found}" for line, found in _mentions(path) if found != pinned]

    assert not stale, f"pyproject.toml pins kpubdata{pinned}; these say otherwise: {stale}"


def test_the_check_reads_a_range_however_it_is_written(tmp_path: Path) -> None:
    sample = tmp_path / "sample.txt"
    sample.write_text(
        "the kpubdata pin (kpubdata>=0.8.0,<0.9) is old\n"
        "pyproject 핀(>= 0.6.0, < 0.7)대로 설치한다\n"
        "python>=3.10,<3.14 is not about it\n"
        "kpubdata 0.9 has no range here\n",
        encoding="utf-8",
    )

    assert _mentions(sample) == [(1, ">=0.8.0,<0.9"), (2, ">=0.6.0,<0.7")]


def test_the_workflows_are_the_known_exception() -> None:
    """Two workflow comments still give an old range (#1193).

    They are named here so that the gap is on record and closes itself: when they are
    corrected this fails, and the workflows can then join ``_CURRENT`` above.
    """
    pinned = _pinned_range()
    stale = sorted(
        {
            path.name
            for path in (_ROOT / ".github" / "workflows").glob("*.yml")
            for _line, found in _mentions(path)
            if found != pinned
        }
    )

    # By file, not by line: other changes to a workflow move its lines.
    assert stale == ["ci.yml", "cubrid.yml"]

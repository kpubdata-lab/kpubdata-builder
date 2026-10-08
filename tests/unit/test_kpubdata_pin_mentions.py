"""A kpubdata version range written outside ``pyproject.toml`` is the one in it (#1193).

The pin moved from 0.6 to 0.9 while comments in the Dockerfile and a script still gave
the range it had left. A range repeated in prose is right on the day it is written;
this fails the day the pin moves and the prose does not.

Every file git tracks is read (``git ls-files``, so a file added or renamed is covered
without anyone listing it). Two kinds are left out, each for a reason written below:
the history, where an old range is what was true then, and the files that are known to
be stale and cannot be corrected by the same change.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

#: ``>=0.9.0,<0.10``, with or without ``kpubdata`` in front.
_RANGE = re.compile(r">=\s*\d+\.\d+(?:\.\d+)?\s*,\s*<\s*\d+\.\d+(?:\.\d+)?")
#: A line is about the kpubdata pin when it says so.
_ABOUT_THE_PIN = re.compile(r"kpubdata|핀|\bpin", re.IGNORECASE)

#: Where an old range is a fact about the past, not a claim about now.
_HISTORY = ("CHANGELOG.md", "ROADMAP.md", "docs/adrs/")

#: This file: its samples are old ranges on purpose.
_SELF = "tests/unit/test_kpubdata_pin_mentions.py"

#: Files that still give an old range. Workflow files need a push with a scope the
#: change that found them did not have. Correcting one means taking it off this list
#: in the same change: the test below fails for a name left here that is no longer
#: stale, and for a stale file that is not here.
KNOWN_STALE: frozenset[str] = frozenset(
    {
        ".github/workflows/ci.yml",
        ".github/workflows/cubrid.yml",
    }
)


def _pinned_range() -> str:
    # tomllib is 3.11+ and this project supports 3.10, so the line is read as text.
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    (requirement,) = re.findall(r'^\s*"kpubdata\s*([<>=!~][^"]*)",?\s*$', text, re.MULTILINE)
    return _normal(requirement)


def _normal(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _mentions(path: Path) -> list[tuple[int, str]]:
    """Each kpubdata range a file names, with the line it is on."""
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return []
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _ABOUT_THE_PIN.search(line):
            found += [(number, _normal(match)) for match in _RANGE.findall(line)]
    return found


def _tracked() -> list[str]:
    """Every file git tracks, as the repository names it."""
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    return sorted(name for name in listed.stdout.split("\0") if name)


def _is_history(name: str) -> bool:
    return any(name == entry or name.startswith(entry) for entry in _HISTORY)


def _stale_files() -> dict[str, list[str]]:
    """Each current file that names a range other than the pinned one, with where."""
    pinned = _pinned_range()
    stale: dict[str, list[str]] = {}
    for name in _tracked():
        path = _ROOT / name
        # A symlink is another name for a file that is read under its own.
        if name == _SELF or _is_history(name) or path.is_symlink() or not path.is_file():
            continue
        wrong = [f"{name}:{line}: {found}" for line, found in _mentions(path) if found != pinned]
        if wrong:
            stale[name] = wrong
    return stale


def test_the_pin_is_one_range() -> None:
    assert _RANGE.fullmatch(_pinned_range())


def test_the_sweep_reads_the_files_this_was_written_for() -> None:
    """Not an empty sweep: the pin's own file and the ones that had gone stale are in it."""
    tracked = set(_tracked())

    expected = {"pyproject.toml", "Dockerfile", "scripts/release_facts.py", "CONTRIBUTING.md"}
    assert tracked >= expected
    assert tracked >= KNOWN_STALE
    assert {found for _line, found in _mentions(_ROOT / "pyproject.toml")} == {_pinned_range()}
    assert _mentions(_ROOT / "CONTRIBUTING.md"), "CONTRIBUTING.md names the pin"


def test_no_current_file_names_a_range_other_than_the_pinned_one() -> None:
    stale = _stale_files()

    unexpected = {name: where for name, where in stale.items() if name not in KNOWN_STALE}

    assert not unexpected, (
        f"pyproject.toml pins kpubdata{_pinned_range()}; these say otherwise: "
        f"{sorted(line for where in unexpected.values() for line in where)}"
    )


def test_a_file_that_was_corrected_is_taken_off_the_known_list() -> None:
    """So the list does not go on excusing a file that no longer needs it."""
    no_longer_stale = sorted(KNOWN_STALE - set(_stale_files()))

    assert not no_longer_stale, f"corrected, so remove from KNOWN_STALE: {no_longer_stale}"


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


def test_history_is_what_was_true_then() -> None:
    assert _is_history("CHANGELOG.md")
    assert _is_history("docs/adrs/0007-kpubdata-version-compatibility-policy.md")
    assert not _is_history("CONTRIBUTING.md")
    assert not _is_history("docs/deploy.md")

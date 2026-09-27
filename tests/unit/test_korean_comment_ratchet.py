"""The Korean comment ratchet must actually fail when debt grows (#710).

A gate nobody has seen fail is a gate nobody knows works. ADR 0003's rule sat in
AGENTS.md unenforced while thousands of Korean comments accumulated, so the
negative case is the test that matters here.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_korean_comments.py"


def _load_module(repo_root: Path, baseline: Path) -> Any:
    """Load the checker with its roots pointed at a temporary repository."""
    spec = importlib.util.spec_from_file_location("_korean_ratchet", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO_ROOT = repo_root
    module.BASELINE_PATH = baseline
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repository tree with a src directory."""
    (tmp_path / "src").mkdir()
    return tmp_path


def test_english_only_file_passes(repo: Path, tmp_path: Path) -> None:
    """A file with English comments only is counted as zero."""
    module = _load_module(repo, tmp_path / "baseline.json")
    (repo / "src" / "clean.py").write_text(
        '"""A module docstring in English."""\n\n# An English comment.\nX = 1\n',
        encoding="utf-8",
    )
    assert module.scan([repo / "src"]) == {}


def test_korean_comment_and_docstring_are_both_found(repo: Path, tmp_path: Path) -> None:
    """Both a Korean comment and a Korean docstring count."""
    module = _load_module(repo, tmp_path / "baseline.json")
    (repo / "src" / "dirty.py").write_text(
        '"""한국어 독스트링."""\n\n# 한국어 주석\nX = 1\n', encoding="utf-8"
    )
    assert module.scan([repo / "src"]) == {"src/dirty.py": 2}


def test_hash_inside_a_string_is_not_a_comment(repo: Path, tmp_path: Path) -> None:
    """A ``#`` inside a string literal is not a comment.

    This is why the check tokenizes instead of scanning lines.
    """
    module = _load_module(repo, tmp_path / "baseline.json")
    (repo / "src" / "strings.py").write_text(
        'TITLE = "# 한국어 제목"\nBODY = "설명"\n', encoding="utf-8"
    )
    assert module.scan([repo / "src"]) == {}


def test_bare_korean_string_is_not_a_docstring(repo: Path, tmp_path: Path) -> None:
    """A Korean string that is not a docstring is out of scope.

    User-visible strings are decided separately (ADR 0003), so only the first
    statement of a module, class or function counts.
    """
    module = _load_module(repo, tmp_path / "baseline.json")
    (repo / "src" / "bare.py").write_text(
        '"""English docstring."""\n\nX = 1\n"블록 주석처럼 쓴 문자열"\n', encoding="utf-8"
    )
    assert module.scan([repo / "src"]) == {}


def test_a_named_file_is_examined(repo: Path, tmp_path: Path) -> None:
    """Passing a file path has to work, not only a directory.

    An earlier version of this tool only walked directories, so naming a file on
    the command line silently examined nothing.
    """
    module = _load_module(repo, tmp_path / "baseline.json")
    target = repo / "src" / "named.py"
    target.write_text("# 한국어 주석\nX = 1\n", encoding="utf-8")
    assert module.scan([target]) == {"src/named.py": 1}


def test_new_debt_fails(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file absent from the baseline with Korean comments fails the check.

    This is the case that would have caught the violation that prompted #710.
    """
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 0, "counts": {}}), encoding="utf-8")
    module = _load_module(repo, baseline)
    (repo / "src" / "new.py").write_text("# 새로 들어온 한국어 주석\nX = 1\n", encoding="utf-8")

    assert module.main([str(repo / "src")]) == 1
    assert "src/new.py" in capsys.readouterr().err


def test_growing_debt_fails(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A baselined file that gains one more Korean comment fails."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 1, "counts": {"src/legacy.py": 1}}), encoding="utf-8")
    module = _load_module(repo, baseline)
    (repo / "src" / "legacy.py").write_text(
        "# 기존 한국어 주석\n# 새로 늘어난 한국어 주석\nX = 1\n", encoding="utf-8"
    )

    assert module.main([str(repo / "src")]) == 1
    assert "2건" in capsys.readouterr().err


def test_debt_at_baseline_passes(repo: Path, tmp_path: Path) -> None:
    """Frozen debt is allowed to stay. The ratchet only blocks growth."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 1, "counts": {"src/legacy.py": 1}}), encoding="utf-8")
    module = _load_module(repo, baseline)
    (repo / "src" / "legacy.py").write_text("# 기존 한국어 주석\nX = 1\n", encoding="utf-8")
    assert module.main([str(repo / "src")]) == 0


def test_shrinking_debt_passes(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Translating a comment passes and the reduction is reported."""
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"total": 2, "counts": {"src/legacy.py": 2}}), encoding="utf-8")
    module = _load_module(repo, baseline)
    (repo / "src" / "legacy.py").write_text("# 기존 한국어 주석\nX = 1\n", encoding="utf-8")

    assert module.main([str(repo / "src")]) == 0
    assert "줄었다" in capsys.readouterr().out


def test_unparseable_file_does_not_break_the_run(repo: Path, tmp_path: Path) -> None:
    """A file that cannot be parsed is the linter's problem, not this check's."""
    module = _load_module(repo, tmp_path / "baseline.json")
    (repo / "src" / "broken.py").write_text("def f(:\n", encoding="utf-8")
    assert module.scan([repo / "src"]) == {}

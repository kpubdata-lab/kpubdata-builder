"""The plain-codes gate has to actually fail (#878).

POLICY 0.1: a review level (R0..R3) or verification level (V0..V5) is written
meaning first, code in parentheses. Most of these tests plant a bare code and
expect the gate to find it; the rest show the forms it must leave alone.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "check_plain_codes.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_plain_codes", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MODULE = _load()


def _codes(text: str, *, changelog: bool = False) -> list[str]:
    return [code for _, code, _ in _MODULE.bare_codes(text, changelog=changelog)]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Required Verification: V2\n", ["V2"]),
        ("이 변경은 R3 로 취급한다.\n", ["R3"]),
        ("단위 테스트를V1로 적었다.\n", ["V1"]),
        ("| #461 | High | V2+V3 |\n", ["V2", "V3"]),
        ("needs V5-replay before merge\n", ["V5-replay"]),
        ("V5-live 는 release 용이다.\n", ["V5-live"]),
        ("R0~R3 정의\n", ["R0", "R3"]),
        ("## 14.1 R3 는 승인이 필요하다\n", ["R3"]),
        # Parentheses with nothing before them gloss nothing.
        ("| (V2) |\n", ["V2"]),
        ("(R1) at the start of a line\n", ["R1"]),
    ],
)
def test_a_bare_code_is_found(text: str, expected: list[str]) -> None:
    assert _codes(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "단위 테스트(V1)\n",
        "unit tests (V1)\n",
        "작성자가 아닌 사람의 승인이 필요(R3)\n",
        "계약·통합 테스트(V2+V3), 영향 데이터셋은 실제 API 검증(V4)\n",
        "리뷰 수준(R0~R3) 정의\n",
        "**무거운 변경**(R3, 25절)\n",
        "the required `R3 review` check\n",
        "the label review:R3 stays\n",
        "see [R3](docs/governance/POLICY.md#codes)\n",
        "Cloudflare R2 bucket\n",
        "API v2 and v3 and utf-8\n",
        "kpubdata-R2 and 1.V2 and R4 and V6 and R10 and V12\n",
        "```text\nRequired Verification: V4\n```\n",
        "~~~\nR3\n~~~\n",
        "<!-- plain-codes: off -->\n| V1 | 단위 테스트 |\n<!-- plain-codes: on -->\n",
    ],
)
def test_a_glossed_or_literal_code_passes(text: str) -> None:
    assert _codes(text) == []


def test_a_link_away_from_the_glossary_does_not_explain_the_code() -> None:
    assert _codes("see [R3](docs/other.md)\n") == ["R3"]


def test_checking_resumes_after_the_off_region() -> None:
    text = "<!-- plain-codes: off -->\nV1\n<!-- plain-codes: on -->\nV2\n"
    assert _codes(text) == ["V2"]


def test_checking_resumes_after_a_fence() -> None:
    assert _codes("```\nV1\n```\nV2\n") == ["V2"]


def test_the_changelog_is_checked_in_its_unreleased_section_only() -> None:
    text = "# Changelog\n\nV0 intro\n\n## [Unreleased]\n\n- bare V2\n\n## [0.9.0]\n\n- old R3\n"
    assert _codes(text, changelog=True) == ["V2"]


def _git_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "--", *files], check=True)
    return tmp_path


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_tracked_document_with_a_bare_code_fails(tmp_path: Path) -> None:
    root = _git_repo(
        tmp_path,
        {
            "docs/plan.md": "Required Verification: 단위 테스트(V1)\n",
            ".github/ISSUE_TEMPLATE/bug.yml": "description: needs V2\n",
        },
    )
    result = _run(root)
    assert result.returncode == 1
    assert ".github/ISSUE_TEMPLATE/bug.yml:1: V2" in result.stderr
    assert "docs/plan.md" not in result.stderr


def test_an_untracked_document_is_not_read(tmp_path: Path) -> None:
    root = _git_repo(tmp_path, {"README.md": "단위 테스트(V1)\n"})
    (root / "scratch.md").write_text("bare V2\n", encoding="utf-8")
    assert _run(root).returncode == 0


def test_no_tracked_document_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    root = _git_repo(tmp_path, {"code.py": "x = 1\n"})
    assert _run(root).returncode == 1


def test_a_named_file_that_does_not_exist_is_a_failure_not_a_pass(tmp_path: Path) -> None:
    root = _git_repo(tmp_path, {"README.md": "단위 테스트(V1)\n"})
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--root", str(root), "README.md", "docs/missing.md"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "docs/missing.md: no such file" in result.stderr


def test_this_repository_passes() -> None:
    result = _run(REPO_ROOT)
    assert result.returncode == 0, result.stderr

"""Lock CLI help and usage guide so they don't diverge.

`docs/guides/cli-usage.md` 는 한때 서브커맨드 4개만 적고 "별도 serve 명령은
없습니다" 라고 단언했는데, 그 시점에 파서에는 11개가 있었고 `serve` 도 그중
하나였다. 사람이 손으로 맞추는 목록은 반드시 낡는다 — 테스트가 대조한다.

`test_env_var_contract.py`(환경변수)와 `test_version.py`(버전 SSOT)가 이미 쓰는
패턴을 문서에까지 넓힌 것이다.
"""

from __future__ import annotations

import re
from pathlib import Path

from kpubdata_builder.cli import build_parser

_DOC = Path(__file__).parents[2] / "docs" / "guides" / "cli-usage.md"


def _parser_subcommands() -> set[str]:
    parser = build_parser()
    names: set[str] = set()
    for action in parser._subparsers._group_actions if parser._subparsers else []:
        choices = getattr(action, "choices", None)
        if choices:
            names.update(choices)
    return names


def _documented_subcommands() -> set[str]:
    text = _DOC.read_text(encoding="utf-8")
    block = text.split("positional arguments:", 1)[1].split("options:", 1)[0]
    # Extract name only from lines like "    name  Help text...". Following description lines
    # have deeper indent and don't match.
    return set(re.findall(r"^ {4}([a-z][a-z-]+) {2,}\S", block, flags=re.MULTILINE))


def test_every_subcommand_is_documented() -> None:
    missing = _parser_subcommands() - _documented_subcommands()

    assert not missing, f"cli-usage.md 에 없는 서브커맨드: {sorted(missing)}"


def test_no_documented_subcommand_has_been_removed() -> None:
    stale = _documented_subcommands() - _parser_subcommands()

    assert not stale, f"cli-usage.md 가 존재하지 않는 서브커맨드를 설명한다: {sorted(stale)}"


def test_the_guide_does_not_claim_serve_is_missing() -> None:
    # This sentence was actually in docs and parser had serve at that time.
    assert "별도 CLI `serve` 명령은 없습니다" not in _DOC.read_text(encoding="utf-8")

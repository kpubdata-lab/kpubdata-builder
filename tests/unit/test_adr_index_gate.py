"""The ADR index gate has to actually refuse (#1110).

The index said ADR 0008, 0010 and 0011 were still proposed while each ADR called itself
accepted. A gate nobody has watched fail is a gate nobody knows works, so most of these
give it an index that disagrees and expect it to say so.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "check_adr_index.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_adr_index_gate", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load()

_HEADER = "| ADR | 제목 | 상태 | 관련 이슈 |\n| :--- | :--- | :--- | :--- |\n"


def _adrs(tmp_path: Path, index: dict[str, str], files: dict[str, str | None]) -> Path:
    """An ADR directory: ``index`` is ``{file name: status text in the table}`` and
    ``files`` is ``{file name: status line text}`` — None for an ADR with no status line."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    rows = "".join(
        f"| [{name[:4]}](./{name}) | a title | {status} | #1 |\n" for name, status in index.items()
    )
    (tmp_path / "README.md").write_text(f"# ADRs\n\n{_HEADER}{rows}", encoding="utf-8")
    for name, status in files.items():
        line = "" if status is None else f"- 상태: {status}\n"
        (tmp_path / name).write_text(
            f"# ADR {name[:4]} — title\n\n{line}- 관련 이슈: #1\n", "utf-8"
        )
    return tmp_path


def test_this_repositorys_index_matches_its_adrs() -> None:
    assert gate.check(gate.ADR_DIR) == []
    assert gate.main([]) == 0


def test_every_adr_file_is_listed_and_states_a_known_status() -> None:
    files = sorted(p.name for p in gate.ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
    rows = gate.index_rows(gate.ADR_DIR / "README.md")

    assert [rows[name[:4]][0] for name in files] == files
    for name in files:
        own = gate.adr_status(gate.ADR_DIR / name)
        assert own is not None and gate.status_word(own) is not None, name


def test_the_mismatch_this_gate_was_written_for_is_refused(tmp_path: Path) -> None:
    """The index says proposed; the ADR says accepted and implemented."""
    directory = _adrs(
        tmp_path,
        {"0008-async.md": "제안됨"},
        {"0008-async.md": "승인됨(Accepted) — 구현 완료"},
    )

    problems = gate.check(directory)

    assert len(problems) == 1
    assert "ADR 0008" in problems[0] and "proposed" in problems[0] and "accepted" in problems[0]
    assert gate.main([str(directory)]) == 1


@pytest.mark.parametrize(
    ("listed", "own"),
    [
        ("승인됨", "제안됨(Proposed)"),
        ("승인됨", "대체됨(Superseded by ADR 0015)"),
        ("대체됨(0015)", "승인됨(Accepted)"),
        ("제안됨", "수용됨(Accepted)"),
    ],
)
def test_any_two_different_statuses_are_refused(tmp_path: Path, listed: str, own: str) -> None:
    directory = _adrs(tmp_path, {"0001-x.md": listed}, {"0001-x.md": own})

    assert len(gate.check(directory)) == 1


@pytest.mark.parametrize(
    ("listed", "own"),
    [
        ("승인됨", "승인됨(Accepted)"),
        ("승인됨 (2026-09-30 개정: 다중 사용자 규칙)", "승인됨 — **그러나 제품 원칙과 충돌한다**"),
        ("승인됨", "수용됨(Accepted)"),
        ("대체됨(0015)", "대체됨(Superseded by [ADR 0015](./0015-x.md)) — 승계"),
        ("제안됨(소유자 ADR 검토)", "제안됨 — 2026-09-30 소유자 실행 계획을 옮긴 것"),
        ("**승인됨**", "승인됨"),
    ],
)
def test_the_same_status_with_different_notes_passes(tmp_path: Path, listed: str, own: str) -> None:
    """Negative: what follows the status word is free text and is not compared."""
    directory = _adrs(tmp_path, {"0001-x.md": listed}, {"0001-x.md": own})

    assert gate.check(directory) == []


def test_an_adr_file_with_no_row_is_refused(tmp_path: Path) -> None:
    directory = _adrs(
        tmp_path, {"0001-x.md": "승인됨"}, {"0001-x.md": "승인됨", "0002-y.md": "제안됨"}
    )

    assert gate.check(directory) == ["ADR 0002: 0002-y.md has no row in the index"]


def test_a_row_with_no_file_is_refused(tmp_path: Path) -> None:
    directory = _adrs(
        tmp_path, {"0001-x.md": "승인됨", "0002-y.md": "제안됨"}, {"0001-x.md": "승인됨"}
    )

    assert gate.check(directory) == ["ADR 0002: the index lists it but there is no such file"]


def test_a_row_that_links_another_file_name_is_refused(tmp_path: Path) -> None:
    directory = _adrs(tmp_path, {"0001-old-name.md": "승인됨"}, {"0001-new-name.md": "승인됨"})

    problems = gate.check(directory)

    assert problems == ["ADR 0001: the index links 0001-old-name.md, the file is 0001-new-name.md"]


def test_an_adr_without_a_status_line_is_refused(tmp_path: Path) -> None:
    directory = _adrs(tmp_path, {"0001-x.md": "승인됨"}, {"0001-x.md": None})

    assert gate.check(directory) == ["ADR 0001: 0001-x.md has no '- 상태:' line"]


@pytest.mark.parametrize("unknown", ["검토 중", "Accepted", "보류"])
def test_a_status_word_the_gate_does_not_know_is_refused(tmp_path: Path, unknown: str) -> None:
    """A new word is added to ``STATUS_WORDS`` on purpose, not accepted by default."""
    in_the_adr = gate.check(_adrs(tmp_path / "a", {"0001-x.md": "승인됨"}, {"0001-x.md": unknown}))
    in_the_index = gate.check(
        _adrs(tmp_path / "b", {"0001-x.md": unknown}, {"0001-x.md": "승인됨"})
    )

    assert len(in_the_adr) == 1 and "unknown status" in in_the_adr[0]
    assert len(in_the_index) == 1 and "unknown status" in in_the_index[0]


def test_a_directory_with_no_index_is_refused(tmp_path: Path) -> None:
    (tmp_path / "0001-x.md").write_text("# ADR\n\n- 상태: 승인됨\n", encoding="utf-8")

    assert len(gate.check(tmp_path)) == 1
    assert gate.main([str(tmp_path)]) == 1


def test_the_index_explains_the_rule_it_is_held_to() -> None:
    readme = (gate.ADR_DIR / "README.md").read_text(encoding="utf-8")

    assert "scripts/check_adr_index.py" in readme
    assert "tests/unit/test_adr_index_gate.py" in readme

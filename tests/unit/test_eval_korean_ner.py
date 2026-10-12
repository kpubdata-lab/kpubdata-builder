"""The Korean name/address candidate evaluation keeps its own promises (#1266).

The script is an evaluation, not a runtime feature, but three of its claims carry the
report: the corpus is fake by construction, a candidate detector can only add to what
the PII gate's scanner finds, and nothing it prints holds a cell value.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "eval_korean_ner.py"


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("_eval_korean_ner", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses look the module up by name while the class body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ner = _load()


def test_corpus_is_deterministic() -> None:
    assert ner.build_corpus(7, 300) == ner.build_corpus(7, 300)
    assert ner.build_corpus(7, 300) != ner.build_corpus(8, 300)


def test_resident_number_shapes_are_never_valid_dates() -> None:
    corpus = ner.build_corpus(1266, 5000)
    rrns = [c.value.replace("-", "") for c in corpus if c.category.startswith("rrn")]
    assert rrns
    for value in rrns:
        assert 13 <= int(value[2:4]) <= 99


def test_mobile_numbers_use_an_unassigned_block() -> None:
    corpus = ner.build_corpus(1266, 5000)
    phones = [re.sub(r"\D", "", c.value) for c in corpus if c.category.startswith("phone")]
    assert phones
    for digits in phones:
        assert digits.startswith("0100")


def test_a_candidate_never_removes_a_baseline_finding() -> None:
    corpus = ner.build_corpus(1266, 5000)
    flagged_before = 0
    flagged_after = 0
    for cell in corpus:
        base = ner.baseline_scan(cell)
        combined = ner.combine(base, ner.rules_detector(cell.value))
        assert base <= combined
        flagged_before += bool(base)
        flagged_after += bool(combined)
    assert flagged_after >= flagged_before


def test_combine_keeps_baseline_when_candidate_finds_nothing() -> None:
    base = frozenset({"rrn", "phone"})
    assert ner.combine(base, frozenset()) == base


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("김민준", {"name"}),
        ("남궁서연", {"name"}),
        ("박서연 님 문의", {"name"}),
        ("서울특별시 강남구 중앙로 12", {"address"}),
        ("부산 해운대구 송정동 12-3", {"address"}),
        ("경기도 수원시 시청로 １２３", {"address"}),
        ("담당자 최지우 (010-0123-4567)", {"name"}),
    ],
)
def test_rules_detector_positives(value: str, expected: set[str]) -> None:
    assert ner.rules_detector(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "강남구청",
        "기획재정부",
        "민원봉사과",
        "서울역",
        "김포공항",
        "2024-01-15",
        "11680",
        "해당없음",
    ],
)
def test_rules_detector_hard_negatives(value: str) -> None:
    assert ner.rules_detector(value) == frozenset()


def test_rules_detector_reports_known_false_positives() -> None:
    # Three-syllable words that start with a surname read as names. The report counts
    # them; this pins that the rule does not hide them.
    assert ner.rules_detector("한마음") == {"name"}


def test_output_holds_no_cell_values(capsys: pytest.CaptureFixture[str]) -> None:
    assert ner.main(["--cells", "400", "--seed", "3"]) == 0
    out = capsys.readouterr().out
    hangul = {c.value for c in ner.build_corpus(3, 400) if re.search(r"[가-힣]", c.value)}
    assert hangul
    assert not [value for value in hangul if value in out]

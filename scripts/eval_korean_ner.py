#!/usr/bin/env python3
"""Evaluate optional Korean name/address candidate detectors against the PII baseline (#1266).

Everything here is an evaluation, not a runtime feature. The corpus is generated from a
seed and is synthetic by construction:

- resident-number-shaped values use a month of 13..99, so none is a valid date and none
  can be a real resident registration number;
- mobile numbers use a middle block starting with 0 (``010-0xxx-xxxx``), a range that is
  not assigned to subscribers;
- names are a surname plus two syllables drawn from fixed lists, and addresses join a
  region with a common road or neighbourhood name and a random number. They point at
  no record and are not linked to any other value.

The baseline is the scanner the PII gate runs today (``stages/silver/pii.py``): value
patterns plus the column-name heuristic. Each candidate detector is measured alone and
as ``baseline | candidate``, which is the only way a detector may be combined with the
gate: it can add a finding, never remove one (``combine``).

Only aggregate numbers are printed. Cell values are never written out unless
``--show-errors`` is passed, and even then they are the synthetic values above.

Optional backends (spaCy with ``ko_core_news_sm``, Kiwi) are measured only when
importable. Install them script-locally, without touching the project's dependencies
or ``uv.lock``; the exact command is in ``docs/pii-korean-ner-evaluation.md``.

Usage:
    python scripts/eval_korean_ner.py [--seed N] [--cells N] [--json PATH] [--show-errors]
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import random
import re
import resource
import sys
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from kpubdata_builder.stages.silver.pii import (  # noqa: E402
    VALUE_PATTERNS,
    suspect_column_kind,
)

#: The kinds the corpus labels. The baseline also reports ``email`` and
#: ``business_no``; the corpus holds neither, so they only count as false positives.
KINDS: tuple[str, ...] = ("name", "address", "phone", "rrn")

Detector = Callable[[str], frozenset[str]]

# ---------------------------------------------------------------------------
# Synthetic corpus
# ---------------------------------------------------------------------------

SURNAMES: tuple[str, ...] = (
    "김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신",
    "권", "황", "안", "송", "류", "전", "홍", "고", "문", "양", "손", "배", "백", "허",
    "유", "남", "심", "노", "하", "곽", "성", "차", "주", "우", "구", "민",
)  # fmt: skip
COMPOUND_SURNAMES: tuple[str, ...] = ("남궁", "황보", "제갈", "선우", "독고")
GIVEN_SYLLABLES: tuple[str, ...] = (
    "민", "준", "서", "연", "지", "우", "현", "수", "영", "진", "하", "윤", "은", "호",
    "도", "예", "성", "재", "원", "아", "린", "율", "건", "태", "희", "혜", "경", "미",
    "석", "훈", "빈", "다", "솔", "나",
)  # fmt: skip
SIDO: tuple[str, ...] = (
    "서울특별시", "부산광역시", "대구광역시", "인천광역시", "광주광역시", "대전광역시",
    "울산광역시", "세종특별자치시", "경기도", "강원특별자치도", "충청북도", "충청남도",
    "전북특별자치도", "전라남도", "경상북도", "경상남도", "제주특별자치도",
)  # fmt: skip
SIDO_SHORT: tuple[str, ...] = (
    "서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "경기", "강원",
    "충북", "충남", "전북", "전남", "경북", "경남", "제주",
)  # fmt: skip
SIGUNGU: tuple[str, ...] = (
    "중구", "동구", "서구", "남구", "북구", "강남구", "종로구", "마포구", "해운대구",
    "수성구", "연수구", "유성구", "수원시", "성남시", "청주시", "전주시", "춘천시",
    "창원시", "포항시", "양평군", "홍천군",
)  # fmt: skip
ROADS: tuple[str, ...] = (
    "중앙로", "시청로", "공원로", "역전로", "대학로", "문화로", "산업로", "번영로",
    "중앙로12번길", "시장길", "학교길",
)  # fmt: skip
DONGS: tuple[str, ...] = ("신촌동", "역삼동", "중앙동", "송정동", "효자동", "연산동")

# Hard negatives: organisations, departments, places and words that share a surname
# syllable or a suffix with the positives. None of them is personal data.
ORGS: tuple[str, ...] = (
    "기획재정부", "행정안전부", "서울특별시청", "강남구청", "해운대구청", "한국전력공사",
    "국민건강보험공단", "한국토지주택공사", "민원봉사과", "도시계획과", "교통행정과",
    "보건소", "주민센터", "정보통신과", "문화관광과", "신한은행", "하나은행", "우리은행",
)  # fmt: skip
PLACES: tuple[str, ...] = (
    "서울역", "한강공원", "남산타워", "경복궁", "김포공항", "조선호텔", "강서구", "성수대교",
    "신촌역", "강남역", "남대문시장", "서울숲", "고려대학교", "해운대해수욕장", "제주도",
)  # fmt: skip
WORDS: tuple[str, ...] = (
    "이용안내", "정상", "운영중", "해당없음", "휴무", "주차가능", "무료", "유료", "신규",
    "변경", "한마음", "장미꽃", "오미자", "서비스", "신호등", "안내소", "조선소", "전통시장",
    "고객센터", "임시휴업", "백일장", "유기농", "우수상", "최우수", "공영주차장",
)  # fmt: skip
CODES: tuple[str, ...] = (
    "2024-01-15", "11680", "A-2024-000123", "제2024-123호", "37.5665", "126.9780",
    "1588-0000", "02-0000-0000", "3번 출구", "제1주차장", "2024년 3월",
)  # fmt: skip


@dataclass(frozen=True)
class Cell:
    """One synthetic cell: its column, the generator category and the true PII kinds."""

    column: str
    category: str
    value: str
    labels: frozenset[str]


def _name(rng: random.Random) -> str:
    surname = rng.choice(COMPOUND_SURNAMES) if rng.random() < 0.05 else rng.choice(SURNAMES)
    return surname + rng.choice(GIVEN_SYLLABLES) + rng.choice(GIVEN_SYLLABLES)


def _address(rng: random.Random) -> str:
    sido = rng.choice(SIDO) if rng.random() < 0.7 else rng.choice(SIDO_SHORT)
    if rng.random() < 0.6:
        tail = f"{rng.choice(ROADS)} {rng.randint(1, 300)}"
    else:
        tail = f"{rng.choice(DONGS)} {rng.randint(1, 999)}-{rng.randint(1, 30)}"
    address = f"{sido} {rng.choice(SIGUNGU)} {tail}"
    if rng.random() < 0.1:
        # Full-width digits, which some public files carry.
        address = address.translate(str.maketrans("0123456789-", "０１２３４５６７８９－"))
    return address


def _phone(rng: random.Random) -> tuple[str, str]:
    middle = f"0{rng.randint(0, 999):03d}"
    last = f"{rng.randint(0, 9999):04d}"
    style = rng.random()
    if style < 0.6:
        return "phone_dashed", f"010-{middle}-{last}"
    if style < 0.8:
        return "phone_compact", f"010{middle}{last}"
    return "phone_spaced", f"010 {middle} {last}"


def _rrn(rng: random.Random) -> tuple[str, str]:
    # Month 13..99: never a valid date, so never a real resident number.
    front = f"{rng.randint(0, 99):02d}{rng.randint(13, 99):02d}{rng.randint(1, 28):02d}"
    back = f"{rng.randint(1, 4)}{rng.randint(0, 999999):06d}"
    if rng.random() < 0.8:
        return "rrn_dashed", f"{front}-{back}"
    return "rrn_compact", f"{front}{back}"


def _free_text(rng: random.Random) -> Cell:
    pick = rng.randrange(6)
    if pick == 0:
        _, phone = _phone(rng)
        return Cell("비고", "text_name_phone", f"담당자 {_name(rng)} ({phone})",
                    frozenset({"name", "phone"}))  # fmt: skip
    if pick == 1:
        return Cell("비고", "text_name", f"{_name(rng)} 님 문의", frozenset({"name"}))
    if pick == 2:
        return Cell("비고", "text_address", f"{_address(rng)} 소재", frozenset({"address"}))
    if pick == 3:
        return Cell("비고", "text_org", f"{rng.choice(ORGS)} 담당", frozenset())
    if pick == 4:
        return Cell("비고", "text_place", f"{rng.choice(PLACES)} 인근", frozenset())
    return Cell("비고", "text_word", f"{rng.choice(WORDS)} {rng.choice(CODES)}", frozenset())


def build_corpus(seed: int = 1266, cells: int = 10_000) -> list[Cell]:
    """A deterministic synthetic corpus of ``cells`` cells, about half of them positive."""
    rng = random.Random(seed)
    # (weight, factory). Column names are chosen so the column-name heuristic hits some
    # positive columns (the name, address and contact ones), misses others (the person
    # in charge, the location) and hits one organisation column by its NM suffix.
    factories: list[tuple[int, Callable[[], Cell]]] = [
        (8, lambda: Cell("성명", "name", _name(rng), frozenset({"name"}))),
        (8, lambda: Cell("담당자", "name", _name(rng), frozenset({"name"}))),
        (8, lambda: Cell("주소", "address", _address(rng), frozenset({"address"}))),
        (8, lambda: Cell("소재지", "address", _address(rng), frozenset({"address"}))),
        (6, lambda: Cell("연락처", *_phone(rng), frozenset({"phone"}))),
        (4, lambda: Cell("등록번호", *_rrn(rng), frozenset({"rrn"}))),
        (10, lambda: _free_text(rng)),
        (10, lambda: Cell("INST_NM", "org", rng.choice(ORGS), frozenset())),
        (10, lambda: Cell("시설명", "place", rng.choice(PLACES), frozenset())),
        (10, lambda: Cell("상태", "word", rng.choice(WORDS), frozenset())),
        (8, lambda: Cell("코드", "code", rng.choice(CODES), frozenset())),
    ]
    weights = [weight for weight, _ in factories]
    makers = [maker for _, maker in factories]
    return [rng.choices(makers, weights)[0]() for _ in range(cells)]


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def baseline_values(value: str) -> frozenset[str]:
    """The gate's value patterns, unchanged (``VALUE_PATTERNS``)."""
    return frozenset(kind for kind, pattern in VALUE_PATTERNS.items() if pattern.search(value))


def baseline_scan(cell: Cell) -> frozenset[str]:
    """What ``scan_pii`` reports for this cell: value patterns plus the column heuristic."""
    kinds = set(baseline_values(cell.value))
    column_kind = suspect_column_kind(cell.column)
    if column_kind is not None:
        # The heuristic says "addr"; the corpus says "address".
        kinds.add("address" if column_kind == "addr" else column_kind)
    return frozenset(kinds)


def combine(baseline: frozenset[str], candidate: frozenset[str]) -> frozenset[str]:
    """The only allowed combination: a candidate detector adds kinds, never removes one."""
    return baseline | candidate


_TOKEN = re.compile(r"[가-힣]+")
_SURNAME_SET = frozenset(SURNAMES)
_COMPOUND_SET = frozenset(COMPOUND_SURNAMES)
# Endings that mark an organisation, place or thing rather than a person.
_NON_PERSON_SUFFIXES = tuple("청부과원소관역회단사장점시군구동읍면리로길항교터실국처궁산강등꽃")
_HONORIFICS = ("님", "씨")
_SIDO_PATTERN = "|".join(sorted(SIDO + SIDO_SHORT, key=len, reverse=True))
_ADDRESS = re.compile(
    rf"(?:{_SIDO_PATTERN})\s*(?:[가-힣]+[시군구]\s*)+"
    r"(?:[가-힣0-9]+(?:로|길)\s*\d+(?:-\d+)?|[가-힣0-9]+[동읍면리가]\s*\d+(?:-\d+)?)"
)
_ROAD_ONLY = re.compile(r"[가-힣]+(?:로|길)(?:\d+번길)?\s*\d+(?:-\d+)?(?:\s|$|,)")


def _looks_like_name(token: str) -> bool:
    for honorific in _HONORIFICS:
        if token.endswith(honorific) and len(token) > 3:
            token = token[: -len(honorific)]
    if token.endswith(_NON_PERSON_SUFFIXES):
        return False
    if len(token) == 3:
        return token[0] in _SURNAME_SET
    if len(token) == 4:
        return token[:2] in _COMPOUND_SET
    return False


def rules_detector(value: str) -> frozenset[str]:
    """Regex and dictionary candidates: surname + given name, region + road + number."""
    text = unicodedata.normalize("NFKC", value)
    kinds: set[str] = set()
    if any(_looks_like_name(token) for token in _TOKEN.findall(text)):
        kinds.add("name")
    if _ADDRESS.search(text) or _ROAD_ONLY.search(text + " "):
        kinds.add("address")
    return frozenset(kinds)


def load_spacy() -> Detector:
    """spaCy ``ko_core_news_sm``: PS entities are names, LC entities are addresses."""
    import spacy

    nlp = spacy.load("ko_core_news_sm", disable=["parser", "lemmatizer"])

    def detect(value: str) -> frozenset[str]:
        kinds: set[str] = set()
        for entity in nlp(value).ents:
            if entity.label_ == "PS":
                kinds.add("name")
            elif entity.label_ == "LC":
                kinds.add("address")
        return frozenset(kinds)

    return detect


def load_spacy_hybrid() -> Detector:
    """spaCy PS for names, the regex rule for addresses (LC alone fires on any place)."""
    spacy_detect = load_spacy()

    def detect(value: str) -> frozenset[str]:
        names = spacy_detect(value) & {"name"}
        return frozenset(names | (rules_detector(value) & {"address"}))

    return detect


def load_kiwi() -> Detector:
    """Kiwi morphology: an eojeol that starts with a proper noun and reads as a name."""
    from kiwipiepy import Kiwi

    kiwi = Kiwi()

    def detect(value: str) -> frozenset[str]:
        for eojeol in value.split():
            tokens = kiwi.tokenize(eojeol)
            if not tokens or tokens[0].tag != "NNP":
                continue
            core = "".join(t.form for t in tokens if not t.tag.startswith(("J", "S", "NNB")))
            if _looks_like_name(core):
                return frozenset({"name"})
        return frozenset()

    return detect


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass
class Score:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class Result:
    name: str
    scores: dict[str, Score] = field(default_factory=dict)
    fp_by_category: dict[str, int] = field(default_factory=dict)
    fn_by_category: dict[str, int] = field(default_factory=dict)
    ms_per_10k: float | None = None
    load_s: float | None = None
    rss_delta_mb: float | None = None


def score(name: str, corpus: Sequence[Cell], predictions: Sequence[frozenset[str]]) -> Result:
    result = Result(name, {kind: Score() for kind in KINDS})
    for cell, predicted in zip(corpus, predictions, strict=True):
        for kind in KINDS:
            s = result.scores[kind]
            if kind in predicted and kind in cell.labels:
                s.tp += 1
            elif kind in predicted:
                s.fp += 1
                result.fp_by_category[cell.category] = (
                    result.fp_by_category.get(cell.category, 0) + 1
                )
            elif kind in cell.labels:
                s.fn += 1
                result.fn_by_category[cell.category] = (
                    result.fn_by_category.get(cell.category, 0) + 1
                )
    return result


def _rss_mb() -> float:
    # ru_maxrss is KiB on Linux.
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _timed(detector: Detector, values: Iterable[str]) -> tuple[list[frozenset[str]], float]:
    start = time.perf_counter()
    predictions = [detector(value) for value in values]
    return predictions, time.perf_counter() - start


def install_size_mb(distribution: str) -> float | None:
    """Installed size of a distribution and everything it requires, each counted once."""
    seen: set[str] = set()
    total = 0
    pending = [distribution]
    while pending:
        name = re.split(r"[\s;\[<>=!~(]", pending.pop(), maxsplit=1)[0].lower().replace("_", "-")
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            continue
        for path in dist.files or ():
            located = Path(str(dist.locate_file(path)))
            if located.is_file():
                total += located.stat().st_size
        for requirement in dist.requires or ():
            if "extra ==" not in requirement:
                pending.append(requirement)
    return round(total / 1_000_000, 1) if seen else None


def licence(distribution: str) -> str | None:
    try:
        meta = importlib.metadata.metadata(distribution)
    except importlib.metadata.PackageNotFoundError:
        return None
    return meta.get("License-Expression") or meta.get("License")


def evaluate(corpus: Sequence[Cell]) -> list[Result]:
    values = [cell.value for cell in corpus]
    per_10k = 10_000 / len(corpus) * 1000
    results: list[Result] = []

    baseline_value_preds, elapsed = _timed(baseline_values, values)
    base_value = score("baseline (value patterns)", corpus, baseline_value_preds)
    base_value.ms_per_10k = elapsed * per_10k
    results.append(base_value)

    baseline_preds = [baseline_scan(cell) for cell in corpus]
    results.append(score("baseline (value + column name)", corpus, baseline_preds))

    candidates: list[tuple[str, Callable[[], Detector]]] = [
        ("rules (regex + dictionary)", lambda: rules_detector),
        ("kiwi NNP + surname", load_kiwi),
        ("spaCy ko_core_news_sm (PS/LC)", load_spacy),
        ("spaCy PS + rules address", load_spacy_hybrid),
    ]
    for name, loader in candidates:
        rss_before, start = _rss_mb(), time.perf_counter()
        try:
            detector = loader()
        except (ImportError, OSError):
            print(f"skipped: {name} (not installed)", file=sys.stderr)
            continue
        load_s = time.perf_counter() - start
        rss_delta = _rss_mb() - rss_before
        predictions, elapsed = _timed(detector, values)
        alone = score(name, corpus, predictions)
        alone.ms_per_10k, alone.load_s, alone.rss_delta_mb = elapsed * per_10k, load_s, rss_delta
        results.append(alone)
        combined = [combine(b, c) for b, c in zip(baseline_preds, predictions, strict=True)]
        assert all(b <= u for b, u in zip(baseline_preds, combined, strict=True))
        results.append(score(f"baseline | {name}", corpus, combined))
    return results


def _fmt(number: float | None) -> str:
    return "-" if number is None else f"{number:.3f}"


def render(results: Sequence[Result], corpus: Sequence[Cell]) -> str:
    positives = {kind: sum(kind in c.labels for c in corpus) for kind in KINDS}
    lines = [
        f"cells: {len(corpus)}; positives: "
        + ", ".join(f"{k}={v}" for k, v in positives.items())
        + f"; negatives: {sum(not c.labels for c in corpus)}",
        "",
        "| detector | "
        + " | ".join(f"{k} P | {k} R" for k in KINDS)
        + " | ms / 10k cells | load s | RSS +MB |",
        "|---|" + "---|---|" * len(KINDS) + "---|---|---|",
    ]
    for r in results:
        cells = " | ".join(
            f"{_fmt(r.scores[k].precision)} | {_fmt(r.scores[k].recall)}" for k in KINDS
        )
        timing = "-" if r.ms_per_10k is None else f"{r.ms_per_10k:.0f}"
        load = "-" if r.load_s is None else f"{r.load_s:.2f}"
        rss = "-" if r.rss_delta_mb is None else f"{r.rss_delta_mb:.0f}"
        lines.append(f"| {r.name} | {cells} | {timing} | {load} | {rss} |")
    lines += ["", "False positives / negatives by corpus category:"]
    for r in results:
        lines.append(f"- {r.name}: FP {dict(sorted(r.fp_by_category.items()))}; "
                     f"FN {dict(sorted(r.fn_by_category.items()))}")  # fmt: skip
    lines += ["", "| package | licence | installed MB (with dependencies) |", "|---|---|---|"]
    for dist in ("spacy", "ko_core_news_sm", "kiwipiepy", "kiwipiepy_model"):
        size = install_size_mb(dist)
        if size is not None:
            lines.append(f"| {dist} | {licence(dist)} | {size} |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seed", type=int, default=1266)
    parser.add_argument("--cells", type=int, default=10_000)
    parser.add_argument("--json", type=Path, help="write the raw results here")
    parser.add_argument("--show-errors", action="store_true",
                        help="print misclassified synthetic values")  # fmt: skip
    args = parser.parse_args(argv)

    corpus = build_corpus(args.seed, args.cells)
    results = evaluate(corpus)
    print(render(results, corpus))
    if args.show_errors:
        for cell in corpus:
            found = rules_detector(cell.value) & {"name", "address"}
            if found != (cell.labels & {"name", "address"}):
                print(f"rules {cell.category}: {cell.value!r} -> {sorted(found)}")
    if args.json:
        payload = [
            {
                "detector": r.name,
                "scores": {k: vars(s) for k, s in r.scores.items()},
                "fp_by_category": r.fp_by_category,
                "fn_by_category": r.fn_by_category,
                "ms_per_10k": r.ms_per_10k,
                "load_s": r.load_s,
                "rss_delta_mb": r.rss_delta_mb,
            }
            for r in results
        ]
        args.json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

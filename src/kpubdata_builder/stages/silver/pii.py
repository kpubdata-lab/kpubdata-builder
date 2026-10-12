"""PII (Personally Identifiable Information) scanner (#441, QG-1).

The table is on DuckDB (#869), but the patterns are not handed to DuckDB's regex engine:
RE2 reads ``\\d``, ``\\w`` and ``\\b`` as ASCII and has no lookaround to emulate a
Unicode word boundary, so a Korean sentence around an email or a full-width digit would
match differently. DuckDB groups each text column's distinct values with their counts,
and the same Unicode-aware patterns run over those in Python — one match per distinct
value, however often it repeats.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from ...tabular.duckdb_load import TableHandle

# Korean PII regex — requires digit/separator patterns to mitigate over-detection.
# Resident ID: YYMMDD-SXXXXXX (S is 1-4, gender/nationality)
_RRN_DASHED = re.compile(r"\b\d{6}-[1-4]\d{6}\b")
# The same thirteen digits with nothing between them (#1296). Thirteen digits alone are
# also a barcode, an account number, a timestamp in milliseconds — so this form is a
# finding only when it could be a resident number: see ``_could_be_undashed_rrn``.
_RRN_UNDASHED = re.compile(r"\b(\d{2})(\d{2})(\d{2})([1-4])\d{5}(\d)\b")
# Mobile: 01X XXXX XXXX, the blocks joined, hyphenated or spaced (#1296).
_PHONE = re.compile(r"\b01[016789][- ]?\d{3,4}[- ]?\d{4}\b")
# Email: local@domain.tld
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Business ID: 000-00-00000
_BRN = re.compile(r"\b\d{3}-\d{2}-\d{5}\b")

#: The weights of a resident number's first twelve digits in its check digit.
_RRN_WEIGHTS = (2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5)
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _could_be_undashed_rrn(match: re.Match[str]) -> bool:
    """Whether thirteen joined digits are a date of birth and a number issued for it.

    The first six must be a month and a day that exist. A number for a birth in the
    1900s (seventh digit 1 or 2) was issued while the last digit was a check digit, so
    it must check. One for a birth in the 2000s (3 or 4) may have been issued since
    October 2020, when the digits after the seventh became random: nothing can be
    asked of its last digit.
    """
    month, day, era = int(match[2]), int(match[3]), match[4]
    if not 1 <= month <= 12 or not 1 <= day <= _DAYS_IN_MONTH[month - 1]:
        return False
    if era in "34":
        return True
    digits = [int(char) for char in match[0]]
    expected = (11 - sum(d * w for d, w in zip(digits, _RRN_WEIGHTS, strict=False)) % 11) % 10
    return digits[12] == expected


class ValuePattern(Protocol):
    """What a value is asked: whether it holds this kind of PII. Truthy when it does."""

    def search(self, value: str) -> object | None: ...


class _ResidentNumber:
    """A resident number, hyphenated as before or joined (#1296).

    The hyphenated form is matched exactly as it always was — any six digits, a hyphen,
    a seventh digit of 1 to 4 — so nothing it flagged stops being flagged. The joined
    form is added, and is held to more: see ``_could_be_undashed_rrn``.
    """

    def search(self, value: str) -> object | None:
        dashed = _RRN_DASHED.search(value)
        if dashed is not None:
            return dashed
        return next((m for m in _RRN_UNDASHED.finditer(value) if _could_be_undashed_rrn(m)), None)


_PATTERNS: dict[str, ValuePattern] = {
    "rrn": _ResidentNumber(),
    "phone": _PHONE,
    "email": _EMAIL,
    "business_no": _BRN,
}

# suspected column name heuristics (case-insensitive partial match). includes public
# data abbreviations.
_SUSPECT_PARTS: dict[str, tuple[str, ...]] = {
    "name": ("NM", "NAME", "성명", "이름"),
    "phone": ("TEL", "TELNO", "PHONE", "HP", "MOBILE", "연락처"),
    "addr": ("ADDR", "ADRES", "주소"),
    "email": ("EMAIL", "이메일"),
    "rrn": ("RRN", "JUMIN", "주민", "SSN"),
}


@dataclass(frozen=True)
class PiiFinding:
    """single PII detection result. never includes original values (#441)."""

    column: str | None
    kind: str
    count: int


def scan_pii_values(table: TableHandle) -> list[PiiFinding]:
    """Findings from value patterns only: text cells that look like PII (#441, #819).

    Evidence in the values themselves, without the column-name heuristic, which flags
    any column whose name merely contains e.g. ``NM``.
    """
    findings: list[PiiFinding] = []
    loaded = table.table
    for column_name, dtype in zip(loaded.names, loaded.dtypes, strict=True):
        if dtype != "String":
            continue
        counts = dict.fromkeys(_PATTERNS, 0)
        for value, count in table.distinct_text_values(column_name):
            for kind, pattern in _PATTERNS.items():
                if pattern.search(value):
                    counts[kind] += count
        for kind, count in counts.items():
            if count > 0:
                findings.append(PiiFinding(column=column_name, kind=kind, count=count))
    return findings


#: Value patterns by kind, for callers that count matches without loading the table.
VALUE_PATTERNS: dict[str, ValuePattern] = dict(_PATTERNS)


def suspect_column_kind(column_name: str) -> str | None:
    """The PII kind a column's name suggests, or None — the heuristic ``scan_pii`` uses."""
    upper = column_name.upper()
    for kind, parts in _SUSPECT_PARTS.items():
        if any(part in upper for part in parts):
            return kind
    return None


def scan_pii(table: TableHandle) -> list[PiiFinding]:
    """scans refined table with PII patterns + column name heuristics (#441)."""
    by_column: dict[str, list[PiiFinding]] = {}
    for finding in scan_pii_values(table):
        by_column.setdefault(str(finding.column), []).append(finding)
    findings: list[PiiFinding] = []
    for column_name in table.columns:
        # pattern matches first, in pattern order, as before.
        findings.extend(by_column.get(column_name, []))
        # column name heuristic — only first matching type per column.
        upper = column_name.upper()
        for kind, parts in _SUSPECT_PARTS.items():
            if any(part in upper for part in parts):
                findings.append(PiiFinding(column=column_name, kind=kind, count=1))
                break
    return findings


__all__ = [
    "VALUE_PATTERNS",
    "PiiFinding",
    "scan_pii",
    "scan_pii_values",
    "suspect_column_kind",
]

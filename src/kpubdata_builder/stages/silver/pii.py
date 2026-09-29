"""PII (Personally Identifiable Information) scanner (#441, QG-1)."""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl

# Korean PII regex — requires digit/separator patterns to mitigate over-detection.
# Resident ID: YYMMDD-SXXXXXX (S is 1-4, gender/nationality)
_RRN = re.compile(r"\b\d{6}-[1-4]\d{6}\b")
# Mobile: 01X(-?)XXXX(-?)XXXX
_PHONE = re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b")
# Email: local@domain.tld
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Business ID: 000-00-00000
_BRN = re.compile(r"\b\d{3}-\d{2}-\d{5}\b")

_PATTERNS: dict[str, re.Pattern[str]] = {
    "rrn": _RRN,
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


def scan_pii_values(table: pl.DataFrame) -> list[PiiFinding]:
    """Findings from value patterns only: text cells that look like PII (#441, #819).

    Evidence in the values themselves, without the column-name heuristic, which flags
    any column whose name merely contains e.g. ``NM``.
    """
    findings: list[PiiFinding] = []
    for column_name in table.columns:
        series = table.get_column(column_name)
        if series.dtype != pl.Utf8:
            continue
        non_null = series.drop_nulls()
        if non_null.len() == 0:
            continue
        for kind, pattern in _PATTERNS.items():
            count = int(non_null.str.contains(pattern.pattern).sum())
            if count > 0:
                findings.append(PiiFinding(column=column_name, kind=kind, count=count))
    return findings


def scan_pii(table: pl.DataFrame) -> list[PiiFinding]:
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


__all__ = ["PiiFinding", "scan_pii", "scan_pii_values"]

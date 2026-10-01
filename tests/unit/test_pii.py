"""PII scanner unit tests (#441, QG-1).

Verify patterns (resident ID/mobile/email/business ID), column name heuristics, and security
principle (no raw value exposure).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import polars as pl
import pytest

from kpubdata_builder.query.export import _PiiScan
from kpubdata_builder.stages.silver.pii import PiiFinding, scan_pii_values
from kpubdata_builder.stages.silver.pii import scan_pii as _scan_table
from kpubdata_builder.tabular.polars_bridge import handle_from_frame


def scan_pii(table: pl.DataFrame) -> list[PiiFinding]:
    """Silver's scan of ``table`` loaded into DuckDB (#869)."""
    return _scan_table(handle_from_frame(table, workdir=Path(tempfile.mkdtemp())))


class TestScanPiiPatterns:
    """Pattern-based PII detection."""

    def test_detects_rrn(self) -> None:
        table = pl.DataFrame({"text": ["900101-1234567", "no pii here"]})
        findings = scan_pii(table)
        kinds = [f.kind for f in findings]
        assert "rrn" in kinds
        rrn = next(f for f in findings if f.kind == "rrn")
        assert rrn.count == 1
        assert rrn.column == "text"

    def test_detects_phone(self) -> None:
        table = pl.DataFrame({"contact": ["010-1234-5678", "x"]})
        findings = scan_pii(table)
        assert "phone" in [f.kind for f in findings]

    def test_detects_email(self) -> None:
        table = pl.DataFrame({"x": ["user@example.com", "y"]})
        findings = scan_pii(table)
        assert "email" in [f.kind for f in findings]

    def test_detects_business_no(self) -> None:
        table = pl.DataFrame({"biz": ["123-45-67890", "z"]})
        findings = scan_pii(table)
        assert "business_no" in [f.kind for f in findings]

    def test_no_pii_returns_empty(self) -> None:
        table = pl.DataFrame({"a": ["normal text", "more text"]})
        assert scan_pii(table) == []

    def test_non_string_columns_skip_pattern_scan(self) -> None:
        """Non-string columns skip pattern scan (column name heuristic only)."""
        table = pl.DataFrame({"value": [1, 2, 3]})
        assert scan_pii(table) == []


class TestScanPiiColumnNameHeuristics:
    """Column name heuristics — public data abbreviations (NM/TELNO/ADRES, etc.)."""

    def test_name_column_flagged(self) -> None:
        table = pl.DataFrame({"OPNR_NM": ["홍길동", "김철수"]})
        assert "name" in [f.kind for f in scan_pii(table)]

    def test_addr_column_flagged(self) -> None:
        table = pl.DataFrame({"ROAD_ADRES": ["서울", "부산"]})
        assert "addr" in [f.kind for f in scan_pii(table)]

    def test_phone_column_flagged(self) -> None:
        table = pl.DataFrame({"TELNO": ["02-123-4567", "x"]})
        assert "phone" in [f.kind for f in scan_pii(table)]


class TestScanPiiSecurityPrinciple:
    """Detection results must not leak original values (#441 security principle)."""

    def test_no_original_values_in_findings(self) -> None:
        secret = "900101-1234567"
        table = pl.DataFrame({"x": [secret, "clean"]})
        findings = scan_pii(table)
        # Serialization of all findings fields (column/kind/count) must not include original values.
        serialized = " ".join(f"{f.column}|{f.kind}|{f.count}" for f in findings)
        assert secret not in serialized


_TRICKY = [
    "900101-1234567",
    "주민번호900101-1234567입니다",
    "x900101-1234567",
    "010-1234-5678",
    "연락처:01012345678",
    "０１０-1234-5678",
    "홍길동hong@example.com",
    "메일 hong@example.co.kr 로",
    "a@b.c",
    "user.name+tag@sub-domain.example.org.",
    "123-45-67890",
    "사업자123-45-67890번",
    "١٢٣٤٥٦-1234567",
    "",
    None,
]


@pytest.mark.parametrize("value", [v for v in _TRICKY if v is not None])
def test_the_silver_and_export_scans_agree(value: str) -> None:
    """#869, #874: Silver scans DuckDB's distinct values and an export scans its rows as
    they are written; both run the same Python patterns, Unicode digits and word
    boundaries included, and count the same rows."""
    values = [value, value, None, "plain"]
    frame = pl.DataFrame({"v": values})

    silver = scan_pii_values(handle_from_frame(frame, workdir=Path(tempfile.mkdtemp())))
    export = _PiiScan([0], ["v"])
    list(export.rows(((v,), {}) for v in values))

    assert silver == export.findings()


def test_counts_are_per_row_not_per_distinct_value() -> None:
    frame = pl.DataFrame({"v": ["010-1234-5678"] * 3 + ["x"]})

    (finding,) = scan_pii_values(handle_from_frame(frame, workdir=Path(tempfile.mkdtemp())))

    assert (finding.kind, finding.count) == ("phone", 3)

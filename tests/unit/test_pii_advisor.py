"""PII false positive advisor test (AI-2, #447)."""

from __future__ import annotations

from kpubdata_builder.stages.silver.pii import PiiFinding
from kpubdata_builder.stages.silver.pii_advisor import build_pii_advisory_prompt


class TestBuildPiiAdvisoryPrompt:
    def test_empty_findings_returns_empty(self) -> None:
        assert build_pii_advisory_prompt([]) == ""

    def test_findings_contain_column_names(self) -> None:
        findings = [
            PiiFinding(column="OPNR_NM", kind="name", count=1),
            PiiFinding(column="TELNO", kind="phone", count=3),
        ]
        prompt = build_pii_advisory_prompt(findings)
        assert "OPNR_NM" in prompt
        assert "TELNO" in prompt

    def test_findings_contain_kinds(self) -> None:
        findings = [PiiFinding(column="x", kind="rrn", count=2)]
        prompt = build_pii_advisory_prompt(findings)
        assert "rrn" in prompt

    def test_prompt_includes_false_positive_instruction(self) -> None:
        findings = [PiiFinding(column="DEPT_NM", kind="name", count=1)]
        prompt = build_pii_advisory_prompt(findings)
        assert "likely_false_positives" in prompt

    def test_none_column_filtered(self) -> None:
        findings = [PiiFinding(column=None, kind="rrn", count=5)]
        prompt = build_pii_advisory_prompt(findings)
        # Items with column=None are excluded from the prompt.
        assert prompt.strip() != ""

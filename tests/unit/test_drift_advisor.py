"""Drift interpretation test (AI-3, #448)."""

from __future__ import annotations

from kpubdata_builder.stages.silver.drift import DriftFinding
from kpubdata_builder.stages.silver.drift_advisor import build_drift_advisory_prompt


class TestBuildDriftAdvisoryPrompt:
    def test_empty_findings_returns_empty(self) -> None:
        assert build_drift_advisory_prompt([]) == ""

    def test_findings_included_in_prompt(self) -> None:
        findings = [
            DriftFinding(kind="column_added", column="new_col", detail="new column"),
            DriftFinding(kind="dtype_changed", column="amount", detail="Int64 → Float64"),
        ]
        prompt = build_drift_advisory_prompt(findings)
        assert "column_added" in prompt
        assert "new_col" in prompt
        assert "dtype_changed" in prompt

    def test_prompt_contains_cause_categories(self) -> None:
        findings = [DriftFinding(kind="row_count_jump", column=None, detail="100 → 500")]
        prompt = build_drift_advisory_prompt(findings)
        assert "upstream_schema_change" in prompt
        assert "data_growth" in prompt

    def test_prompt_contains_advisory_disclaimer(self) -> None:
        findings = [DriftFinding(kind="column_removed", column="old_col", detail="gone")]
        prompt = build_drift_advisory_prompt(findings)
        assert "참고용" in prompt

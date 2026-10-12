"""Regression tests for the stabilization scope gate.

Run: python3 scripts/test_stabilization_scope.py -v
"""

from __future__ import annotations

import unittest

from check_stabilization_scope import check_scope, scope_indicators

SDK = "kpubdata-lab/kpubdata"
BUILDER = "kpubdata-lab/kpubdata-builder"
STUDIO = "kpubdata-lab/kpubdata-studio"
BODY = (
    "Scope exception: YES\n"
    "Existing flow or risk: Preserve an existing user flow whose safety is broken.\n"
    "Refs #918\n"
)


class StabilizationGateTests(unittest.TestCase):
    def test_bugfix_unit_tests_and_docs_pass(self) -> None:
        for repo in (SDK, BUILDER, STUDIO):
            self.assertEqual(
                check_scope(repo, "fix: correct failure", "", set(), "M\tsrc/a.py", ""),
                (),
            )

    def test_feature_title_fails_without_exception(self) -> None:
        self.assertTrue(check_scope(SDK, "feat(api): add feature", "", set(), "", ""))

    def test_exception_requires_review_label(self) -> None:
        errors = check_scope(BUILDER, "feat(backup): fix recovery", BODY, set(), "", "")
        self.assertTrue(any("review:R3" in error for error in errors))

    def test_exception_requires_justification_and_issue(self) -> None:
        errors = check_scope(
            STUDIO, "feat(ui): new page", "Scope exception: YES", {"review:R3"}, "", ""
        )
        self.assertTrue(any("Existing flow or risk:" in error for error in errors))
        self.assertTrue(any("Refs #N" in error for error in errors))

    def test_reviewed_necessary_exception_passes(self) -> None:
        self.assertEqual(
            check_scope(BUILDER, "feat(ops): restore existing runs", BODY, {"review:R3"}, "", ""),
            (),
        )

    def test_new_dataset_spec_cannot_hide_in_fix(self) -> None:
        self.assertTrue(
            check_scope(
                SDK, "fix: adjust docs", "", set(), "A\tsrc/kpubdata/specs/datago/new.yaml", ""
            )
        )

    def test_new_provider_source_is_flagged(self) -> None:
        self.assertIn(
            "new provider source:",
            " ".join(
                scope_indicators(
                    SDK, "fix: provider", "A\tsrc/kpubdata/providers/new/adapter.py", ""
                )
            ),
        )

    def test_new_backend_route_is_flagged(self) -> None:
        patch = (
            "diff --git a/contract/builder-api.yaml b/contract/builder-api.yaml\n"
            "+++ b/contract/builder-api.yaml\n+  /new-route:\n"
        )
        self.assertTrue(
            check_scope(BUILDER, "fix: api", "", set(), "M\tcontract/builder-api.yaml", patch)
        )

    def test_new_browser_route_is_flagged(self) -> None:
        patch = "+++ b/src/app/router.tsx\n+    path: 'new-page',\n"
        self.assertTrue(
            check_scope(STUDIO, "fix: router", "", set(), "M\tsrc/app/router.tsx", patch)
        )

    def test_new_workflow_or_schedule_is_flagged(self) -> None:
        self.assertTrue(
            check_scope(SDK, "ci: monitor", "", set(), "A\t.github/workflows/new.yml", "")
        )
        patch = "+++ b/.github/workflows/old.yml\n+    - cron: '0 2 * * *'\n"
        self.assertTrue(
            check_scope(SDK, "ci: monitor", "", set(), "M\t.github/workflows/old.yml", patch)
        )

    def test_new_sdk_export_needs_exception(self) -> None:
        patch = '+++ b/src/kpubdata/__init__.py\n+    "NewExport",\n'
        self.assertTrue(
            check_scope(SDK, "fix: exports", "", set(), "M\tsrc/kpubdata/__init__.py", patch)
        )

    def test_normal_workflow_fix_still_passes(self) -> None:
        patch = "+++ b/.github/workflows/ci.yml\n+      - name: Validate changed inputs\n"
        self.assertEqual(
            check_scope(
                BUILDER, "fix(ci): pin a check", "", set(), "M\t.github/workflows/ci.yml", patch
            ),
            (),
        )

    def test_new_issue_writer_in_an_existing_workflow_requires_review(self) -> None:
        patch = '+++ b/.github/workflows/report.yml\n+        run: gh issue create --title "new"\n'
        self.assertTrue(check_scope(BUILDER, "fix(ci): alert", "", set(), "", patch))

    def test_new_python_issue_creator_requires_review(self) -> None:
        patch = '+++ b/scripts/alerts.py\n+    _gh(run, "issue", "create", title)\n'
        self.assertTrue(check_scope(SDK, "fix: alerts", "", set(), "", patch))

    def test_literal_in_a_test_fixture_does_not_trigger_issue_gate(self) -> None:
        patch = "+++ b/scripts/test_alerts.py\n+    command = 'gh issue create'\n"
        self.assertEqual(
            check_scope(SDK, "test: alerts", "", set(), "", patch),
            (),
        )

    def test_new_studio_page_requires_exception(self) -> None:
        self.assertTrue(
            check_scope(STUDIO, "fix: screen", "", set(), "A\tsrc/pages/NewPage.tsx", "")
        )


if __name__ == "__main__":
    unittest.main()

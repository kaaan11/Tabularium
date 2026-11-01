"""Package-level regressions for context loader text and fence defense rules."""

from __future__ import annotations

import json
from pathlib import Path

from tabularium.engine.context_loader.model import (
    ACTIONABLE, BLOCKED, DIAGNOSTIC_ONLY, FRAGMENT, FULL_BODY,
    METADATA_ONLY, NOT_ACTIONABLE, NOT_APPLICABLE, READY,
)
from tests.test_context_loader import Fixture, LoaderTestCase


class Round4Case(LoaderTestCase):
    def duplicate_claim(self, original: Path, mismatched_id: str) -> Path:
        """Add a second physical file that declares the original logical id."""
        duplicate = original.with_name(f"{mismatched_id}.md")
        duplicate.write_bytes(original.read_bytes())
        return duplicate

    def raw_record(self, directory: str, filename: str, data: bytes) -> Path:
        target = self.fx.project / ".ai" / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target


class TestAmbiguousIdentityQuarantine(Round4Case):
    def test_ambiguous_question_backlink_does_not_change_actionability(self):
        original = self.fx.question(
            "QST-2026-0200", task="TASK-2026-0001", impact="BLOCKING")
        self.duplicate_claim(original, "QST-2026-9200")
        self.fx.task()

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.not_selected(pkg, "QST-2026-0200")
        self.diag(pkg, "RESOLUTION_AMBIGUOUS")

    def test_ambiguous_approved_decision_is_not_project_candidate(self):
        original = self.fx.decision("DEC-2026-0200")
        self.duplicate_claim(original, "DEC-2026-9200")
        self.fx.task()

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.not_selected(pkg, "DEC-2026-0200")
        self.assertFalse(any(
            item.record_id == "DEC-2026-0200"
            and "PROJECT_APPROVED_UNSCOPED_CANDIDATE" in item.reasons
            for item in pkg.selected))

    def test_ambiguous_generic_evidence_reference_has_no_winner(self):
        original = self.fx.evidence("EVD-2026-0200")
        self.duplicate_claim(original, "EVD-2026-9200")
        self.fx.task(evidence=["EVD-2026-0200"])

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.not_selected(pkg, "EVD-2026-0200")
        diag = self.diag(pkg, "RESOLUTION_AMBIGUOUS")
        self.assertEqual(diag.blocking_dimension, "NEITHER")

    def test_ambiguous_investigation_backlink_is_quarantined(self):
        original = self.fx.investigation(
            "INV-2026-0200", task="TASK-2026-0001")
        self.duplicate_claim(original, "INV-2026-9200")
        self.fx.task()

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.not_selected(pkg, "INV-2026-0200")

    def test_ambiguous_handoff_and_report_are_not_candidates(self):
        handoff = self.fx.handoff("HND-2026-0200")
        report = self.fx.report("RPT-2026-0200")
        self.duplicate_claim(handoff, "HND-2026-9200")
        self.duplicate_claim(report, "RPT-2026-9200")
        self.fx.task()

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.not_selected(pkg, "HND-2026-0200")
        self.not_selected(pkg, "RPT-2026-0200")
        self.assertNotIn("HND-2026-0200", pkg.navigation.get("Handoff", []))
        self.assertNotIn("RPT-2026-0200", pkg.navigation.get("Report", []))

    def test_ambiguous_root_task_blocks_without_selecting_a_claimant(self):
        original = self.fx.task()
        self.duplicate_claim(original, "TASK-2026-9001")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        self.assertIsNone(pkg.root_task)
        self.not_selected(pkg, "TASK-2026-0001")

    def test_ambiguous_explicit_selection_blocks_without_winner(self):
        original = self.fx.evidence("EVD-2026-0201")
        self.duplicate_claim(original, "EVD-2026-9201")

        pkg = self.fx.load(select=["EVD-2026-0201"])

        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "EVD-2026-0201")

    def test_ambiguous_decision_cannot_be_supersession_endpoint(self):
        self.fx.decision(
            "DEC-2026-0201", status="SUPERSEDED",
            superseded_by=["DEC-2026-0202"])
        successor = self.fx.decision(
            "DEC-2026-0202", supersedes=["DEC-2026-0201"])
        self.duplicate_claim(successor, "DEC-2026-9202")

        pkg = self.fx.load(select=["DEC-2026-0201"])

        self.not_selected(pkg, "DEC-2026-0202")
        self.diag(pkg, "RESOLUTION_AMBIGUOUS")
        self.diag(pkg, "MISSING_REFERENCE")

    def test_project_orientation_does_not_select_ambiguous_task(self):
        original = self.fx.task("TASK-2026-0200")
        self.duplicate_claim(original, "TASK-2026-9200")

        pkg = self.fx.load()

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
        self.not_selected(pkg, "TASK-2026-0200")


class TestMalformedTextQuarantine(Round4Case):
    def assert_unrelated_record_is_quarantined(self, raw: bytes) -> None:
        self.fx.task()
        self.raw_record("questions", "QST-2026-0300.md", raw)

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.diag(pkg, "MALFORMED_RECORD_QUARANTINED")

    def test_sequence_mapping_key_is_quarantined_without_crash(self):
        self.assert_unrelated_record_is_quarantined(
            b"---\nschema_version: '1.0'\nid: QST-2026-0300\n"
            b"? [a, b]\n: value\n---\n# Question\n")

    def test_mapping_and_non_string_scalar_keys_are_quarantined(self):
        samples = (
            b"? {a: b}\n: value\n",
            b"null: value\n",
            b"true: value\n",
            b"7: value\n",
        )
        for extra in samples:
            with self.subTest(extra=extra):
                fx = Fixture()
                try:
                    fx.task()
                    target = fx.project / ".ai/questions/QST-2026-0300.md"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(
                        b"---\nschema_version: '1.0'\n"
                        b"id: QST-2026-0300\n" + extra + b"---\n")
                    pkg = fx.load(task="TASK-2026-0001")
                    self.assertEqual(pkg.package_status, READY)
                    self.assertIn("MALFORMED_RECORD_QUARANTINED",
                                  [d.code for d in pkg.diagnostics])
                finally:
                    fx.cleanup()

    def test_malformed_yaml_record_is_quarantined_without_crash(self):
        self.assert_unrelated_record_is_quarantined(
            b"---\nschema_version: '1.0'\nid: QST-2026-0300\n"
            b"broken: [\n---\n# Question\n")

    def test_invalid_utf8_unrelated_record_is_quarantined(self):
        self.assert_unrelated_record_is_quarantined(b"---\n\xff\n---\n")

    def test_invalid_utf8_root_task_blocks_deterministically(self):
        self.raw_record("tasks", "TASK-2026-0001.md", b"---\n\xff\n---\n")

        first = self.fx.load(task="TASK-2026-0001")
        second = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(first.package_status, BLOCKED)
        self.assertEqual(first.task_actionability, NOT_ACTIONABLE)
        self.diag(first, "MALFORMED_RECORD_QUARANTINED")
        self.assertEqual(json.dumps(first.to_dict(), sort_keys=True),
                         json.dumps(second.to_dict(), sort_keys=True))

    def test_invalid_utf8_manifest_returns_diagnostic_only(self):
        (self.fx.project / ".ai/manifest.yaml").write_bytes(b"\xff")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
        self.assertEqual(pkg.selected, [])
        self.diag(pkg, "MANIFEST_ENTRY_FAILED")

    def test_invalid_utf8_required_foundation_blocks(self):
        for name in ("project.md", "constraints.md"):
            with self.subTest(name=name):
                fx = Fixture()
                try:
                    fx.task()
                    (fx.project / ".ai" / name).write_bytes(b"\xff")
                    pkg = fx.load(task="TASK-2026-0001")
                    self.assertEqual(pkg.package_status, BLOCKED)
                    self.assertIn("REQUIRED_FOUNDATION_UNAVAILABLE",
                                  [d.code for d in pkg.diagnostics])
                finally:
                    fx.cleanup()

    def test_invalid_utf8_optional_foundations_keep_existing_semantics(self):
        self.fx.task()
        (self.fx.project / ".ai/current-state.md").write_bytes(b"\xff")
        (self.fx.project / ".ai/architecture.md").write_bytes(b"\xff")

        pkg = self.fx.load(task="TASK-2026-0001", include_architecture=True)

        self.assertEqual(pkg.package_status, READY)
        self.diag(pkg, "CURRENT_STATE_ABSENT")
        self.diag(pkg, "ARCHITECTURE_ABSENT")


class TestFenceScannerDefense(Round4Case):
    def fragment_package(self, body: str):
        self.fx.investigation("INV-2026-0400", body=body)
        return self.fx.load(select=["INV-2026-0400#H1"])

    def test_backtick_info_text_pseudo_close_remains_content(self):
        pkg = self.fragment_package(
            "# I\n\n```text\n```python\n### H1\nFake.\n```\n")
        self.assertEqual(pkg.package_status, BLOCKED)
        parent = self.item(pkg, "INV-2026-0400")
        self.assertEqual(parent.load_mode, METADATA_ONLY)
        self.assertIsNone(parent.body)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_tilde_info_text_pseudo_close_remains_content(self):
        pkg = self.fragment_package(
            "# I\n\n~~~text\n~~~python\n### H1\nFake.\n~~~\n")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_shorter_and_opposite_delimiters_do_not_close(self):
        bodies = (
            "# I\n\n````text\n```\n### H1\nFake.\n````\n",
            "# I\n\n~~~~text\n~~~\n### H1\nFake.\n~~~~\n",
            "# I\n\n```text\n~~~\n### H1\nFake.\n```\n",
            "# I\n\n~~~text\n```\n### H1\nFake.\n~~~\n",
        )
        for body in bodies:
            with self.subTest(body=body):
                fx = Fixture()
                try:
                    fx.investigation("INV-2026-0400", body=body)
                    pkg = fx.load(select=["INV-2026-0400#H1"])
                    self.assertEqual(pkg.package_status, BLOCKED)
                    self.assertIn("INVALID_FRAGMENT",
                                  [d.code for d in pkg.diagnostics])
                finally:
                    fx.cleanup()

    def test_longer_delimiter_validly_closes_fence(self):
        pkg = self.fragment_package(
            "# I\n\n```text\n### H1\nFake.\n````\n"
            "### H1\n\nReal.\n")

        self.assertEqual(pkg.package_status, READY)
        item = self.item(pkg, "INV-2026-0400", "H1")
        self.assertEqual(item.load_mode, FRAGMENT)
        self.assertIn("Real.", item.body)
        self.assertNotIn("Fake.", item.body)

    def test_unclosed_fence_keeps_remaining_heading_fenced(self):
        pkg = self.fragment_package("# I\n\n```text\n### H1\nFake.\n")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_real_heading_and_fenced_decoy_resolve_only_real_heading(self):
        pkg = self.fragment_package(
            "# I\n\n```text\n### H1\nFake.\n```\n"
            "### H1\n\nReal.\n")
        item = self.item(pkg, "INV-2026-0400", "H1")
        self.assertIn("Real.", item.body)
        self.assertNotIn("Fake.", item.body)

    def test_duplicate_real_hypothesis_headings_are_ambiguous(self):
        pkg = self.fragment_package(
            "# I\n\n### H1\nOne.\n\n### H1\nTwo.\n")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "AMBIGUOUS_FRAGMENT")

    def test_terminal_current_conclusion_inside_pseudo_close_is_unavailable(self):
        self.fx.task()
        self.fx.investigation(
            "INV-2026-0401", task="TASK-2026-0001", status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# I\n\n```text\n```python\n"
                 "## Current conclusion\n\nFake.\n```\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        item = self.item(pkg, "INV-2026-0401")
        self.assertEqual(item.load_mode, METADATA_ONLY)
        self.assertIsNone(item.body)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_terminal_current_conclusion_uses_real_not_fenced_decoy(self):
        self.fx.task()
        self.fx.investigation(
            "INV-2026-0402", task="TASK-2026-0001", status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# I\n\n~~~text\n## Current conclusion\nFake.\n~~~\n"
                 "## Current conclusion\n\nReal.\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        item = self.item(pkg, "INV-2026-0402")
        self.assertEqual(item.load_mode, FRAGMENT)
        self.assertIn("Real.", item.body)
        self.assertNotIn("Fake.", item.body)

    def test_terminal_current_conclusion_after_unclosed_fence_is_unavailable(self):
        self.fx.task()
        self.fx.investigation(
            "INV-2026-0403", task="TASK-2026-0001", status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# I\n\n```text\n## Current conclusion\n\nFake.\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        item = self.item(pkg, "INV-2026-0403")
        self.assertEqual(item.load_mode, METADATA_ONLY)
        self.assertIsNone(item.body)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_handoff_required_headings_inside_fence_are_invalid(self):
        self.fx.handoff(
            "HND-2026-0400",
            body="# H\n\n```text\n```python\n## Current State\nFake.\n"
                 "## Continuation\nFake.\n```\n")

        pkg = self.fx.load(select=["HND-2026-0400"])

        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "HND-2026-0400")
        self.diag(pkg, "MALFORMED_REFERENCE")

    def test_report_required_headings_inside_fence_are_invalid(self):
        self.fx.report(
            "RPT-2026-0400",
            body="# R\n\n~~~text\n~~~python\n## Outcome\nFake.\n"
                 "## Validation\nFake.\n## Limitations\nFake.\n~~~\n")

        pkg = self.fx.load(select=["RPT-2026-0400"])

        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "RPT-2026-0400")
        self.diag(pkg, "MALFORMED_REFERENCE")

    def test_longer_close_exposes_real_handoff_and_report_sections(self):
        self.fx.handoff(
            "HND-2026-0401",
            body="# H\n\n```text\n## Current State\nFake.\n````\n"
                 "## Current State\nReal.\n## Continuation\nContinue.\n")
        self.fx.report(
            "RPT-2026-0401",
            body="# R\n\n~~~text\n## Outcome\nFake.\n~~~~\n"
                 "## Outcome\nReal.\n## Validation\nChecked.\n"
                 "## Limitations\nNone.\n")

        pkg = self.fx.load(
            select=["HND-2026-0401", "RPT-2026-0401"])

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(self.item(pkg, "HND-2026-0401").load_mode, FULL_BODY)
        self.assertEqual(self.item(pkg, "RPT-2026-0401").load_mode, FULL_BODY)

    def test_unclosed_fences_do_not_supply_handoff_or_report_sections(self):
        self.fx.handoff(
            "HND-2026-0402",
            body="# H\n\n```text\n## Current State\nFake.\n"
                 "## Continuation\nFake.\n")
        self.fx.report(
            "RPT-2026-0402",
            body="# R\n\n~~~text\n## Outcome\nFake.\n"
                 "## Validation\nFake.\n## Limitations\nFake.\n")

        pkg = self.fx.load(
            select=["HND-2026-0402", "RPT-2026-0402"])

        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "HND-2026-0402")
        self.not_selected(pkg, "RPT-2026-0402")

    def test_duplicate_real_required_sections_preserve_presence_semantics(self):
        self.fx.handoff(
            "HND-2026-0403",
            body="# H\n\n## Current State\nOne.\n## Current State\nTwo.\n"
                 "## Continuation\nContinue.\n")
        self.fx.report(
            "RPT-2026-0403",
            body="# R\n\n## Outcome\nOne.\n## Outcome\nTwo.\n"
                 "## Validation\nChecked.\n## Limitations\nNone.\n")

        pkg = self.fx.load(
            select=["HND-2026-0403", "RPT-2026-0403"])

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(self.item(pkg, "HND-2026-0403").load_mode, FULL_BODY)
        self.assertEqual(self.item(pkg, "RPT-2026-0403").load_mode, FULL_BODY)


class TestActiveDocumentationRules(Round4Case):
    def test_readme_does_not_overclaim(self):
        # The README ships in every tree; it must describe reality without
        # overclaiming maturity or loader capability.
        root = Path(__file__).resolve().parents[1]
        readme = (root / "README.md").read_text(encoding="utf-8")

        self.assertNotIn("production-ready", readme.lower())
        self.assertNotIn("validator for any of them", readme)

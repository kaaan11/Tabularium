"""Package-level regressions for context loader quarantine and validation rules."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tabularium.engine.context_loader.model import (  # noqa: E402
    ACTIONABLE, BLOCKED, FRAGMENT, FULL_BODY, METADATA_ONLY, NEITHER,
    NOT_ACTIONABLE, READY)
from tests.test_context_loader import Fixture, LoaderTestCase  # noqa: E402


class TestIdentityQuarantine(LoaderTestCase):

    def mismatch(self, rid: str, filename: str, family_writer: str, **fields):
        writer = getattr(self.fx, family_writer)
        if family_writer == "task":
            base = {"schema_version": "1.10", "title": "Mismatch",
                    "objective": "Never usable", "status": "IN_PROGRESS",
                    "created_at": "2026-01-01T00:00:00Z",
                    "created_by": "USER"}
            base.update(fields)
            return self.fx.rec(rid, base, "# Task\n", filename=filename)
        return writer(rid, **fields).rename(
            self.fx.project / ".ai" /
            {"evidence": "evidence", "decision": "decisions",
             "question": "questions", "handoff": "handoffs",
             "report": "reports", "investigation": "investigations"}
            [family_writer] / f"{filename}.md")

    def test_generic_evidence_relationship_quarantines_mismatch(self):
        self.fx.task(evidence=["EVD-2026-0101"])
        self.mismatch("EVD-2026-0101", "EVD-2026-9101", "evidence")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "EVD-2026-0101")
        self.diag(pkg, "FILENAME_ID_MISMATCH")
        d = self.diag(pkg, "MALFORMED_REFERENCE")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(pkg.package_status, READY)

    def test_explicit_evidence_mismatch_blocks(self):
        self.fx.task()
        self.mismatch("EVD-2026-0102", "EVD-2026-9102", "evidence")
        pkg = self.fx.load(task="TASK-2026-0001",
                           select=["EVD-2026-0102"])
        self.not_selected(pkg, "EVD-2026-0102")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "MALFORMED_REFERENCE")

    def test_root_task_mismatch_blocks_without_task_semantics(self):
        self.mismatch("TASK-2026-0103", "TASK-2026-9103", "task")
        pkg = self.fx.load(task="TASK-2026-0103")
        self.not_selected(pkg, "TASK-2026-0103")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)

    def test_mismatched_approved_decision_is_not_project_metadata(self):
        self.mismatch("DEC-2026-0104", "DEC-2026-9104", "decision")
        pkg = self.fx.load()
        self.not_selected(pkg, "DEC-2026-0104")

    def test_mismatched_question_cannot_change_actionability(self):
        self.fx.task(questions=["QST-2026-0105"])
        self.mismatch("QST-2026-0105", "QST-2026-9105", "question",
                      status="OPEN", impact="BLOCKING",
                      task="TASK-2026-0001")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "QST-2026-0105")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_mismatched_handoff_and_report_are_not_candidates(self):
        self.fx.task(handoffs=["HND-2026-0106"], reports=["RPT-2026-0106"])
        self.mismatch("HND-2026-0106", "HND-2026-9106", "handoff")
        self.mismatch("RPT-2026-0106", "RPT-2026-9106", "report")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "HND-2026-0106")
        self.not_selected(pkg, "RPT-2026-0106")

    def test_mismatched_investigation_is_not_selected(self):
        self.fx.task(investigations=["INV-2026-0107"])
        self.mismatch("INV-2026-0107", "INV-2026-9107", "investigation")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "INV-2026-0107")

    def test_mismatch_duplicate_interaction_never_chooses_a_winner(self):
        self.fx.task(evidence=["EVD-2026-0108"])
        self.fx.evidence("EVD-2026-0108")
        self.fx.rec("EVD-2026-0108", {
            "schema_version": "1.4", "title": "Mismatch",
            "source_type": "OBSERVATION", "status": "ACTIVE",
            "observation": "Duplicate identity.",
            "collected_at": "2026-01-05T00:00:00Z",
            "collected_by": "ai:claude",
        }, "# Evidence\n", filename="EVD-2026-9108")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "EVD-2026-0108")
        self.diag(pkg, "RESOLUTION_AMBIGUOUS")


class TestConsumedValidation(LoaderTestCase):

    def test_task_approval_provenance_wrong_types_block_root(self):
        for field, value in (("approved_by", 42), ("approved_by", True),
                             ("approved_at", False), ("approved_at", None)):
            with self.subTest(field=field, value=value):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                data = {"approval_status": "APPROVED",
                        "approved_by": "human:user",
                        "approved_at": "2026-01-01T01:00:00Z"}
                data[field] = value
                fx.task(**data)
                pkg = fx.load(task="TASK-2026-0001")
                self.assertEqual(pkg.package_status, BLOCKED)
                self.not_selected(pkg, "TASK-2026-0001")

    def test_approval_required_without_status_remains_valid_not_actionable(self):
        self.fx.task(approval_required=True)
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)

    def test_decision_approval_provenance_wrong_types_is_quarantined(self):
        self.fx.task(decisions=["DEC-2026-0201"])
        self.fx.decision("DEC-2026-0201", approved_by=7, approved_at=True)
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "DEC-2026-0201")
        self.diag(pkg, "INVALID_RECORD_QUARANTINED")

    def test_non_answered_question_with_null_answer_key_cannot_block(self):
        self.fx.task(questions=["QST-2026-0202"])
        self.fx.question("QST-2026-0202", status="OPEN", impact="BLOCKING",
                         task="TASK-2026-0001", answer=None)
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "QST-2026-0202")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_evidence_present_invalid_optional_values_are_quarantined(self):
        self.fx.task(evidence=["EVD-2026-0203", "EVD-2026-0204"])
        self.fx.evidence("EVD-2026-0203", reliability=None)
        self.fx.evidence("EVD-2026-0204", source_locator=["not", "a", "string"])
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "EVD-2026-0203")
        self.not_selected(pkg, "EVD-2026-0204")

    def test_handoff_and_report_forbidden_status_are_quarantined(self):
        self.fx.task(handoffs=["HND-2026-0205"], reports=["RPT-2026-0205"])
        self.fx.handoff("HND-2026-0205", status="APPROVED")
        self.fx.report("RPT-2026-0205", status="FINAL")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "HND-2026-0205")
        self.not_selected(pkg, "RPT-2026-0205")


class TestMetadataDecisionBoundary(LoaderTestCase):

    def test_unscoped_approved_metadata_decision_does_not_expand(self):
        self.fx.task()
        self.fx.investigation("INV-2026-0301")
        self.fx.decision("DEC-2026-0301", investigation="INV-2026-0301")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(self.item(pkg, "DEC-2026-0301").load_mode,
                         METADATA_ONLY)
        self.not_selected(pkg, "INV-2026-0301")

    def test_project_constraints_metadata_decision_does_not_expand(self):
        self.fx.doc("constraints.md", "# Constraints\n\nSee DEC-2026-0302.\n")
        self.fx.investigation("INV-2026-0302")
        self.fx.decision("DEC-2026-0302", investigation="INV-2026-0302")
        pkg = self.fx.load()
        self.assertEqual(self.item(pkg, "DEC-2026-0302").load_mode,
                         METADATA_ONLY)
        self.not_selected(pkg, "INV-2026-0302")

    def test_closure_only_decision_body_references_do_not_expand(self):
        self.fx.task(decisions=["DEC-2026-0304"])
        self.fx.investigation("INV-2026-0303")
        self.fx.decision("DEC-2026-0303", status="SUPERSEDED",
                         superseded_by=["DEC-2026-0304"],
                         investigation="INV-2026-0303")
        self.fx.decision("DEC-2026-0304", supersedes=["DEC-2026-0303"])
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(self.item(pkg, "DEC-2026-0303").load_mode,
                         METADATA_ONLY)
        self.not_selected(pkg, "INV-2026-0303")

    def test_caller_full_body_promotion_expands_and_is_deterministic(self):
        self.fx.doc("constraints.md", "# Constraints\n\nSee DEC-2026-0305.\n")
        self.fx.investigation("INV-2026-0305")
        self.fx.decision("DEC-2026-0305", investigation="INV-2026-0305")
        first = self.fx.load(select=["DEC-2026-0305"])
        second = self.fx.load(select=["DEC-2026-0305"])
        self.assertEqual(self.item(first, "DEC-2026-0305").load_mode,
                         FULL_BODY)
        self.item(first, "INV-2026-0305")
        self.assertEqual(first.to_json(), second.to_json())

    def test_task_constraints_full_body_decision_expands(self):
        self.fx.task()
        self.fx.doc("constraints.md", "# Constraints\n\nSee DEC-2026-0306.\n")
        self.fx.investigation("INV-2026-0306")
        self.fx.decision("DEC-2026-0306", investigation="INV-2026-0306")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(self.item(pkg, "DEC-2026-0306").load_mode,
                         FULL_BODY)
        self.item(pkg, "INV-2026-0306")

    def test_supersession_metadata_closure_still_operates(self):
        self.fx.decision("DEC-2026-0307", status="SUPERSEDED",
                         superseded_by=["DEC-2026-0308"])
        self.fx.decision("DEC-2026-0308", supersedes=["DEC-2026-0307"])
        pkg = self.fx.load()
        self.item(pkg, "DEC-2026-0307")
        self.item(pkg, "DEC-2026-0308")
        self.no_diag(pkg, "DECISION_SUPERSESSION_NONRECIPROCAL")


class TestTerminalInvestigationFragment(LoaderTestCase):

    def terminal_package(self, body: str):
        self.fx.task(investigations=["INV-2026-0401"])
        self.fx.investigation("INV-2026-0401", status="CONCLUDED",
                              conclusion_status="SUPPORTED", body=body)
        return self.fx.load(task="TASK-2026-0001")

    def test_missing_current_conclusion_is_not_a_successful_fragment(self):
        pkg = self.terminal_package("# Investigation\n\nNo conclusion section.\n")
        item = self.item(pkg, "INV-2026-0401")
        self.assertEqual(item.load_mode, METADATA_ONLY)
        self.assertIsNone(item.body)
        self.assertIn("CONCLUSION_FRAGMENT_UNAVAILABLE", item.annotations)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_duplicate_current_conclusion_is_ambiguous(self):
        pkg = self.terminal_package(
            "# Investigation\n\n## Current conclusion\n\nOne.\n\n"
            "## Current conclusion\n\nTwo.\n")
        self.assertEqual(self.item(pkg, "INV-2026-0401").load_mode,
                         METADATA_ONLY)
        self.diag(pkg, "AMBIGUOUS_FRAGMENT")

    def test_fenced_fake_current_conclusion_is_missing(self):
        pkg = self.terminal_package(
            "# Investigation\n\n```md\n## Current conclusion\nFake.\n```\n")
        self.assertEqual(self.item(pkg, "INV-2026-0401").load_mode,
                         METADATA_ONLY)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_valid_current_conclusion_materializes_to_heading_boundary(self):
        pkg = self.terminal_package(
            "# Investigation\n\n## Current conclusion\n\nValid.\n\n"
            "### Detail\n\nIncluded.\n\n## Next\n\nExcluded.\n")
        item = self.item(pkg, "INV-2026-0401")
        self.assertEqual(item.load_mode, FRAGMENT)
        self.assertIn("### Detail", item.body)
        self.assertNotIn("## Next", item.body)

    def test_automatic_missing_fragment_is_nonblocking_under_b1(self):
        pkg = self.terminal_package("# Investigation\n")
        d = self.diag(pkg, "INVALID_FRAGMENT")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_explicit_canonical_terminal_selection_remains_full_body(self):
        self.fx.task(investigations=["INV-2026-0402"])
        self.fx.investigation("INV-2026-0402", status="CONCLUDED",
                              conclusion_status="SUPPORTED",
                              body="# Investigation\n\nNo conclusion.\n")
        pkg = self.fx.load(task="TASK-2026-0001",
                           select=["INV-2026-0402"])
        self.assertEqual(self.item(pkg, "INV-2026-0402").load_mode, FULL_BODY)
        self.assertEqual(pkg.package_status, READY)
        self.no_diag(pkg, "INVALID_FRAGMENT")

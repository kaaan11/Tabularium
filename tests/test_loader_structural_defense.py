"""Package-level regressions for context loader structural defense rules."""

from __future__ import annotations

from pathlib import Path

from tabularium.engine.context_loader.model import (
    ACTIONABLE, BLOCKED, DIAGNOSTIC_ONLY, FRAGMENT, FULL_BODY,
    METADATA_ONLY, NOT_ACTIONABLE, NOT_APPLICABLE, READY,
)
from tests.test_context_loader import Fixture, LoaderTestCase


def nested_list(depth: int = 1500) -> str:
    return "[" * depth + "'value'" + "]" * depth


def nested_mapping(depth: int = 1500) -> str:
    return "{k: " * depth + "'value'" + "}" * depth


class Round5Case(LoaderTestCase):
    def raw_record(self, directory: str, filename: str, text: str) -> Path:
        target = self.fx.project / ".ai" / directory / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def malformed_reason(self, pkg) -> str:
        return self.diag(pkg, "MALFORMED_RECORD_QUARANTINED").reason


class TestYamlRecursionLimits(Round5Case):
    def test_deep_manifest_is_diagnostic_input_not_exception(self):
        self.fx.manifest(
            'manifest_version: "1.0"\n'
            'tabularium_ruleset: "0.1.0"\n'
            f"title: {nested_list()}\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
        self.assertEqual(pkg.selected, [])
        reason = self.diag(pkg, "MANIFEST_ENTRY_FAILED").reason
        self.assertIn("nesting complexity", reason)
        self.assertNotIn("RecursionError", reason)

    def test_deep_unrelated_record_list_is_quarantined(self):
        self.fx.task()
        self.raw_record(
            "questions", "QST-2026-0500.md",
            "---\nschema_version: '1.0'\nid: QST-2026-0500\n"
            "title: Q\nquestion: Why?\nstatus: OPEN\nimpact: NON_BLOCKING\n"
            "created_at: '2026-01-01T00:00:00Z'\ncreated_by: human:user\n"
            f"answer: {nested_list()}\n---\n# Q\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        reason = self.malformed_reason(pkg)
        self.assertIn("nesting complexity", reason)
        self.assertNotIn("RecursionError", reason)

    def test_deep_unrelated_record_mapping_is_quarantined(self):
        self.fx.task()
        self.raw_record(
            "evidence", "EVD-2026-0500.md",
            "---\nschema_version: '1.4'\nid: EVD-2026-0500\n"
            "title: E\nsource_type: OBSERVATION\nstatus: ACTIVE\n"
            "observation: Seen\ncollected_at: '2026-01-01T00:00:00Z'\n"
            f"collected_by: human:user\nproject: {nested_mapping()}\n"
            "---\n# E\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertIn("nesting complexity", self.malformed_reason(pkg))

    def test_deep_root_task_blocks_without_exception(self):
        self.raw_record(
            "tasks", "TASK-2026-0001.md",
            "---\nschema_version: '1.10'\nid: TASK-2026-0001\n"
            "title: Root\nobjective: Do it\nstatus: IN_PROGRESS\n"
            "created_at: '2026-01-01T00:00:00Z'\ncreated_by: USER\n"
            f"plan: {nested_list()}\n---\n# Task\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        self.assertIn("nesting complexity", self.malformed_reason(pkg))


class TestValidationOrder(Round5Case):
    def decision_with_status(self, rid: str, status) -> Path:
        return self.fx.rec(
            rid,
            {"schema_version": "1.2", "title": "D", "question": "What?",
             "status": status, "created_at": "2026-01-01T00:00:00Z",
             "created_by": "human:user"},
            "# Decision\n")

    def test_wrong_type_decision_statuses_are_quarantined(self):
        values = ([], {}, None, True, 0)
        for index, value in enumerate(values, start=1):
            with self.subTest(value=value):
                fx = Fixture()
                try:
                    fx.task()
                    fx.rec(
                        f"DEC-2026-05{index:02d}",
                        {"schema_version": "1.2", "title": "D",
                         "question": "What?", "status": value,
                         "created_at": "2026-01-01T00:00:00Z",
                         "created_by": "human:user"},
                        "# Decision\n")
                    pkg = fx.load(task="TASK-2026-0001")
                    self.assertEqual(pkg.package_status, READY)
                    self.assertIn("INVALID_RECORD_QUARANTINED",
                                  [d.code for d in pkg.diagnostics])
                finally:
                    fx.cleanup()

    def test_required_wrong_type_decision_status_blocks_without_lookup(self):
        self.decision_with_status("DEC-2026-0510", [])

        pkg = self.fx.load(select=["DEC-2026-0510"])

        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "DEC-2026-0510")
        self.diag(pkg, "MALFORMED_REFERENCE")

    def test_adjacent_wrong_type_status_branches_do_not_execute(self):
        cases = (
            ("questions", "QST-2026-0511", {
                "schema_version": "1.0", "title": "Q", "question": "Why?",
                "status": {}, "impact": "BLOCKING",
                "created_at": "2026-01-01T00:00:00Z",
                "created_by": "human:user", "answer": "not authoritative"}),
            ("investigations", "INV-2026-0512", {
                "schema_version": "1.4", "title": "I", "objective": "Why?",
                "status": [], "conclusion_status": "SUPPORTED",
                "created_at": "2026-01-01T00:00:00Z",
                "created_by": "human:user"}),
        )
        self.fx.task()
        for _directory, rid, frontmatter in cases:
            self.fx.rec(rid, frontmatter, "# Record\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertGreaterEqual(self.codes(pkg).count("INVALID_RECORD_QUARANTINED"),
                                2)

    def test_wrong_type_task_status_blocks_root_without_lifecycle_branch(self):
        self.fx.rec(
            "TASK-2026-0513",
            {"schema_version": "1.10", "title": "T", "objective": "Do",
             "status": [], "archived_outcome": "COMPLETED",
             "created_at": "2026-01-01T00:00:00Z", "created_by": "USER"},
            "# Task\n")

        pkg = self.fx.load(task="TASK-2026-0513")

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)


class TestApprovedScope(Round5Case):
    def test_scope_without_approval_status_is_invalid_by_key_presence(self):
        self.fx.task(
            approval_required=False,
            approved_scope={"approved_files": ["a.py"]})

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        self.diag(pkg, "ROOT_TASK_INVALID")

    def test_required_approval_without_status_or_scope_remains_valid(self):
        self.fx.task(approval_required=True)

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)

    def test_scope_is_valid_under_recorded_approved_state(self):
        self.fx.task(
            approval_required=True, approval_status="APPROVED",
            approved_by="human:user", approved_at="2026-01-01T01:00:00Z",
            approved_scope={
                "approved_files": ["a.py", "b.py"],
                "approved_operations": ["edit", "test"],
                "approved_risk": "MEDIUM", "approved_plan": "Plan A"})

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertEqual(
            self.item(pkg, "TASK-2026-0001").metadata["approved_scope"]
            ["approved_risk"], "MEDIUM")

    def test_null_and_wrong_type_scope_are_present_and_invalid(self):
        for value in (None, False, []):
            with self.subTest(value=value):
                fx = Fixture()
                try:
                    fx.task(
                        approval_status="APPROVED", approved_by="human:user",
                        approved_at="2026-01-01T01:00:00Z",
                        approved_scope=value)
                    pkg = fx.load(task="TASK-2026-0001")
                    self.assertEqual(pkg.package_status, BLOCKED)
                    self.assertIn("ROOT_TASK_INVALID",
                                  [d.code for d in pkg.diagnostics])
                finally:
                    fx.cleanup()

    def test_partial_malformed_scope_is_invalid(self):
        self.fx.task(
            approval_status="APPROVED", approved_by="human:user",
            approved_at="2026-01-01T01:00:00Z",
            approved_scope={"approved_files": "a.py"})

        pkg = self.fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "ROOT_TASK_INVALID")


class TestAtxHeadings(Round5Case):
    def investigation_fragment(self, body: str):
        self.fx.investigation("INV-2026-0520", body=body)
        return self.fx.load(select=["INV-2026-0520#H1"])

    def test_attached_hypothesis_hashes_are_exact_text_not_h1(self):
        pkg = self.investigation_fragment("# I\n\n### H1###\n\nDecoy.\n")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_spaced_hypothesis_closing_hashes_are_valid(self):
        pkg = self.investigation_fragment("# I\n\n### H1 ###\n\nReal.\n")
        self.assertEqual(pkg.package_status, READY)
        item = self.item(pkg, "INV-2026-0520", "H1")
        self.assertEqual(item.load_mode, FRAGMENT)
        self.assertIn("Real.", item.body)

    def test_exact_hypothesis_ignores_attached_and_fenced_decoys(self):
        pkg = self.investigation_fragment(
            "# I\n\n```text\n### H1\n### H1###\n```\n"
            "### H1###\nDecoy.\n\n### H1\nReal.\n")
        self.assertEqual(pkg.package_status, READY)
        item = self.item(pkg, "INV-2026-0520", "H1")
        self.assertIn("Real.", item.body)

    def test_attached_current_conclusion_is_not_terminal_structure(self):
        self.fx.task()
        self.fx.investigation(
            "INV-2026-0521", task="TASK-2026-0001", status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# I\n\n## Current conclusion###\n\nDecoy.\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        item = self.item(pkg, "INV-2026-0521")
        self.assertEqual(item.load_mode, METADATA_ONLY)
        self.assertIsNone(item.body)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_spaced_current_conclusion_closing_hashes_are_valid(self):
        self.fx.task()
        self.fx.investigation(
            "INV-2026-0522", task="TASK-2026-0001", status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# I\n\n## Current conclusion ###\n\nReal.\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        item = self.item(pkg, "INV-2026-0522")
        self.assertEqual(item.load_mode, FRAGMENT)
        self.assertIn("Real.", item.body)

    def test_exact_current_conclusion_ignores_attached_and_fenced_decoys(self):
        self.fx.task()
        self.fx.investigation(
            "INV-2026-0523", task="TASK-2026-0001", status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# I\n\n```text\n## Current conclusion\n"
                 "## Current conclusion###\n```\n"
                 "## Current conclusion###\nDecoy.\n"
                 "## Current conclusion\nReal.\n")

        pkg = self.fx.load(task="TASK-2026-0001")

        item = self.item(pkg, "INV-2026-0523")
        self.assertEqual(item.load_mode, FRAGMENT)
        self.assertIn("Real.", item.body)
        self.assertNotIn("Decoy.", item.body)

    def test_attached_handoff_headings_do_not_satisfy_body_contract(self):
        self.fx.handoff(
            "HND-2026-0520",
            body="# H\n\n## Current State###\nState.\n"
                 "## Continuation###\nContinue.\n")
        pkg = self.fx.load(select=["HND-2026-0520"])
        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "HND-2026-0520")

    def test_spaced_handoff_closing_hashes_are_valid(self):
        self.fx.handoff(
            "HND-2026-0521",
            body="# H\n\n## Current State ##\nState.\n"
                 "## Continuation ###\nContinue.\n")
        pkg = self.fx.load(select=["HND-2026-0521"])
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(self.item(pkg, "HND-2026-0521").load_mode, FULL_BODY)

    def test_exact_handoff_sections_ignore_attached_and_fenced_decoys(self):
        self.fx.handoff(
            "HND-2026-0522",
            body="# H\n\n```text\n## Current State\n"
                 "## Continuation###\n```\n## Current State###\nDecoy.\n"
                 "## Current State\nReal.\n## Continuation\nContinue.\n")
        pkg = self.fx.load(select=["HND-2026-0522"])
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(self.item(pkg, "HND-2026-0522").load_mode, FULL_BODY)

    def test_attached_report_headings_do_not_satisfy_body_contract(self):
        self.fx.report(
            "RPT-2026-0520",
            body="# R\n\n## Outcome###\nDone.\n## Validation###\nChecked.\n"
                 "## Limitations###\nNone.\n")
        pkg = self.fx.load(select=["RPT-2026-0520"])
        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "RPT-2026-0520")

    def test_spaced_report_closing_hashes_are_valid(self):
        self.fx.report(
            "RPT-2026-0521",
            body="# R\n\n## Outcome ##\nDone.\n## Validation ###\nChecked.\n"
                 "## Limitations ####\nNone.\n")
        pkg = self.fx.load(select=["RPT-2026-0521"])
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(self.item(pkg, "RPT-2026-0521").load_mode, FULL_BODY)

    def test_exact_report_sections_ignore_attached_and_fenced_decoys(self):
        self.fx.report(
            "RPT-2026-0522",
            body="# R\n\n~~~text\n## Outcome\n## Validation###\n~~~\n"
                 "## Outcome###\nDecoy.\n## Outcome\nReal.\n"
                 "## Validation\nChecked.\n## Limitations\nNone.\n")
        pkg = self.fx.load(select=["RPT-2026-0522"])
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(self.item(pkg, "RPT-2026-0522").load_mode, FULL_BODY)

    def test_attached_alternatives_heading_cannot_enable_citation(self):
        self.fx.evidence("EVD-2026-0520")
        self.fx.decision(
            "DEC-2026-0520",
            body="# D\n\n## Alternatives###\nEVD-2026-0520\n")
        self.fx.task(decisions=["DEC-2026-0520"])

        pkg = self.fx.load(task="TASK-2026-0001")

        self.not_selected(pkg, "EVD-2026-0520")

    def test_spaced_alternatives_closing_hashes_enable_exact_citation(self):
        self.fx.evidence("EVD-2026-0521")
        self.fx.decision(
            "DEC-2026-0521",
            body="# D\n\n## Alternatives ###\nEVD-2026-0521\n")
        self.fx.task(decisions=["DEC-2026-0521"])

        pkg = self.fx.load(task="TASK-2026-0001")

        self.item(pkg, "EVD-2026-0521")

    def test_fenced_and_attached_alternatives_do_not_enable_citations(self):
        self.fx.evidence("EVD-2026-0522")
        self.fx.decision(
            "DEC-2026-0522",
            body="# D\n\n```text\n## Alternatives\nEVD-2026-0522\n"
                 "## Alternatives###\nEVD-2026-0522\n```\n"
                 "## Alternatives###\nEVD-2026-0522\n")
        self.fx.task(decisions=["DEC-2026-0522"])

        pkg = self.fx.load(task="TASK-2026-0001")

        self.not_selected(pkg, "EVD-2026-0522")

    def test_duplicate_exact_alternatives_sections_are_both_eligible(self):
        self.fx.evidence("EVD-2026-0523")
        self.fx.evidence("EVD-2026-0524")
        self.fx.decision(
            "DEC-2026-0523",
            body="# D\n\n## Alternatives\nEVD-2026-0523\n"
                 "## Alternatives\nEVD-2026-0524\n")
        self.fx.task(decisions=["DEC-2026-0523"])

        pkg = self.fx.load(task="TASK-2026-0001")

        self.item(pkg, "EVD-2026-0523")
        self.item(pkg, "EVD-2026-0524")


class TestEphemeralSerialization(Round5Case):
    """JSON output is ephemeral: serializing a package persists nothing."""

    def test_json_output_writes_nothing(self):
        import contextlib
        import io
        import json

        from tabularium.engine.context_loader.cli import main

        def tree() -> dict[str, tuple[int, int]]:
            # Project and ruleset both live under tmp; nothing may appear,
            # vanish, or be rewritten anywhere below it.
            return {str(p.relative_to(self.fx.tmp)):
                    (p.stat().st_size, p.stat().st_mtime_ns)
                    for p in sorted(self.fx.tmp.rglob("*"))}

        self.fx.task()
        before = self.fx.snapshot()
        tree_before = tree()

        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(json.loads(pkg.to_json())["package_status"], READY)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--project", str(self.fx.project),
                       "--task", "TASK-2026-0001",
                       "--ai-os-root", str(self.fx.ai_os_root)])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["package_status"], READY)

        self.assertEqual(self.fx.snapshot(), before)
        self.assertEqual(tree(), tree_before)

"""The text of an Investigation FRAGMENT selection reaches the projection.

1. Explicit fragment selection (INV-...#<fragment>): the selected hypothesis text is projected.
2. Fragment derived from the conclusion ("Current conclusion" section): the text is projected.
3. Budget: a fragment longer than the limit is truncated and recorded, exactly like FULL_BODY.
4. Unchanged: METADATA_ONLY stays bodiless; FULL_BODY output is unchanged.
"""

from __future__ import annotations

import unittest

from tabularium.engine.projection import PROJECTED, project
from tabularium.engine.projection.budget import HOT
from tabularium.engine.projection.projector import MIN_TRUNCATION_CHARS
from tests.test_context_loader import Fixture

INTENT = "continue the project"


class TestProjectionFragment(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    def test_explicit_fragment_selection_includes_body(self):
        """An explicit fragment selection (INV-...#<fragment>) projects the hypothesis text."""
        self.fx.task("TASK-2026-0001")
        self.fx.task("TASK-2026-0002")
        body = (
            "# Investigation\n\n"
            "### H1 — first hypothesis\n\n"
            "Hypothesis one detailed narrative that must be projected.\n\n"
            "### H2 — second hypothesis\n\n"
            "Second hypothesis narrative.\n\n"
            "## Current conclusion\n\n"
            "Preliminary conclusion.\n"
        )
        self.fx.investigation("INV-2026-0001", task="TASK-2026-0002", body=body)

        pkg = self.fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        selected = [s for s in pkg.selected if s.record_id == "INV-2026-0001"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].load_mode, "FRAGMENT")
        self.assertEqual(selected[0].fragment, "H1")
        self.assertTrue(selected[0].body)

        res = project(pkg, INTENT)
        self.assertEqual(res.outcome, PROJECTED)
        self.assertIn("investigations", res.payload.section_names())
        invs = res.payload.get("investigations")
        self.assertEqual(len(invs), 1)
        inv = invs[0]
        self.assertEqual(inv["id"], "INV-2026-0001")
        self.assertEqual(inv["load_mode"], "FRAGMENT")
        self.assertEqual(inv["fragment"], "H1")
        self.assertIn("body", inv, "FRAGMENT item in projection must carry 'body'")
        self.assertIn("Hypothesis one detailed narrative that must be projected.", inv["body"])
        # Only the selected fragment: the other hypothesis and the conclusion stay out.
        self.assertNotIn("Second hypothesis narrative.", inv["body"])
        self.assertNotIn("Preliminary conclusion.", inv["body"])

    def test_derived_conclusion_fragment_includes_body(self):
        """A fragment derived from the 'Current conclusion' section is projected."""
        self.fx.task("TASK-2026-0001", investigations=["INV-2026-0001"])
        body = (
            "# Investigation\n\n"
            "## Background\n\n"
            "Some detailed background investigation text.\n\n"
            "## Current conclusion\n\n"
            "The root cause was definitively isolated and verified.\n"
        )
        # CONCLUDED status with conclusion_status SUPPORTED causes loader._select_investigation
        # to extract 'Current conclusion' as FRAGMENT
        self.fx.investigation(
            "INV-2026-0001",
            task="TASK-2026-0001",
            status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body=body,
        )

        pkg = self.fx.load(task="TASK-2026-0001")
        selected = [s for s in pkg.selected if s.record_id == "INV-2026-0001"]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].load_mode, "FRAGMENT")
        self.assertIsNone(selected[0].fragment)
        self.assertTrue(selected[0].body)

        res = project(pkg, INTENT)
        self.assertEqual(res.outcome, PROJECTED)
        self.assertIn("investigations", res.payload.section_names())
        invs = res.payload.get("investigations")
        self.assertEqual(len(invs), 1)
        inv = invs[0]
        self.assertEqual(inv["id"], "INV-2026-0001")
        self.assertEqual(inv["load_mode"], "FRAGMENT")
        self.assertIn("body", inv, "Derived conclusion fragment must carry 'body'")
        self.assertIn("The root cause was definitively isolated and verified.", inv["body"])
        self.assertNotIn("Some detailed background investigation text.", inv["body"])

    def test_fragment_body_respects_budget_and_truncates(self):
        """A fragment over the budget is truncated and a Truncation is recorded."""
        self.fx.task("TASK-2026-0001")
        self.fx.task("TASK-2026-0002")
        long_paragraph = "Critical hypothesis reasoning that fills the token budget. " * 200
        body = (
            "# Investigation\n\n"
            f"### H1 — oversized hypothesis\n\n"
            f"{long_paragraph}\n\n"
            "## Current conclusion\n\n"
            "Done.\n"
        )
        self.fx.investigation("INV-2026-0001", task="TASK-2026-0002", body=body)

        pkg = self.fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        res = project(pkg, INTENT, profile=HOT)
        self.assertEqual(res.outcome, PROJECTED)
        invs = res.payload.get("investigations")
        self.assertEqual(len(invs), 1)
        inv = invs[0]
        self.assertIn("body", inv)
        self.assertIn("[TRUNCATED:", inv["body"])

        inv_truncs = [
            t for t in res.truncations
            if t.item_id == "INV-2026-0001" and t.field_name == "body"
        ]
        self.assertEqual(len(inv_truncs), 1, "Expected a Truncation record for the fragment body")
        t = inv_truncs[0]
        self.assertEqual(t.section, "investigations")
        self.assertLess(t.kept_chars, t.original_chars)
        self.assertGreaterEqual(t.kept_chars, MIN_TRUNCATION_CHARS)

    def test_invariants_metadata_only_and_full_body_unaffected(self):
        """METADATA_ONLY stays bodiless; FULL_BODY output is unchanged."""
        self.fx.task("TASK-2026-0001", investigations=["INV-2026-0001", "INV-2026-0002"])
        # INV-1 OPEN -> FULL_BODY
        self.fx.investigation(
            "INV-2026-0001",
            task="TASK-2026-0001",
            status="OPEN",
            body="# Investigation 1\n\nFull body of investigation 1.\n",
        )
        # INV-2 CONCLUDED without Current conclusion -> METADATA_ONLY
        self.fx.investigation(
            "INV-2026-0002",
            task="TASK-2026-0001",
            status="CONCLUDED",
            conclusion_status="SUPPORTED",
            body="# Investigation 2\n\nNo conclusion heading here.\n",
        )

        pkg = self.fx.load(task="TASK-2026-0001")
        sel_by_id = {s.record_id: s for s in pkg.selected}
        self.assertEqual(sel_by_id["INV-2026-0001"].load_mode, "FULL_BODY")
        self.assertEqual(sel_by_id["INV-2026-0002"].load_mode, "METADATA_ONLY")

        res = project(pkg, INTENT)
        self.assertEqual(res.outcome, PROJECTED)
        inv_by_id = {i["id"]: i for i in res.payload.get("investigations")}

        # FULL_BODY has body
        self.assertEqual(inv_by_id["INV-2026-0001"]["load_mode"], "FULL_BODY")
        self.assertIn("body", inv_by_id["INV-2026-0001"])
        self.assertIn("Full body of investigation 1.", inv_by_id["INV-2026-0001"]["body"])

        # METADATA_ONLY has NO body
        self.assertEqual(inv_by_id["INV-2026-0002"]["load_mode"], "METADATA_ONLY")
        self.assertNotIn("body", inv_by_id["INV-2026-0002"])

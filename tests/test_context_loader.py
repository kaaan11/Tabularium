"""Conformance tests for the M2.4 read-only Context Loader.

Fixtures are built in temporary directories. No test reads, writes, or
otherwise touches the real project's M1 records.


Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from tabularium.engine.context_loader import load_context  # noqa: E402
from tabularium.engine.context_loader.confinement import ProjectScope  # noqa: E402
from tabularium.engine.context_loader.discovery import discover  # noqa: E402
from tabularium.engine.context_loader.model import (  # noqa: E402
    ACTIONABLE, AMBIGUITY, BLOCKED, CONFLICT, DIAGNOSTIC, DIAGNOSTIC_ONLY,
    DRIFT, FRAGMENT, FULL_BODY, GOVERNED, METADATA_ONLY, NEITHER,
    NOT_ACTIONABLE, NOT_APPLICABLE, PACKAGE, READY, TASK_ACTIONABILITY,
    WARNING)

RULESET = "0.1.0"
SUBDIR = {"TASK": "tasks", "QST": "questions", "DEC": "decisions",
          "INV": "investigations", "EVD": "evidence", "HND": "handoffs",
          "RPT": "reports"}


# --- fixture helpers --------------------------------------------------------

class Fixture:
    """A throwaway project plus a throwaway ruleset root."""

    def __init__(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="ctxload-"))
        self.project = self.tmp / "project"
        self.ai_os_root = self.tmp / "ruleset"
        (self.project / ".ai").mkdir(parents=True)
        self.ai_os_root.mkdir()
        (self.ai_os_root / "VERSION").write_text(RULESET + "\n",
                                                 encoding="utf-8")
        self.manifest({"manifest_version": "1.0", "tabularium_ruleset": RULESET,
                       "title": "Fixture Project"})
        self.doc("project.md", "# Fixture Project\n\nCharter.\n")
        self.doc("constraints.md", "# Constraints\n\nNone.\n")

    # -- writers (fixture construction only) ------------------------------
    def manifest(self, data) -> None:
        """Write a manifest. A dict is emitted as explicitly quoted strings,
        which is what Manifest v1.0 requires; raw text is written verbatim so
        adversarial shapes can be exercised."""
        if isinstance(data, str):
            text = data
        else:
            text = "".join(f'{k}: "{v}"\n' for k, v in data.items())
        (self.project / ".ai" / "manifest.yaml").write_text(text,
                                                            encoding="utf-8")

    def doc(self, name: str, text: str) -> None:
        (self.project / ".ai" / name).write_text(text, encoding="utf-8")

    def remove(self, rel: str) -> None:
        (self.project / ".ai" / rel).unlink()

    def rec(self, rid: str, fm: dict, body: str = "", filename: str | None = None):
        sub = SUBDIR[rid.split("-", 1)[0]]
        d = self.project / ".ai" / sub
        d.mkdir(parents=True, exist_ok=True)
        fm = {"schema_version": fm.pop("schema_version", "1.0"), "id": rid,
              **fm}
        text = "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n" + body
        path = d / f"{filename or rid}.md"
        path.write_text(text, encoding="utf-8")
        return path

    def task(self, rid="TASK-2026-0001", **fm):
        base = {"schema_version": "1.10", "title": "Root", "objective": "Do it",
                "status": "IN_PROGRESS", "created_at": "2026-01-01T00:00:00Z",
                "created_by": "USER"}
        base.update(fm)
        return self.rec(rid, base, "# Root Task\n\nBody.\n")

    def question(self, rid, **fm):
        base = {"title": "Q", "question": "Why?", "status": "OPEN",
                "impact": "NON_BLOCKING", "created_at": "2026-01-02T00:00:00Z",
                "created_by": "human:user"}
        base.update(fm)
        return self.rec(rid, base, "# Question\n\nBody.\n")

    def decision(self, rid, **fm):
        base = {"schema_version": "1.2", "title": "D", "status": "APPROVED",
                "question": "What?", "created_at": "2026-01-03T00:00:00Z",
                "created_by": "human:user"}
        body = fm.pop("body", "# Decision\n\nBody.\n")
        base.update(fm)
        # Decision v1.2 D1: approval provenance is required for APPROVED and
        # SUPERSEDED, and must be absent for every other status.
        if base["status"] in ("APPROVED", "SUPERSEDED"):
            base.setdefault("approved_by", "human:user")
            base.setdefault("approved_at", "2026-01-03T01:00:00Z")
        return self.rec(rid, base, body)

    def investigation(self, rid, **fm):
        base = {"schema_version": "1.4", "title": "I", "status": "OPEN",
                "objective": "Find out", "created_at": "2026-01-04T00:00:00Z",
                "created_by": "ai:claude"}
        body = fm.pop("body", "# Investigation\n\n### H1\n\nFirst.\n\n"
                              "## Current conclusion\n\nNone yet.\n")
        base.update(fm)
        return self.rec(rid, base, body)

    def evidence(self, rid, **fm):
        base = {"schema_version": "1.4", "title": "E",
                "source_type": "OBSERVATION", "status": "ACTIVE",
                "observation": "It failed.", "collected_at":
                "2026-01-05T00:00:00Z", "collected_by": "ai:claude"}
        base.update(fm)
        return self.rec(rid, base, "# Evidence\n\nNarrative.\n")

    def handoff(self, rid, **fm):
        base = {"title": "H", "task": "TASK-2026-0001",
                "created_at": "2026-01-06T00:00:00Z", "created_by": "ai:claude"}
        body = fm.pop("body", "# Handoff\n\n## Current State\n\nIn progress.\n\n"
                               "## Continuation\n\nContinue safely.\n")
        base.update(fm)
        return self.rec(rid, base, body)

    def report(self, rid, **fm):
        base = {"title": "R", "task": "TASK-2026-0001",
                "created_at": "2026-01-07T00:00:00Z", "created_by": "ai:claude"}
        body = fm.pop("body", "# Report\n\n## Outcome\n\nComplete.\n\n"
                               "## Validation\n\nChecked.\n\n"
                               "## Limitations\n\nNone known.\n")
        base.update(fm)
        return self.rec(rid, base, body)

    # -- runner ------------------------------------------------------------
    def load(self, **kw):
        kw.setdefault("ai_os_root", self.ai_os_root)
        return load_context(self.project, **kw)

    def snapshot(self) -> dict[str, str]:
        out = {}
        for p in sorted(self.project.rglob("*")):
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(self.project))] = "{}:{}:{}".format(
                    hashlib.sha256(p.read_bytes()).hexdigest(),
                    st.st_size, st.st_mtime_ns)
        return out

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


class LoaderTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    # -- assertions --------------------------------------------------------
    def codes(self, pkg) -> list[str]:
        return [d.code for d in pkg.diagnostics]

    def diag(self, pkg, code):
        hits = [d for d in pkg.diagnostics if d.code == code]
        self.assertTrue(hits, f"expected diagnostic {code}; got {self.codes(pkg)}")
        return hits[0]

    def no_diag(self, pkg, code) -> None:
        self.assertNotIn(code, self.codes(pkg))

    def item(self, pkg, rid, fragment=None):
        hits = [i for i in pkg.selected
                if i.record_id == rid and i.fragment == fragment]
        self.assertEqual(len(hits), 1,
                         f"expected exactly one selected {rid}"
                         f"{'#' + fragment if fragment else ''}; "
                         f"got {[(i.record_id, i.fragment) for i in pkg.selected]}")
        return hits[0]

    def not_selected(self, pkg, rid) -> None:
        self.assertNotIn(rid, [i.record_id for i in pkg.selected])


# --- SC1 - SC17 -------------------------------------------------------------

class TestScenarios(LoaderTestCase):

    def test_sc1_actionable_task_with_selected_approved_decision(self):
        fx = self.fx
        fx.decision("DEC-2026-0001")
        fx.decision("DEC-2026-0002")  # unrelated approved Decision
        fx.task(decisions=["DEC-2026-0001"], approval_required=False)
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        root = self.item(pkg, "TASK-2026-0001")
        self.assertEqual(root.load_mode, FULL_BODY)
        self.assertIsNotNone(root.body)

        sel = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(sel.load_mode, FULL_BODY)
        self.assertEqual(sel.metadata["approved_by"], "human:user")
        self.assertEqual(sel.metadata["approved_at"], "2026-01-03T01:00:00Z")

        other = self.item(pkg, "DEC-2026-0002")
        self.assertEqual(other.load_mode, METADATA_ONLY)
        self.assertIn("PROJECT_APPROVED_UNSCOPED_CANDIDATE",
                      other.selection_reasons)
        # exit assertion 8: selective approved Decision body loading
        self.assertIsNone(other.body)
        # no Decision is labeled singular current
        for i in pkg.selected:
            self.assertNotIn("CURRENT", i.annotations)

    def test_sc2_applicable_open_blocking_question(self):
        fx = self.fx
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="OPEN",
                    impact="BLOCKING")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        q = self.item(pkg, "QST-2026-0001")
        self.assertEqual(q.load_mode, FULL_BODY)
        self.assertIn("RELEVANT", q.annotations)
        d = self.diag(pkg, "OPEN_BLOCKING_QUESTION")
        self.assertEqual(d.blocking_dimension, TASK_ACTIONABILITY)

    def test_sc3_selected_approved_decision_branch_ambiguity(self):
        fx = self.fx
        # A -> {B, C};  B -> D
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0002", "DEC-2026-0003"])
        fx.decision("DEC-2026-0002", status="SUPERSEDED",
                    supersedes=["DEC-2026-0001"],
                    superseded_by=["DEC-2026-0004"])
        fx.decision("DEC-2026-0003", status="APPROVED",
                    supersedes=["DEC-2026-0001"])
        fx.decision("DEC-2026-0004", status="APPROVED",
                    supersedes=["DEC-2026-0002"])
        fx.task(decisions=["DEC-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertEqual(self.item(pkg, "DEC-2026-0001").load_mode, FULL_BODY)
        for rid in ("DEC-2026-0002", "DEC-2026-0003", "DEC-2026-0004"):
            self.assertEqual(self.item(pkg, rid).load_mode, METADATA_ONLY,
                             f"{rid} was reached only through closure")
        self.assertIn("HISTORICAL", self.item(pkg, "DEC-2026-0002").annotations)
        self.assertEqual(self.item(pkg, "DEC-2026-0003").native_status,
                         "APPROVED")
        self.assertEqual(self.item(pkg, "DEC-2026-0004").native_status,
                         "APPROVED")
        amb = self.diag(pkg, "DECISION_MULTIPLE_SUCCESSORS")
        self.assertEqual(amb.category, AMBIGUITY)
        self.assertEqual(amb.blocking_dimension, NEITHER)
        self.no_diag(pkg, "DECISION_SUPERSESSION_CYCLE")

    def test_sc4_report_outcome_versus_native_task_state(self):
        fx = self.fx
        fx.report("RPT-2026-0001")
        fx.task(status="IN_PROGRESS", report="RPT-2026-0001")
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertEqual(self.item(pkg, "TASK-2026-0001").native_status,
                         "IN_PROGRESS")
        r = self.item(pkg, "RPT-2026-0001")
        self.assertEqual(r.load_mode, FULL_BODY)
        self.assertEqual(r.family, "Report")
        d = self.diag(pkg, "REPORT_TASK_STATE_AUTHORITY_BOUNDARY")
        self.assertEqual(d.category, WARNING)
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_sc5_current_state_status_drift(self):
        fx = self.fx
        fx.task(status="IN_PROGRESS")
        fx.doc("current-state.md",
               "# Current state\n\n- TASK-2026-0001 is COMPLETED\n")
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertEqual(self.item(pkg, "TASK-2026-0001").native_status,
                         "IN_PROGRESS")
        drift = self.diag(pkg, "CURRENT_STATE_TASK_STATUS_DRIFT")
        self.assertEqual(drift.category, DRIFT)
        self.assertEqual(drift.blocking_dimension, NEITHER)
        stale = self.diag(pkg, "STALE-SUSPECTED")
        self.assertEqual(stale.blocking_dimension, NEITHER)

    def test_sc6_generic_task_reference_to_retracted_evidence(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001", status="RETRACTED",
                    source="pytest", source_locator="tests/test_x.py::t")
        fx.task(evidence=["EVD-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        e = self.item(pkg, "EVD-2026-0001")
        # exit assertion 15: observation, status and provenance inseparable
        self.assertEqual(e.metadata["observation"], "It failed.")
        self.assertEqual(e.metadata["source_type"], "OBSERVATION")
        self.assertEqual(e.native_status, "RETRACTED")
        self.assertEqual(e.metadata["reliability"], "NOT_RECORDED")
        self.assertEqual(e.metadata["collected_by"], "ai:claude")
        self.assertEqual(e.metadata["source_locator"], "tests/test_x.py::t")
        self.assertTrue(e.selection_reasons)
        self.assertIn("RETRACTED_NEVER_ACTIVE_SUPPORT", e.annotations)
        d = self.diag(pkg, "RETRACTED_EVIDENCE_REFERENCE")
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_sc7_multiple_handoff_and_report_candidates(self):
        fx = self.fx
        for n, ts in ((1, "2026-02-01T00:00:00Z"), (2, "2026-02-03T00:00:00Z"),
                      (3, "2026-02-02T00:00:00Z")):
            fx.handoff(f"HND-2026-000{n}", created_at=ts)
            fx.report(f"RPT-2026-000{n}", created_at=ts)
        fx.task(updated_at="2026-01-01T00:00:00Z")
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        for n in (1, 2, 3):
            self.assertEqual(self.item(pkg, f"HND-2026-000{n}").load_mode,
                             METADATA_ONLY)
            self.assertEqual(self.item(pkg, f"RPT-2026-000{n}").load_mode,
                             METADATA_ONLY)
        for code in ("MULTIPLE_HANDOFF_CANDIDATES", "MULTIPLE_REPORT_CANDIDATES"):
            d = self.diag(pkg, code)
            self.assertEqual(d.category, WARNING)
            self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertNotIn(AMBIGUITY, [d.category for d in pkg.diagnostics])
        # RECENT goes to exactly the newest candidate in each family
        self.assertIn("RECENT", self.item(pkg, "HND-2026-0002").annotations)
        self.assertIn("RECENT", self.item(pkg, "RPT-2026-0002").annotations)
        for rid in ("HND-2026-0001", "HND-2026-0003",
                    "RPT-2026-0001", "RPT-2026-0003"):
            self.assertNotIn("RECENT", self.item(pkg, rid).annotations)

    def test_sc7_recent_suppressed_when_disabled(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-02-01T00:00:00Z")
        fx.handoff("HND-2026-0002", created_at="2026-02-02T00:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", recent=False)
        for i in pkg.selected:
            self.assertNotIn("RECENT", i.annotations)

    def test_sc8_required_exact_fragment_missing(self):
        fx = self.fx
        fx.investigation("INV-2026-0001",
                         body="# Inv\n\n### H1\n\nOnly one hypothesis.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H3"])

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        d = self.diag(pkg, "INVALID_FRAGMENT")
        self.assertEqual(d.blocking_dimension, PACKAGE)
        self.assertEqual(d.target, "INV-2026-0001#H3")
        self.assertEqual(d.field_or_fragment, "### H3")
        parent = self.item(pkg, "INV-2026-0001")
        self.assertEqual(parent.metadata["title"], "I")   # parent preserved
        self.assertIsNone(parent.body)

    def test_sc8_fragment_failure_without_task_is_not_applicable(self):
        fx = self.fx
        fx.investigation("INV-2026-0001", body="# Inv\n\n### H1\n\nx\n")
        pkg = fx.load(select=["INV-2026-0001#H3"])
        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)

    def test_sc9_required_ruleset_unavailable(self):
        fx = self.fx
        fx.manifest({"manifest_version": "1.0", "tabularium_ruleset": "9.9.9",
                     "title": "Fixture Project"})
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
        self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
        self.assertEqual(pkg.selected, [])
        self.assertIsNone(pkg.root_task)
        self.diag(pkg, "RULESET_UNAVAILABLE")

    def test_sc10_governed_project_entry_without_task(self):
        fx = self.fx
        fx.task("TASK-2026-0001", status="IN_PROGRESS")
        fx.task("TASK-2026-0002", status="COMPLETED")
        fx.question("QST-2026-0001", status="OPEN", impact="BLOCKING")
        fx.question("QST-2026-0002", status="ANSWERED", impact="BLOCKING")
        fx.decision("DEC-2026-0001", status="APPROVED")
        fx.decision("DEC-2026-0002", status="REJECTED")
        fx.investigation("INV-2026-0001")
        fx.evidence("EVD-2026-0001")
        fx.handoff("HND-2026-0001")
        fx.report("RPT-2026-0001")
        fx.doc("architecture.md", "# Architecture\n")
        pkg = fx.load()

        self.assertEqual(pkg.entry_mode, GOVERNED)
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
        self.assertIsNone(pkg.root_task)
        self.assertIsNotNone(pkg.foundations["project.md"])
        self.assertIsNotNone(pkg.foundations["constraints.md"])
        self.assertNotIn("architecture.md", pkg.foundations)

        self.assertEqual(self.item(pkg, "TASK-2026-0001").load_mode,
                         METADATA_ONLY)
        self.not_selected(pkg, "TASK-2026-0002")
        q = self.item(pkg, "QST-2026-0001")
        self.assertEqual(q.metadata["question"], "Why?")
        self.not_selected(pkg, "QST-2026-0002")
        self.assertEqual(self.item(pkg, "DEC-2026-0001").load_mode,
                         METADATA_ONLY)
        self.not_selected(pkg, "DEC-2026-0002")
        for rid in ("INV-2026-0001", "EVD-2026-0001", "HND-2026-0001",
                    "RPT-2026-0001"):
            self.not_selected(pkg, rid)

    def test_sc11_retracted_explicit_investigation_support(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001", status="RETRACTED")
        fx.investigation("INV-2026-0001", status="OPEN",
                         supporting_evidence=["EVD-2026-0001"])
        fx.task(investigations=["INV-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertEqual(self.item(pkg, "INV-2026-0001").load_mode, FULL_BODY)
        e = self.item(pkg, "EVD-2026-0001")
        self.assertEqual(e.native_status, "RETRACTED")
        self.assertEqual(e.metadata["collected_by"], "ai:claude")
        d = self.diag(pkg, "RETRACTED_EXPLICIT_SUPPORT")
        self.assertEqual(d.category, CONFLICT)
        self.assertEqual(d.semantic_owner, "Investigation")
        self.assertEqual(d.field_or_fragment, "supporting_evidence")
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_sc12_unrelated_approved_decision_ambiguity(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0002", "DEC-2026-0003"])
        fx.decision("DEC-2026-0002", status="APPROVED",
                    supersedes=["DEC-2026-0001"])
        fx.decision("DEC-2026-0003", status="APPROVED",
                    supersedes=["DEC-2026-0001"])
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        a = self.item(pkg, "DEC-2026-0001")
        self.assertIn("SUPERSESSION_CLOSURE", a.selection_reasons)
        self.assertEqual(a.load_mode, METADATA_ONLY)
        self.assertIsNone(a.body)
        self.assertEqual(a.metadata["superseded_by"],
                         ["DEC-2026-0002", "DEC-2026-0003"])
        amb = self.diag(pkg, "DECISION_MULTIPLE_SUCCESSORS")
        self.assertEqual(amb.category, AMBIGUITY)
        self.assertEqual(amb.blocking_dimension, NEITHER)

    def test_sc13_missing_generic_task_relationship(self):
        fx = self.fx
        fx.task(questions=["QST-2026-0404"])
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        d = self.diag(pkg, "MISSING_REFERENCE")
        self.assertEqual(d.category, WARNING)
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(d.sources, ["TASK-2026-0001"])
        self.assertEqual(d.field_or_fragment, "questions")
        self.assertEqual(d.target, "QST-2026-0404")
        self.assertEqual(d.wave, 1)
        self.not_selected(pkg, "QST-2026-0404")

    def test_sc14_duplicate_required_root_task_id(self):
        fx = self.fx
        fx.task()
        fx.rec("TASK-2026-0001",
               {"schema_version": "1.10", "title": "Dup", "objective": "x",
                "status": "PLANNED", "created_at": "2026-01-01T00:00:00Z",
                "created_by": "USER"},
               filename="TASK-2026-0002")
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        self.assertIsNone(pkg.root_task)
        d = [x for x in pkg.diagnostics
             if x.code == "RESOLUTION_AMBIGUOUS"
             and x.blocking_dimension == PACKAGE]
        self.assertTrue(d, "expected a PACKAGE-blocking RESOLUTION_AMBIGUOUS")
        self.assertEqual(sorted(d[0].sources),
                         [".ai/tasks/TASK-2026-0001.md",
                          ".ai/tasks/TASK-2026-0002.md"])

    def test_sc15_bounded_traversal(self):
        fx = self.fx
        fx.evidence("EVD-2026-0003")
        fx.evidence("EVD-2026-0002", related_evidence=["EVD-2026-0003"])
        fx.evidence("EVD-2026-0001", related_evidence=["EVD-2026-0002"])
        fx.investigation("INV-2026-0002")
        fx.investigation("INV-2026-0001", task="TASK-2026-0001",
                         related_investigations=["INV-2026-0002"])
        fx.handoff("HND-2026-0002", task="TASK-2026-0009")
        fx.handoff("HND-2026-0001", previous_handoff="HND-2026-0002")
        fx.task("TASK-2026-0002")
        fx.task(evidence=["EVD-2026-0001"],
                related_tasks=["TASK-2026-0002"],
                context_items=["docs/x.md"], evaluations=["EVAL-1"])
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertTrue(all((i.wave or 0) <= 2 for i in pkg.selected))
        # Wave 1 Evidence expands once into Wave 2, as metadata diagnostics
        self.assertEqual(self.item(pkg, "EVD-2026-0001").wave, 1)
        e2 = self.item(pkg, "EVD-2026-0002")
        self.assertEqual(e2.wave, 2)
        self.assertEqual(e2.load_mode, METADATA_ONLY)
        # and stops there
        self.not_selected(pkg, "EVD-2026-0003")   # would be Wave 3
        self.not_selected(pkg, "INV-2026-0002")   # related_investigations
        self.not_selected(pkg, "HND-2026-0002")   # previous_handoff
        # related Task is metadata navigation, never a recursive body load
        self.assertEqual(self.item(pkg, "TASK-2026-0002").load_mode,
                         METADATA_ONLY)
        boundaries = [o for o in pkg.omissions if o.get("traversal_boundary")]
        self.assertTrue(boundaries)
        reasons = " ".join(str(o["reason"]) for o in pkg.omissions)
        self.assertIn("context_items", reasons)
        self.assertIn("evaluations", reasons)

    def test_sc16_native_blocked_task(self):
        fx = self.fx
        fx.task(status="BLOCKED")
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        self.assertEqual(self.item(pkg, "TASK-2026-0001").native_status,
                         "BLOCKED")
        trace = [t for t in pkg.selection_trace
                 if t.get("step") == "task_actionability"][0]
        self.assertIn("TASK_STATUS_BLOCKED", trace["closed_rule_reasons"])

    def test_sc17_decision_supersession_cycle(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    supersedes=["DEC-2026-0002"],
                    superseded_by=["DEC-2026-0002"])
        fx.decision("DEC-2026-0002", status="SUPERSEDED",
                    supersedes=["DEC-2026-0001"],
                    superseded_by=["DEC-2026-0001"])
        fx.task(decisions=["DEC-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")   # must terminate

        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.assertEqual(self.item(pkg, "DEC-2026-0001").load_mode, FULL_BODY)
        self.assertEqual(self.item(pkg, "DEC-2026-0002").load_mode,
                         METADATA_ONLY)
        self.assertEqual(self.item(pkg, "DEC-2026-0002").metadata["supersedes"],
                         ["DEC-2026-0001"])
        d = self.diag(pkg, "DECISION_SUPERSESSION_CYCLE")
        self.assertEqual(d.category, CONFLICT)
        self.assertEqual(d.blocking_dimension, PACKAGE)

    def test_sc17_unscoped_cycle_is_warning_only(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    supersedes=["DEC-2026-0002"],
                    superseded_by=["DEC-2026-0002", "DEC-2026-0003"])
        fx.decision("DEC-2026-0002", status="SUPERSEDED",
                    supersedes=["DEC-2026-0001"],
                    superseded_by=["DEC-2026-0001"])
        fx.decision("DEC-2026-0003", status="APPROVED",
                    supersedes=["DEC-2026-0001"])
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")

        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        d = self.diag(pkg, "PROJECT_DECISION_SUPERSESSION_CYCLE")
        self.assertEqual(d.category, WARNING)
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.no_diag(pkg, "DECISION_SUPERSESSION_CYCLE")


# --- M2.3 exit assertions not already covered above -------------------------

class TestExitAssertions(LoaderTestCase):

    def test_ea2_ready_package_for_approval_waiting_task(self):
        fx = self.fx
        fx.task(status="WAITING_APPROVAL", approval_required=True,
                approval_status="PENDING")
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        trace = [t for t in pkg.selection_trace
                 if t.get("step") == "task_actionability"][0]
        self.assertIn("TASK_STATUS_WAITING_APPROVAL",
                      trace["closed_rule_reasons"])
        self.assertIn("APPROVAL_REQUIRED_NOT_APPROVED",
                      trace["closed_rule_reasons"])
        self.assertIn("APPROVAL_STATUS_PENDING", trace["closed_rule_reasons"])

    def test_ea3_question_association_absent(self):
        fx = self.fx
        fx.question("QST-2026-0001", status="OPEN", impact="BLOCKING")
        fx.task(questions=["QST-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        q = self.item(pkg, "QST-2026-0001")
        self.assertEqual(q.load_mode, METADATA_ONLY)
        self.assertEqual(q.metadata["question"], "Why?")
        d = self.diag(pkg, "QUESTION_TASK_ASSOCIATION_ABSENT")
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_ea3_question_association_mismatch(self):
        fx = self.fx
        fx.question("QST-2026-0001", task="TASK-2026-0099", status="OPEN",
                    impact="BLOCKING")
        fx.task(questions=["QST-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        d = self.diag(pkg, "QUESTION_TASK_ASSOCIATION_MISMATCH")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(d.target, "TASK-2026-0099")

    def test_withdrawn_question_reference(self):
        fx = self.fx
        fx.question("QST-2026-0001", task="TASK-2026-0001",
                    status="WITHDRAWN", impact="BLOCKING")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        d = self.diag(pkg, "WITHDRAWN_QUESTION_REFERENCE")
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_answered_question_carries_provenance_not_truth(self):
        fx = self.fx
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="ANSWERED",
                    impact="BLOCKING", answer="Because.",
                    answered_by="human:user",
                    answered_at="2026-01-09T00:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        q = self.item(pkg, "QST-2026-0001")
        self.assertEqual(q.metadata["answer"], "Because.")
        self.assertEqual(q.metadata["answered_by"], "human:user")
        self.assertIn("ANSWER_IS_NOT_TRUTH_OR_APPROVAL", q.annotations)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_ea7_project_wide_approved_decision_discovery_in_task_mode(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="APPROVED")
        fx.decision("DEC-2026-0002", status="PROPOSED")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertIn("PROJECT_APPROVED_UNSCOPED_CANDIDATE",
                      self.item(pkg, "DEC-2026-0001").selection_reasons)
        self.not_selected(pkg, "DEC-2026-0002")

    def test_decision_backlink_loads_full_body(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", task="TASK-2026-0001", status="PROPOSED")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertEqual(d.native_status, "PROPOSED")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_rejected_decision_stays_metadata_unless_explicit(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", task="TASK-2026-0001", status="REJECTED")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(self.item(pkg, "DEC-2026-0001").load_mode,
                         METADATA_ONLY)
        pkg2 = fx.load(task="TASK-2026-0001", select=["DEC-2026-0001"])
        self.assertEqual(self.item(pkg2, "DEC-2026-0001").load_mode, FULL_BODY)

    def test_ea12_exact_fragment_resolution(self):
        fx = self.fx
        fx.investigation(
            "INV-2026-0001", status="CONCLUDED", conclusion_status="SUPPORTED",
            body=("# Inv\n\n### H1 — first\n\nAlpha.\n\n"
                  "#### H1 detail\n\nNested stays.\n\n"
                  "### H2 — second\n\nBeta.\n\n"
                  "## Current conclusion\n\nSupported.\n"))
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        self.assertEqual(pkg.package_status, READY)
        frag = self.item(pkg, "INV-2026-0001", fragment="H1")
        self.assertEqual(frag.load_mode, FRAGMENT)
        self.assertIn("Alpha.", frag.body)
        self.assertIn("Nested stays.", frag.body)
        self.assertNotIn("Beta.", frag.body)
        self.assertNotIn("Supported.", frag.body)

    def test_terminal_investigation_loads_conclusion_fragment(self):
        fx = self.fx
        fx.investigation(
            "INV-2026-0001", status="CONCLUDED", conclusion_status="NO_VALID_CONCLUSION",
            body=("# Inv\n\n### H1\n\nAlpha.\n\n"
                  "## Current conclusion\n\nNo valid conclusion from EVD.\n"))
        fx.task(investigations=["INV-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")
        i = self.item(pkg, "INV-2026-0001")
        self.assertEqual(i.load_mode, FRAGMENT)
        self.assertIn("No valid conclusion from EVD.", i.body)
        self.assertNotIn("Alpha.", i.body)
        self.assertIn("CONCLUSION_FRAGMENT_ONLY", i.annotations)

    def test_ea13_architecture_is_on_demand(self):
        fx = self.fx
        fx.doc("architecture.md", "# Architecture\n\nSynthesis.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertNotIn("architecture.md", pkg.foundations)
        pkg2 = fx.load(task="TASK-2026-0001", include_architecture=True)
        self.assertIn("Synthesis.", pkg2.foundations["architecture.md"])

    def test_current_state_absence_is_non_blocking(self):
        fx = self.fx
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        d = self.diag(pkg, "CURRENT_STATE_ABSENT")
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_ea14_sole_handoff_loads_full_and_is_not_current(self):
        fx = self.fx
        fx.handoff("HND-2026-0001")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        h = self.item(pkg, "HND-2026-0001")
        self.assertEqual(h.load_mode, FULL_BODY)
        self.assertIn("SOLE_CANDIDATE", h.selection_reasons)
        self.assertNotIn("CURRENT", h.annotations)
        self.no_diag(pkg, "MULTIPLE_HANDOFF_CANDIDATES")

    def test_invalid_candidate_timestamp(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-02-01T00:00:00Z")
        fx.handoff("HND-2026-0002", created_at="2026-02-01 09:00:00")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.diag(pkg, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(d.sources, ["HND-2026-0002"])
        self.assertNotIn("RECENT", self.item(pkg, "HND-2026-0002").annotations)
        self.assertNotIn("HISTORICAL",
                         self.item(pkg, "HND-2026-0002").annotations)
        self.assertIn("RECENT", self.item(pkg, "HND-2026-0001").annotations)

    def test_ea16_diagnostic_entry_isolation_invalid_manifest(self):
        for bad in ({"manifest_version": "2.0", "tabularium_ruleset": RULESET},
                    {"manifest_version": "1.0"},
                    {"manifest_version": "1.0", "tabularium_ruleset": RULESET,
                     "extra": "x"},
                    {"manifest_version": "1.0", "tabularium_ruleset": "   "}):
            with self.subTest(bad=bad):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.manifest(bad)
                fx.task()
                pkg = fx.load(task="TASK-2026-0001")
                self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
                self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
                self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
                self.assertEqual(pkg.selected, [])

    def test_manifest_duplicate_key_is_diagnostic_entry(self):
        fx = self.fx
        fx.manifest("manifest_version: \"1.0\"\ntabularium_ruleset: \"0.1.0\"\n"
                    "tabularium_ruleset: \"0.2.0\"\n")
        pkg = fx.load()
        self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
        self.diag(pkg, "MANIFEST_ENTRY_FAILED")

    def test_missing_required_foundation_blocks(self):
        fx = self.fx
        fx.task()
        fx.remove("constraints.md")
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.entry_mode, GOVERNED)
        self.assertEqual(pkg.package_status, BLOCKED)
        d = self.diag(pkg, "REQUIRED_FOUNDATION_UNAVAILABLE")
        self.assertEqual(d.blocking_dimension, PACKAGE)

    def test_ea18_unrelated_duplicate_id_is_warning_only(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001")
        fx.rec("EVD-2026-0001",
               {"schema_version": "1.4", "title": "dup",
                "source_type": "OBSERVATION", "status": "ACTIVE",
                "observation": "x", "collected_at": "2026-01-05T00:00:00Z",
                "collected_by": "ai:claude"}, filename="EVD-2026-0002")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        d = [x for x in pkg.diagnostics if x.code == "RESOLUTION_AMBIGUOUS"]
        self.assertTrue(d)
        self.assertEqual(d[0].blocking_dimension, NEITHER)

    def test_filename_id_mismatch_is_reported_not_repaired(self):
        fx = self.fx
        fx.task()
        fx.rec("EVD-2026-0001",
               {"schema_version": "1.4", "title": "e", "source_type": "OBSERVATION",
                "status": "ACTIVE", "observation": "x",
                "collected_at": "2026-01-05T00:00:00Z",
                "collected_by": "ai:claude"}, filename="EVD-2026-0009")
        pkg = fx.load(task="TASK-2026-0001")
        self.diag(pkg, "FILENAME_ID_MISMATCH")

    def test_malformed_record_is_quarantined(self):
        fx = self.fx
        fx.task()
        (fx.project / ".ai" / "evidence").mkdir(parents=True, exist_ok=True)
        (fx.project / ".ai" / "evidence" / "EVD-2026-0001.md").write_text(
            "no frontmatter here\n", encoding="utf-8")
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        d = self.diag(pkg, "MALFORMED_RECORD_QUARANTINED")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.not_selected(pkg, "EVD-2026-0001")

    def test_explicit_selection_of_missing_record_blocks(self):
        fx = self.fx
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["EVD-2026-0404"])
        self.assertEqual(pkg.package_status, BLOCKED)
        d = self.diag(pkg, "MISSING_REFERENCE")
        self.assertEqual(d.blocking_dimension, PACKAGE)

    def test_explicit_selection_task_mismatch_is_surfaced(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001", task="TASK-2026-0099")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["EVD-2026-0001"])
        self.assertEqual(pkg.package_status, READY)
        d = self.diag(pkg, "EXPLICIT_SELECTION_TASK_MISMATCH")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(self.item(pkg, "EVD-2026-0001").load_mode, FULL_BODY)

    def test_path_shaped_relationship_value_is_an_m1_violation(self):
        """A path in a typed relationship field violates the frozen shape, so
        the record is quarantined rather than dereferenced."""
        fx = self.fx
        fx.task(evidence=["../other/.ai/evidence/EVD-2026-0001.md"])
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, BLOCKED)
        d = self.diag(pkg, "ROOT_TASK_INVALID")
        self.assertEqual(d.blocking_dimension, PACKAGE)
        self.assertIn("canonical Evidence id", d.reason)

    def test_path_shaped_explicit_selection_is_never_followed(self):
        fx = self.fx
        fx.task()
        pkg = fx.load(task="TASK-2026-0001",
                      select=["../other/.ai/evidence/EVD-2026-0001.md"])
        self.assertEqual(pkg.package_status, BLOCKED)
        d = self.diag(pkg, "INVALID_EXPLICIT_SELECTION")
        self.assertEqual(d.blocking_dimension, PACKAGE)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_ea19_repeated_selection_is_represented_once(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001", task="TASK-2026-0001")
        fx.task(evidence=["EVD-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")
        e = self.item(pkg, "EVD-2026-0001")   # asserts exactly one
        self.assertIn("TASK_EVIDENCE", e.selection_reasons)
        self.assertIn("EVIDENCE_TASK_BACKLINK", e.selection_reasons)

    def test_ea19_deterministic_ordering(self):
        fx = self.fx
        fx.evidence("EVD-2026-0002", task="TASK-2026-0001")
        fx.evidence("EVD-2026-0001", task="TASK-2026-0001")
        fx.question("QST-2026-0001", task="TASK-2026-0001")
        fx.decision("DEC-2026-0001", task="TASK-2026-0001")
        fx.investigation("INV-2026-0001", task="TASK-2026-0001")
        fx.handoff("HND-2026-0001")
        fx.report("RPT-2026-0001")
        fx.task()
        d = fx.load(task="TASK-2026-0001").to_dict()
        families = [i["family"] for i in d["selected"]]
        order = ["Task", "Question", "Decision", "Investigation", "Evidence",
                 "Handoff", "Report"]
        self.assertEqual(families, sorted(families, key=order.index))
        evd = [i["id"] for i in d["selected"] if i["family"] == "Evidence"]
        self.assertEqual(evd, sorted(evd))
        cats = [(x["category"], x["code"]) for x in d["diagnostics"]]
        rank = {"DRIFT": 0, "CONFLICT": 1, "AMBIGUITY": 2, "WARNING": 3}
        self.assertEqual(cats, sorted(cats, key=lambda c: (rank[c[0]], c[1])))

    def test_ea19_repeated_runs_are_byte_identical(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", task="TASK-2026-0001")
        fx.evidence("EVD-2026-0001", task="TASK-2026-0001")
        fx.handoff("HND-2026-0001")
        fx.task(decisions=["DEC-2026-0001"])
        a = fx.load(task="TASK-2026-0001").to_json()
        b = fx.load(task="TASK-2026-0001").to_json()
        self.assertEqual(a, b)
        json.loads(a)

    def test_ea21_loader_writes_nothing(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", task="TASK-2026-0001")
        fx.evidence("EVD-2026-0001", status="RETRACTED",
                    task="TASK-2026-0001")
        fx.investigation("INV-2026-0001", task="TASK-2026-0001",
                         supporting_evidence=["EVD-2026-0001"])
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="OPEN",
                    impact="BLOCKING")
        fx.handoff("HND-2026-0001")
        fx.report("RPT-2026-0001")
        fx.doc("current-state.md", "- TASK-2026-0001 is COMPLETED\n")
        fx.doc("architecture.md", "# A\n")
        fx.task(decisions=["DEC-2026-0001"], questions=["QST-2026-0404"])

        before = fx.snapshot()
        fx.load(task="TASK-2026-0001", include_architecture=True,
                select=["INV-2026-0001#H1"])
        fx.load()
        fx.load(task="TASK-2026-0404")
        after = fx.snapshot()
        self.assertEqual(before, after,
                         "loader modified the project (content, size, or mtime)")

    def test_no_durable_package_or_ctx_artifact_is_created(self):
        fx = self.fx
        fx.task()
        fx.load(task="TASK-2026-0001")
        names = [p.name for p in fx.project.rglob("*")]
        for forbidden in ("CTX", "context-package.json", ".cache", "index.db"):
            self.assertFalse([n for n in names if forbidden in n],
                             f"unexpected artifact matching {forbidden}")

    def test_project_root_without_ai_dir_is_diagnostic(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctxload-bare-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pkg = load_context(tmp, ai_os_root=self.fx.ai_os_root)
        self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
        self.assertEqual(pkg.entry_mode, DIAGNOSTIC)

    def test_unresolved_root_task_blocks(self):
        fx = self.fx
        fx.task()
        pkg = fx.load(task="TASK-2026-0404")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        self.diag(pkg, "ROOT_TASK_UNRESOLVED")
        self.assertTrue(pkg.assumptions)
        self.assertEqual(pkg.assumptions[0]["blocking_dimension"],
                         TASK_ACTIONABILITY)

    def test_constraints_citation_selects_decision_body(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="APPROVED")
        fx.doc("constraints.md",
               "# Constraints\n\n- Rule X, per DEC-2026-0001.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertIn("CONSTRAINTS_CITATION", d.selection_reasons)
        # citation is a selection basis only, never Task actionability
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_architecture_citing_non_effective_decision_is_stale_suspected(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="REJECTED")
        fx.doc("architecture.md", "# Architecture\n\nBuilt per DEC-2026-0001.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.no_diag(pkg, "STALE-SUSPECTED")   # not loaded, not cited
        pkg2 = fx.load(task="TASK-2026-0001", include_architecture=True)
        d = self.diag(pkg2, "STALE-SUSPECTED")
        self.assertEqual(d.target, "DEC-2026-0001")
        self.assertEqual(d.blocking_dimension, NEITHER)
        self.assertEqual(pkg2.task_actionability, ACTIONABLE)

    def test_decision_body_citations_are_wave2_allowlisted(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001")
        fx.evidence("EVD-2026-0002")
        fx.decision(
            "DEC-2026-0001", task="TASK-2026-0001",
            body=("# Decision\n\n## Rationale\n\nSee EVD-2026-0002.\n\n"
                  "## Alternatives\n\nRejected per EVD-2026-0001.\n"))
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        cited = self.item(pkg, "EVD-2026-0001")
        self.assertIn("DECISION_BODY_CITATION", cited.selection_reasons)
        # a citation outside the two designated sections is not followed
        self.not_selected(pkg, "EVD-2026-0002")


class TestDecisionCitationScope(LoaderTestCase):
    """Architect correction 1: constraints citations are bounded at project
    entry; architecture citations are not, because loading it was requested."""

    def setUp(self) -> None:
        super().setUp()
        self.fx.decision("DEC-2026-0001", status="APPROVED")
        self.fx.task()

    def test_project_entry_constraints_citation_is_metadata_only(self):
        fx = self.fx
        fx.doc("constraints.md", "# Constraints\n\n- Rule per DEC-2026-0001.\n")
        pkg = fx.load()
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, METADATA_ONLY)
        self.assertIsNone(d.body)
        self.assertIn("CONSTRAINTS_CITATION", d.selection_reasons)
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)

    def test_task_entry_constraints_citation_is_full_body(self):
        fx = self.fx
        fx.doc("constraints.md", "# Constraints\n\n- Rule per DEC-2026-0001.\n")
        pkg = fx.load(task="TASK-2026-0001")
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertIsNotNone(d.body)
        self.assertIn("CONSTRAINTS_CITATION", d.selection_reasons)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_project_entry_architecture_citation_is_full_body(self):
        fx = self.fx
        fx.doc("architecture.md", "# Architecture\n\nPer DEC-2026-0001.\n")
        pkg = fx.load(include_architecture=True)
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertIn("ARCHITECTURE_CITATION", d.selection_reasons)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)

    def test_task_entry_architecture_citation_is_full_body(self):
        fx = self.fx
        fx.doc("architecture.md", "# Architecture\n\nPer DEC-2026-0001.\n")
        pkg = fx.load(task="TASK-2026-0001", include_architecture=True)
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertIn("ARCHITECTURE_CITATION", d.selection_reasons)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_unloaded_architecture_citation_is_not_a_basis(self):
        fx = self.fx
        fx.doc("architecture.md", "# Architecture\n\nPer DEC-2026-0001.\n")
        pkg = fx.load()   # architecture not requested
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, METADATA_ONLY)
        self.assertNotIn("ARCHITECTURE_CITATION", d.selection_reasons)


class TestTimestampRecency(LoaderTestCase):
    """Architect correction 2: recency compares parsed absolute instants."""

    def recent_id(self, pkg, family_prefix):
        hits = [i.record_id for i in pkg.selected
                if i.record_id.startswith(family_prefix)
                and "RECENT" in i.annotations]
        self.assertLessEqual(len(hits), 1, "RECENT must be single per family")
        return hits[0] if hits else None

    def test_instants_sort_chronologically_not_lexically(self):
        fx = self.fx
        # 09:00+05:00 is 04:00Z, i.e. EARLIER than 05:00Z, but sorts later
        # lexically. The newest instant must win.
        fx.handoff("HND-2026-0001", created_at="2026-03-01T09:00:00+05:00")
        fx.handoff("HND-2026-0002", created_at="2026-03-01T05:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(self.recent_id(pkg, "HND"), "HND-2026-0002")
        self.assertIn("HISTORICAL",
                      self.item(pkg, "HND-2026-0001").annotations)

    def test_equivalent_instants_tie_break_by_id_not_offset_text(self):
        fx = self.fx
        # identical absolute instants written with different offsets
        fx.report("RPT-2026-0001", created_at="2026-03-01T14:00:00+02:00")
        fx.report("RPT-2026-0002", created_at="2026-03-01T12:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(self.recent_id(pkg, "RPT"), "RPT-2026-0001")
        self.no_diag(pkg, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP")

    def test_timezone_naive_timestamp_is_not_comparable(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-03-01T09:00:00")
        fx.handoff("HND-2026-0002", created_at="2026-01-01T00:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.diag(pkg, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP")
        self.assertEqual(d.sources, ["HND-2026-0001"])
        self.assertEqual(d.field_or_fragment, "created_at")
        self.assertEqual(d.blocking_dimension, NEITHER)
        naive = self.item(pkg, "HND-2026-0001")
        self.assertNotIn("RECENT", naive.annotations)
        self.assertNotIn("HISTORICAL", naive.annotations)
        # the comparable candidate is still usable for navigation
        self.assertEqual(self.recent_id(pkg, "HND"), "HND-2026-0002")

    def test_malformed_timestamp_warns_and_falls_back_to_id_order(self):
        fx = self.fx
        fx.handoff("HND-2026-0002", created_at="not-a-timestamp")
        fx.handoff("HND-2026-0001", created_at="2026-13-45T99:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        codes = [d for d in pkg.diagnostics
                 if d.code == "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP"]
        self.assertEqual(sorted(x.sources[0] for x in codes),
                         ["HND-2026-0001", "HND-2026-0002"])
        self.assertIsNone(self.recent_id(pkg, "HND"))
        for rid in ("HND-2026-0001", "HND-2026-0002"):
            self.assertNotIn("HISTORICAL", self.item(pkg, rid).annotations)
        self.assertEqual(fx.load(task="TASK-2026-0001").to_json(),
                         pkg.to_json())

    def test_comparable_candidates_sort_before_non_comparable(self):
        fx = self.fx
        # non-comparable candidate holds the lexically first id
        fx.handoff("HND-2026-0001", created_at="2026-02-01 09:00:00")
        fx.handoff("HND-2026-0002", created_at="2026-01-01T00:00:00Z")
        fx.handoff("HND-2026-0003", created_at="2026-02-01T00:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(self.recent_id(pkg, "HND"), "HND-2026-0003")
        self.assertIn("HISTORICAL",
                      self.item(pkg, "HND-2026-0002").annotations)
        self.assertNotIn("HISTORICAL",
                         self.item(pkg, "HND-2026-0001").annotations)

    def test_selected_handoff_historical_requires_two_comparable_instants(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-01-01T00:00:00Z")
        fx.task(updated_at="2026-06-01T00:00:00Z")
        pkg = fx.load(task="TASK-2026-0001")
        h = self.item(pkg, "HND-2026-0001")
        self.assertEqual(h.load_mode, FULL_BODY)
        self.assertIn("HISTORICAL", h.annotations)
        self.no_diag(pkg, "STALE-SUSPECTED")

    def test_selected_handoff_not_historical_when_task_is_older(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-06-01T00:00:00Z")
        fx.task(updated_at="2026-01-01T00:00:00Z")
        pkg = fx.load(task="TASK-2026-0001")
        self.assertNotIn("HISTORICAL",
                         self.item(pkg, "HND-2026-0001").annotations)

    def test_non_comparable_task_updated_at_infers_nothing_from_time(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-01-01T00:00:00Z")
        fx.report("RPT-2026-0001", created_at="2026-01-01T00:00:00Z")
        fx.task(updated_at="2026-06-01 00:00:00")   # timezone-naive
        pkg = fx.load(task="TASK-2026-0001")
        for rid in ("HND-2026-0001", "RPT-2026-0001"):
            self.assertNotIn("HISTORICAL", self.item(pkg, rid).annotations)
        self.no_diag(pkg, "STALE-SUSPECTED")
        task_diags = [d for d in pkg.diagnostics
                      if d.code == "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP"
                      and d.field_or_fragment == "updated_at"]
        self.assertEqual(len(task_diags), 1, "warn once, deterministically")
        self.assertEqual(task_diags[0].semantic_owner, "Task")
        self.assertEqual(task_diags[0].blocking_dimension, NEITHER)

    def test_report_recency_uses_the_same_rules(self):
        fx = self.fx
        fx.report("RPT-2026-0001", created_at="2026-03-01T09:00:00+05:00")
        fx.report("RPT-2026-0002", created_at="2026-03-01T05:00:00Z")
        fx.report("RPT-2026-0003", created_at="2026-03-01T09:00:00")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(self.recent_id(pkg, "RPT"), "RPT-2026-0002")
        d = self.diag(pkg, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP")
        self.assertEqual(d.sources, ["RPT-2026-0003"])
        # Reports carry no authority to lose
        for i in pkg.selected:
            self.assertNotIn("CURRENT", i.annotations)

    def test_mixed_timestamp_runs_remain_byte_identical(self):
        fx = self.fx
        fx.handoff("HND-2026-0001", created_at="2026-03-01T09:00:00+05:00")
        fx.handoff("HND-2026-0002", created_at="2026-03-01T05:00:00Z")
        fx.handoff("HND-2026-0003", created_at="oops")
        fx.report("RPT-2026-0001", created_at="2026-03-01T12:00:00Z")
        fx.report("RPT-2026-0002", created_at="2026-03-01T14:00:00+02:00")
        fx.task(updated_at="2026-04-01T00:00:00Z")
        a = fx.load(task="TASK-2026-0001").to_json()
        b = fx.load(task="TASK-2026-0001").to_json()
        self.assertEqual(a, b)
        json.loads(a)

    def test_instant_parser_accepts_only_aware_iso8601(self):
        from tabularium.engine.context_loader.loader import Loader
        for good in ("2026-03-01T05:00:00Z", "2026-03-01T05:00:00z",
                     "2026-03-01T09:00:00+05:00", "2026-03-01T09:00:00-08:00",
                     "2026-03-01T05:00:00+00:00", "2026-03-01T05:00:00 UTC"):
            self.assertIsNotNone(Loader.instant(good), good)
        for bad in ("2026-03-01T05:00:00", "2026-03-01", "", None, 17,
                    "not-a-timestamp", "2026-13-45T99:00:00Z", "   ",
                    " 2026-03-01T05:00:00Z "):
            self.assertIsNone(Loader.instant(bad), repr(bad))

    def test_equivalent_offsets_parse_to_one_instant(self):
        from tabularium.engine.context_loader.loader import Loader
        self.assertEqual(Loader.instant("2026-03-01T12:00:00Z"),
                         Loader.instant("2026-03-01T14:00:00+02:00"))


class TestCli(LoaderTestCase):

    def test_cli_emits_json_and_status_exit_codes(self):
        from tabularium.engine.context_loader.cli import main
        import contextlib
        import io

        fx = self.fx
        fx.task()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["--project", str(fx.project),
                       "--task", "TASK-2026-0001",
                       "--ai-os-root", str(fx.ai_os_root)])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["package_status"], READY)
        self.assertEqual(data["task_actionability"], ACTIONABLE)

        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            rc2 = main(["--project", str(fx.project),
                        "--task", "TASK-2026-0001",
                        "--select", "EVD-2026-0404",
                        "--ai-os-root", str(fx.ai_os_root), "--compact"])
        self.assertEqual(rc2, 1)
        self.assertEqual(json.loads(buf2.getvalue())["package_status"], BLOCKED)


class TestDotPrefixedDiscovery(unittest.TestCase):
    """D4: dot-prefixed entries, non-.md files, and README.md are never
    records; visible misnamed .md files stay quarantined and diagnosable."""

    ARTIFACTS = (
        ".TASK-2026-0001.md",          # hidden copy of a canonical name
        ".#TASK-2026-0001.md",         # editor lock
        "TASK-2026-0001.md~",          # editor backup (non-.md)
        ".DS_Store",                   # OS metadata
        "notes.txt",                   # unrelated non-record
    )

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)
        self.fx.task("TASK-2026-0002")
        tasks = self.fx.project / ".ai" / "tasks"
        for name in self.ARTIFACTS:
            (tasks / name).write_text("---\nid: TASK-2026-9999\n---\n",
                                      encoding="utf-8")

    def test_artifacts_never_become_records_or_diagnostics(self):
        pkg = self.fx.load(task="TASK-2026-0002")
        self.assertEqual(pkg.package_status, READY)
        self.assertNotIn("MALFORMED_RECORD_QUARANTINED",
                         [d.code for d in pkg.diagnostics])
        self.assertNotIn("INVALID_RECORD_QUARANTINED",
                         [d.code for d in pkg.diagnostics])
        selected = [i.record_id for i in pkg.selected]
        self.assertIn("TASK-2026-0002", selected)
        for hidden in ("TASK-2026-9999",):
            self.assertNotIn(hidden, selected)

    def test_discovery_inventory_counts_only_real_candidates(self):
        repo = discover(self.fx.project, ProjectScope(self.fx.project))
        task_records = repo.family_records("Task")
        self.assertEqual([r.filename_id for r in task_records],
                         ["TASK-2026-0002"])
        self.assertEqual(repo.unreadable_dirs, [])

    def test_misnamed_visible_markdown_stays_quarantined(self):
        (self.fx.project / ".ai" / "tasks" /
         "draft-task.md").write_text("# not a record\n", encoding="utf-8")
        pkg = self.fx.load(task="TASK-2026-0002")
        d = [x for x in pkg.diagnostics if x.code == "MALFORMED_RECORD_QUARANTINED"]
        self.assertTrue(d)
        self.assertTrue(any("draft-task" in str(x.sources) or
                            "draft-task" in x.reason for x in d))


class TestUnreadableRecordDirectory(unittest.TestCase):
    """B6-02: an existing but unlistable family directory is evidence of an
    access failure, never silently-empty enumeration."""

    def setUp(self) -> None:
        self.fx = Fixture()
        self.addCleanup(self.fx.cleanup)

    @contextmanager
    def denied(self, rel: str):
        real_listdir = os.listdir
        target = str(self.fx.project / ".ai" / rel)

        def selective(path, *args, **kwargs):
            if str(path) == target:
                raise PermissionError(13, "Permission denied")
            return real_listdir(path, *args, **kwargs)

        with mock.patch("os.listdir", selective):
            yield

    def test_discovery_records_unreadable_directory_and_other_families(self):
        from tabularium.engine.context_loader.confinement import ProjectScope
        from tabularium.engine.context_loader.discovery import discover
        self.fx.task("TASK-2026-0001", status="INBOX")
        self.fx.decision("DEC-2026-0001")
        with self.denied("tasks"):
            repo = discover(self.fx.project, ProjectScope(self.fx.project))
        self.assertEqual([tuple(entry) for entry in repo.unreadable_dirs],
                         [("Task", ".ai/tasks")])
        self.assertEqual(repo.family_records("Task"), [])
        self.assertEqual(len(repo.family_records("Decision")), 1)

    def test_absent_family_directory_is_not_unreadable_evidence(self):
        from tabularium.engine.context_loader.confinement import ProjectScope
        from tabularium.engine.context_loader.discovery import discover
        self.fx.decision("DEC-2026-0001")
        repo = discover(self.fx.project, ProjectScope(self.fx.project))
        self.assertEqual(repo.unreadable_dirs, [])
        self.assertEqual(repo.family_records("Task"), [])

    def test_loader_emits_warning_and_stays_governed(self):
        self.fx.task("TASK-2026-0001", status="INBOX")
        with self.denied("tasks"):
            pkg = self.fx.load()
        unreadable = [d for d in pkg.diagnostics
                      if d.code == "RECORD_DIRECTORY_UNREADABLE"]
        self.assertEqual(len(unreadable), 1)
        self.assertEqual(unreadable[0].semantic_owner, "Task")
        self.assertEqual(unreadable[0].sources, [".ai/tasks"])
        self.assertNotIn("[Errno", unreadable[0].reason)
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.entry_mode, GOVERNED)


if __name__ == "__main__":
    unittest.main(verbosity=2)

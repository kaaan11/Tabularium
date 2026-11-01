"""Adversarial regressions for the M2.4 findings F01-F09.

These target externally observable Context Package behavior rather than
implementation helpers, except where the confinement predicate itself is the
security boundary under test.

Run:  python -m unittest discover -s tests -t .
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tabularium.engine.context_loader import load_context  # noqa: E402
from tabularium.engine.context_loader.confinement import ProjectScope  # noqa: E402
from tabularium.engine.context_loader.discovery import (  # noqa: E402
    FRAG_AMBIGUOUS, FRAG_MISSING, FRAG_OK, find_hypothesis_fragment,
    iter_headings)
from tabularium.engine.context_loader.manifest import validate as validate_manifest  # noqa: E402
from tabularium.engine.context_loader.model import (  # noqa: E402
    ACTIONABLE, AMBIGUITY, BLOCKED, CONFLICT, DIAGNOSTIC, DIAGNOSTIC_ONLY,
    FULL_BODY, GOVERNED, METADATA_ONLY, NEITHER, NOT_ACTIONABLE,
    NOT_APPLICABLE, PACKAGE, READY, WARNING)
from tests.test_context_loader import Fixture, LoaderTestCase  # noqa: E402

RULESET = "0.1.0"


def make_dir_link(link: Path, target: Path) -> bool:
    """Create a directory symlink or NTFS junction. False when unavailable."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return link.exists()
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        try:
            r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link),
                                str(target)], capture_output=True, timeout=30)
            return r.returncode == 0 and link.exists()
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def remove_dir_link(link: Path) -> None:
    """Remove a directory link portably.

    POSIX symlinks must be unlinked; os.rmdir on a symlink raises
    NotADirectoryError. Windows junctions are removed with rmdir.
    """
    if link.is_symlink():
        link.unlink()
    else:
        link.rmdir()


def make_file_link(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target)
        return link.exists()
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        try:
            r = subprocess.run(["cmd", "/c", "mklink", "/H", str(link),
                                str(target)], capture_output=True, timeout=30)
            # a hard link is indistinguishable from the real file and is not a
            # confinement escape, so only report success for true reparse links
            return False if r.returncode == 0 else False
        except (OSError, subprocess.SubprocessError):
            return False
    return False


# --- F01 project confinement ------------------------------------------------

class TestF01Confinement(LoaderTestCase):

    def other_project(self) -> Fixture:
        other = Fixture()
        self.addCleanup(other.cleanup)
        other.task("TASK-2026-0777", title="Foreign")
        other.evidence("EVD-2026-0777")
        return other

    # -- the containment predicate itself (always executable) -------------

    def test_scope_root_and_true_descendants_are_contained(self):
        fx = self.fx
        fx.task()
        scope = ProjectScope(fx.project)
        self.assertTrue(scope.contains(fx.project / ".ai"))
        self.assertTrue(scope.contains(fx.project / ".ai" / "tasks"))
        self.assertTrue(scope.contains(
            fx.project / ".ai" / "tasks" / "TASK-2026-0001.md"))
        self.assertFalse(scope.contains(fx.project))   # the parent is outside

    def test_path_prefix_trap_is_rejected(self):
        """`.ai-evil` shares a string prefix with `.ai` but is not inside it."""
        fx = self.fx
        evil = fx.project / ".ai-evil" / "tasks"
        evil.mkdir(parents=True)
        (evil / "TASK-2026-0001.md").write_text("x", encoding="utf-8")
        scope = ProjectScope(fx.project)
        self.assertFalse(scope.contains(evil))
        self.assertFalse(scope.contains(evil / "TASK-2026-0001.md"))

    def test_dotdot_traversal_is_rejected(self):
        fx = self.fx
        outside = fx.tmp / "outside"
        outside.mkdir()
        (outside / "x.md").write_text("x", encoding="utf-8")
        scope = ProjectScope(fx.project)
        self.assertFalse(scope.contains(fx.project / ".ai" / ".." / ".."
                                        / "outside" / "x.md"))

    def test_absolute_external_path_is_rejected(self):
        other = self.other_project()
        scope = ProjectScope(self.fx.project)
        self.assertFalse(scope.contains(other.project / ".ai" / "tasks"))
        self.assertFalse(scope.contains(
            other.project / ".ai" / "tasks" / "TASK-2026-0777.md"))

    def test_missing_ai_root_is_never_contained(self):
        tmp = Path(tempfile.mkdtemp(prefix="ctx-bare-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        scope = ProjectScope(tmp)
        self.assertFalse(scope.available)
        self.assertFalse(scope.contains(tmp / ".ai" / "tasks"))

    # -- link-mediated escapes (platform permitting) ----------------------

    def test_family_directory_junction_escape_is_refused(self):
        fx, other = self.fx, self.other_project()
        (fx.project / ".ai" / "tasks").mkdir(parents=True, exist_ok=True)
        shutil.rmtree(fx.project / ".ai" / "tasks")
        if not make_dir_link(fx.project / ".ai" / "tasks",
                             other.project / ".ai" / "tasks"):
            self.skipTest("directory links unavailable on this platform")
        pkg = fx.load(task="TASK-2026-0777")
        self.not_selected(pkg, "TASK-2026-0777")
        d = self.diag(pkg, "OUT_OF_SCOPE_REFERENCE")
        self.assertEqual(d.target, ".ai/tasks")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "ROOT_TASK_UNRESOLVED")

    def test_record_file_symlink_escape_is_refused(self):
        fx, other = self.fx, self.other_project()
        fx.task()
        d = fx.project / ".ai" / "evidence"
        d.mkdir(parents=True, exist_ok=True)
        link = d / "EVD-2026-0777.md"
        try:
            link.symlink_to(other.project / ".ai" / "evidence"
                            / "EVD-2026-0777.md")
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks unavailable on this platform")
        pkg = fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "EVD-2026-0777")
        hits = [x for x in pkg.diagnostics
                if x.code == "OUT_OF_SCOPE_REFERENCE"]
        self.assertTrue(hits)
        self.assertEqual(pkg.package_status, READY)

    def test_manifest_junction_escape_stays_diagnostic_only(self):
        fx, other = self.fx, self.other_project()
        ai = fx.project / ".ai"
        (ai / "manifest.yaml").unlink()
        try:
            (ai / "manifest.yaml").symlink_to(
                other.project / ".ai" / "manifest.yaml")
        except (OSError, NotImplementedError):
            self.skipTest("file symlinks unavailable on this platform")
        pkg = fx.load()
        self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
        self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
        self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
        self.assertEqual(pkg.selected, [])

    def test_second_project_ai_root_is_never_reachable(self):
        fx, other = self.fx, self.other_project()
        fx.task()
        pkg = fx.load(task="TASK-2026-0001",
                      select=["TASK-2026-0777"])
        self.assertEqual(pkg.package_status, BLOCKED)
        self.not_selected(pkg, "TASK-2026-0777")
        self.diag(pkg, "MISSING_REFERENCE")

    def test_entire_ai_junction_to_foreign_project_is_refused(self):
        fx, other = self.fx, self.other_project()
        ai = fx.project / ".ai"
        shutil.rmtree(ai)
        if not make_dir_link(ai, other.project / ".ai"):
            self.skipTest("directory links unavailable on this platform")
        try:
            before_caller = fx.snapshot()
            before_foreign = other.snapshot()
            pkg = fx.load(task="TASK-2026-0777")
            self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
            self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
            self.assertEqual(pkg.selected, [])
            self.diag(pkg, "PROJECT_SCOPE_UNAVAILABLE")
            self.assertEqual(fx.snapshot(), before_caller)
            self.assertEqual(other.snapshot(), before_foreign)
        finally:
            remove_dir_link(ai)

    def test_entire_ai_directory_symlink_is_refused_when_available(self):
        fx, other = self.fx, self.other_project()
        ai = fx.project / ".ai"
        shutil.rmtree(ai)
        try:
            ai.symlink_to(other.project / ".ai", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlinks unavailable on this platform")
        try:
            pkg = fx.load(task="TASK-2026-0777")
            self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
            self.assertEqual(pkg.selected, [])
        finally:
            ai.unlink()

    def test_ai_redirected_to_nested_project_directory_is_refused(self):
        fx = self.fx
        nested = fx.project / "foreign" / ".ai"
        nested.parent.mkdir()
        shutil.move(str(fx.project / ".ai"), str(nested))
        ai = fx.project / ".ai"
        if not make_dir_link(ai, nested):
            self.skipTest("directory links unavailable on this platform")
        try:
            pkg = fx.load()
            self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
            self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
            self.assertEqual(pkg.selected, [])
        finally:
            remove_dir_link(ai)

    def test_real_direct_child_ai_is_governed(self):
        self.fx.task()
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.entry_mode, GOVERNED)
        self.assertEqual(pkg.package_status, READY)

    def test_caller_project_root_alias_preserves_direct_ai_anchor(self):
        fx = self.fx
        fx.task()
        alias = fx.tmp / "project-alias"
        if not make_dir_link(alias, fx.project):
            self.skipTest("directory links unavailable on this platform")
        try:
            pkg = load_context(alias, task="TASK-2026-0001",
                               ai_os_root=fx.ai_os_root)
            self.assertEqual(pkg.entry_mode, GOVERNED)
            self.assertEqual(pkg.package_status, READY)
        finally:
            remove_dir_link(alias)


# --- F02 manifest validation ------------------------------------------------

class TestF02Manifest(LoaderTestCase):

    def bad(self, text: str) -> None:
        manifest, reason = validate_manifest(text)
        self.assertIsNone(manifest, f"expected rejection for: {text!r}")
        self.assertTrue(reason)

    def test_valid_manifest_is_accepted(self):
        manifest, reason = validate_manifest(
            'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\ntitle: "P"\n')
        self.assertIsNone(reason)
        self.assertEqual(manifest["tabularium_ruleset"], "0.1.0")

    def test_title_is_optional(self):
        manifest, reason = validate_manifest(
            'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n')
        self.assertIsNone(reason)
        self.assertNotIn("title", manifest)

    def test_rejected_shapes(self):
        cases = {
            "nested title": 'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n'
                            'title:\n  nested: "mapping"\n',
            "sequence title": 'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n'
                              'title:\n  - "a"\n',
            "empty title": 'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n'
                           'title: ""\n',
            "unquoted ruleset": 'manifest_version: "1.0"\ntabularium_ruleset: 0.1.0\n',
            "unquoted version": 'manifest_version: 1.0\ntabularium_ruleset: "0.1.0"\n',
            "numeric value": 'manifest_version: "1.0"\ntabularium_ruleset: 7\n',
            "boolean value": 'manifest_version: "1.0"\ntabularium_ruleset: true\n',
            "null value": 'manifest_version: "1.0"\ntabularium_ruleset:\n',
            "duplicate key": 'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n'
                             'tabularium_ruleset: "0.2.0"\n',
            "unknown key": 'manifest_version: "1.0"\ntabularium_ruleset: "0.1.0"\n'
                           'extra: "x"\n',
            "list root": '- "a"\n- "b"\n',
            "scalar root": '"just a string"\n',
            "empty document": "",
            "malformed yaml": 'manifest_version: "1.0"\n  bad indent: [\n',
            "unsupported version": 'manifest_version: "2.0"\n'
                                   'tabularium_ruleset: "0.1.0"\n',
            "whitespace only": 'manifest_version: "1.0"\ntabularium_ruleset: "   "\n',
            "nested configuration": 'manifest_version: "1.0"\n'
                                    'tabularium_ruleset: "0.1.0"\n'
                                    'title:\n  a:\n    b: "c"\n',
        }
        for label, text in cases.items():
            with self.subTest(case=label):
                self.bad(text)

    def test_unsafe_python_tag_never_constructs_an_object(self):
        text = ('manifest_version: "1.0"\n'
                'tabularium_ruleset: !!python/object/apply:os.system ["echo pwned"]\n')
        manifest, reason = validate_manifest(text)
        self.assertIsNone(manifest)
        self.assertTrue(reason)

    def test_alias_to_a_quoted_scalar_is_still_type_checked(self):
        good = ('manifest_version: &v "1.0"\ntabularium_ruleset: "0.1.0"\n'
                'title: *v\n')
        manifest, reason = validate_manifest(good)
        self.assertIsNone(reason, reason)
        self.assertEqual(manifest["title"], "1.0")
        self.bad('manifest_version: "1.0"\ntabularium_ruleset: &r\n  a: "b"\n'
                 'title: *r\n')

    def test_malformed_manifest_never_reaches_governed_mode(self):
        for text in ('manifest_version: "1.0"\ntabularium_ruleset: 0.1.0\n',
                     'title:\n  nested: "mapping"\n',
                     '- "a"\n'):
            with self.subTest(text=text):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.manifest(text)
                fx.task()
                pkg = fx.load(task="TASK-2026-0001")
                self.assertEqual(pkg.entry_mode, DIAGNOSTIC)
                self.assertEqual(pkg.package_status, DIAGNOSTIC_ONLY)
                self.assertEqual(pkg.task_actionability, NOT_APPLICABLE)
                self.assertEqual(pkg.selected, [])
                self.diag(pkg, "MANIFEST_ENTRY_FAILED")


# --- F03 consumed M1 validation --------------------------------------------

class TestF03ConsumedValidation(LoaderTestCase):

    def test_corrected_baseline_fixture_records_are_frozen_valid(self):
        fx = self.fx
        fx.question("QST-2026-0001")
        fx.decision("DEC-2026-0001")
        fx.investigation("INV-2026-0001")
        fx.evidence("EVD-2026-0001")
        fx.handoff("HND-2026-0001")
        fx.report("RPT-2026-0001")
        fx.task()
        selected = ["QST-2026-0001", "DEC-2026-0001",
                    "INV-2026-0001", "EVD-2026-0001",
                    "HND-2026-0001", "RPT-2026-0001"]
        pkg = fx.load(task="TASK-2026-0001", select=selected)
        self.assertEqual(pkg.package_status, READY)
        self.assertNotIn("INVALID_RECORD_QUARANTINED", self.codes(pkg))
        for rid in selected:
            self.assertIsNotNone(self.item(pkg, rid))

    def test_approval_required_without_status_is_ready_not_actionable(self):
        self.fx.task(approval_required=True)
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)

    def test_invalid_task_created_by_blocks_root(self):
        self.fx.task(created_by="human:user")
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "ROOT_TASK_INVALID")

    def test_genuine_task_approval_validation_remains_enforced(self):
        for fields in ({"approval_status": "UNKNOWN"},
                       {"approval_status": "APPROVED"},
                       {"approval_status": "PENDING",
                        "approved_by": "reviewer",
                        "approved_at": "2026-01-01T00:00:00Z"}):
            with self.subTest(fields=fields):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.task(**fields)
                pkg = fx.load(task="TASK-2026-0001")
                self.assertEqual(pkg.package_status, BLOCKED)
                self.diag(pkg, "ROOT_TASK_INVALID")

    def test_abandoned_investigation_requires_no_valid_conclusion(self):
        for value in (None, "SUPPORTED", "INCONCLUSIVE"):
            with self.subTest(conclusion_status=value):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fields = {"status": "ABANDONED", "task": "TASK-2026-0001"}
                if value is not None:
                    fields["conclusion_status"] = value
                fx.investigation("INV-2026-0001", **fields)
                fx.task(investigations=["INV-2026-0001"])
                pkg = fx.load(task="TASK-2026-0001")
                self.not_selected(pkg, "INV-2026-0001")
                self.diag(pkg, "INVALID_RECORD_QUARANTINED")
                required = fx.load(task="TASK-2026-0001",
                                   select=["INV-2026-0001"])
                self.assertEqual(required.package_status, BLOCKED)

    def test_abandoned_investigation_accepts_required_conclusion(self):
        self.fx.investigation("INV-2026-0001", status="ABANDONED",
                              conclusion_status="NO_VALID_CONCLUSION",
                              task="TASK-2026-0001")
        self.fx.task(investigations=["INV-2026-0001"])
        pkg = self.fx.load(task="TASK-2026-0001")
        self.assertIsNotNone(self.item(pkg, "INV-2026-0001"))

    def test_decision_superseded_by_lifecycle_conditional(self):
        cases = (("SUPERSEDED", None),
                 ("APPROVED", ["DEC-2026-0002"]))
        for status, superseded_by in cases:
            with self.subTest(status=status):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fields = {"status": status, "task": "TASK-2026-0001"}
                if superseded_by is not None:
                    fields["superseded_by"] = superseded_by
                fx.decision("DEC-2026-0001", **fields)
                fx.task(decisions=["DEC-2026-0001"])
                pkg = fx.load(task="TASK-2026-0001")
                self.not_selected(pkg, "DEC-2026-0001")
                self.diag(pkg, "INVALID_RECORD_QUARANTINED")
                required = fx.load(task="TASK-2026-0001",
                                   select=["DEC-2026-0001"])
                self.assertEqual(required.package_status, BLOCKED)

    def test_invalid_handoff_body_is_quarantined_and_explicitly_blocks(self):
        for body in ("", "# H\n\n## Current State\n\nOnly one.\n"):
            with self.subTest(body=body):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.handoff("HND-2026-0001", body=body)
                fx.task()
                candidate = fx.load(task="TASK-2026-0001")
                self.not_selected(candidate, "HND-2026-0001")
                required = fx.load(task="TASK-2026-0001",
                                   select=["HND-2026-0001"])
                self.assertEqual(required.package_status, BLOCKED)

    def test_invalid_report_body_is_quarantined_and_explicitly_blocks(self):
        for body in ("", "# R\n\n## Outcome\n\nOnly one.\n"):
            with self.subTest(body=body):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.report("RPT-2026-0001", body=body)
                fx.task()
                candidate = fx.load(task="TASK-2026-0001")
                self.not_selected(candidate, "RPT-2026-0001")
                required = fx.load(task="TASK-2026-0001",
                                   select=["RPT-2026-0001"])
                self.assertEqual(required.package_status, BLOCKED)

    def test_unsupported_task_version_blocks_the_root(self):
        fx = self.fx
        fx.task(schema_version="9.9")
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.assertEqual(pkg.task_actionability, NOT_ACTIONABLE)
        d = self.diag(pkg, "ROOT_TASK_INVALID")
        self.assertEqual(d.blocking_dimension, PACKAGE)
        self.assertIn("9.9", d.reason)

    def test_invalid_question_cannot_block_actionability(self):
        """The ninth actionability condition needs a VALID Question."""
        fx = self.fx
        fx.question("QST-2026-0001", schema_version="9.9",
                    task="TASK-2026-0001", status="OPEN", impact="BLOCKING")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, READY)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)
        self.not_selected(pkg, "QST-2026-0001")
        d = self.diag(pkg, "INVALID_RECORD_QUARANTINED")
        self.assertEqual(d.blocking_dimension, NEITHER)

    def test_question_with_bad_impact_cannot_block(self):
        fx = self.fx
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="OPEN",
                    impact="VERY_BLOCKING")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_answered_question_missing_answer_unit_is_quarantined(self):
        fx = self.fx
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="ANSWERED",
                    impact="BLOCKING", answer="Because.")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "QST-2026-0001")
        self.diag(pkg, "INVALID_RECORD_QUARANTINED")

    def test_decision_with_false_approval_provenance_is_quarantined(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", task="TASK-2026-0001", status="PROPOSED",
                    approved_by="human:user", approved_at="2026-01-03T00:00:00Z")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "DEC-2026-0001")
        self.diag(pkg, "INVALID_RECORD_QUARANTINED")

    def test_approved_decision_without_provenance_gains_no_supersession(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0002"])
        fx.decision("DEC-2026-0002", status="APPROVED",
                    supersedes=["DEC-2026-0001"])
        # strip provenance from the successor: it can no longer be effective
        p = fx.project / ".ai" / "decisions" / "DEC-2026-0002.md"
        p.write_text(p.read_text(encoding="utf-8")
                     .replace('approved_by: human:user\n', "")
                     .replace('approved_at: \'2026-01-03T01:00:00Z\'\n', ""),
                     encoding="utf-8")
        fx.task(decisions=["DEC-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")
        self.assertNotIn("DEC-2026-0002",
                         [i.record_id for i in pkg.selected
                          if "SUPERSESSION_CLOSURE" in i.selection_reasons])

    def test_evidence_bad_enum_is_quarantined_not_normalized(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001", task="TASK-2026-0001",
                    reliability="EXTREMELY_HIGH")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "EVD-2026-0001")
        d = self.diag(pkg, "INVALID_RECORD_QUARANTINED")
        self.assertIn("reliability", d.reason)

    def test_investigation_concluded_needs_conclusion_status(self):
        fx = self.fx
        fx.investigation("INV-2026-0001", task="TASK-2026-0001",
                         status="CONCLUDED")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "INV-2026-0001")
        self.diag(pkg, "INVALID_RECORD_QUARANTINED")

    def test_archived_task_needs_archived_outcome(self):
        fx = self.fx
        fx.task(status="ARCHIVED")
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "ROOT_TASK_INVALID")

    def test_relationship_shape_violation_is_caught(self):
        fx = self.fx
        fx.task(decisions="DEC-2026-0001")     # string, not a list
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "ROOT_TASK_INVALID")

    def test_handoff_missing_created_at_is_not_a_candidate(self):
        fx = self.fx
        fx.handoff("HND-2026-0001")
        p = fx.project / ".ai" / "handoffs" / "HND-2026-0001.md"
        p.write_text(p.read_text(encoding="utf-8")
                     .replace("created_at: '2026-01-06T00:00:00Z'\n", ""),
                     encoding="utf-8")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.not_selected(pkg, "HND-2026-0001")
        self.diag(pkg, "INVALID_RECORD_QUARANTINED")


# --- F04 Decision citation load modes ---------------------------------------

class TestF04CitationLoadMode(LoaderTestCase):

    STATUSES = ["PROPOSED", "UNDER_REVIEW", "APPROVED", "REJECTED",
                "SUPERSEDED", "WITHDRAWN"]

    def build(self, status: str, doc: str) -> Fixture:
        fx = Fixture()
        self.addCleanup(fx.cleanup)
        if status == "SUPERSEDED":
            fx.decision("DEC-2026-0001", status=status,
                        superseded_by=["DEC-2026-0002"])
            fx.decision("DEC-2026-0002", status="APPROVED",
                        supersedes=["DEC-2026-0001"])
        else:
            fx.decision("DEC-2026-0001", status=status)
        fx.doc(doc, f"# Doc\n\nGoverned by DEC-2026-0001.\n")
        fx.task()
        return fx

    def test_task_entry_constraints_citation_is_full_body_for_any_status(self):
        for status in self.STATUSES:
            with self.subTest(status=status):
                fx = self.build(status, "constraints.md")
                pkg = fx.load(task="TASK-2026-0001")
                d = self.item(pkg, "DEC-2026-0001")
                self.assertEqual(d.load_mode, FULL_BODY,
                                 f"{status} must not downgrade the basis")
                self.assertEqual(d.native_status, status)
                self.assertIn("CONSTRAINTS_CITATION", d.selection_reasons)
                self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_project_entry_constraints_citation_is_metadata_for_any_status(self):
        for status in self.STATUSES:
            with self.subTest(status=status):
                fx = self.build(status, "constraints.md")
                pkg = fx.load()
                d = self.item(pkg, "DEC-2026-0001")
                self.assertEqual(d.load_mode, METADATA_ONLY)
                self.assertIsNone(d.body)

    def test_architecture_citation_is_full_body_in_both_modes(self):
        for status in self.STATUSES:
            for task in (None, "TASK-2026-0001"):
                with self.subTest(status=status, task=task):
                    fx = self.build(status, "architecture.md")
                    pkg = fx.load(task=task, include_architecture=True)
                    d = self.item(pkg, "DEC-2026-0001")
                    self.assertEqual(d.load_mode, FULL_BODY)
                    self.assertEqual(d.native_status, status)

    def test_full_body_is_not_approval(self):
        fx = self.build("REJECTED", "constraints.md")
        pkg = fx.load(task="TASK-2026-0001")
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.native_status, "REJECTED")
        self.assertNotIn("APPROVED", d.annotations)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_stronger_basis_wins_over_weaker_one(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="WITHDRAWN", task="TASK-2026-0001")
        fx.doc("constraints.md", "# C\n\nSee DEC-2026-0001.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertIn("DECISION_TASK_BACKLINK", d.selection_reasons)
        self.assertIn("CONSTRAINTS_CITATION", d.selection_reasons)


# --- F05 wave construction --------------------------------------------------

class TestF05Waves(LoaderTestCase):

    def test_explicit_selection_precedes_expansion(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001")
        fx.investigation("INV-2026-0001", evidence=["EVD-2026-0001"])
        fx.report("RPT-2026-0001", investigations=["INV-2026-0001"])
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["RPT-2026-0001"])
        rpt = self.item(pkg, "RPT-2026-0001")
        self.assertEqual(rpt.wave, 0)
        self.assertEqual(rpt.load_mode, FULL_BODY)
        self.assertIn("CALLER_SELECTED", rpt.selection_reasons)
        # Wave 1 from the explicitly selected container, Wave 2 beyond it
        self.assertEqual(self.item(pkg, "INV-2026-0001").wave, 1)
        self.assertEqual(self.item(pkg, "EVD-2026-0001").wave, 2)

    def test_wave_three_is_never_created(self):
        fx = self.fx
        fx.evidence("EVD-2026-0003")
        fx.evidence("EVD-2026-0002", related_evidence=["EVD-2026-0003"])
        fx.evidence("EVD-2026-0001", related_evidence=["EVD-2026-0002"])
        fx.investigation("INV-2026-0001", evidence=["EVD-2026-0001"])
        fx.handoff("HND-2026-0001", investigations=["INV-2026-0001"])
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["HND-2026-0001"])
        waves = {i.record_id: i.wave for i in pkg.selected}
        self.assertEqual(waves["HND-2026-0001"], 0)
        self.assertEqual(waves["INV-2026-0001"], 1)
        self.assertEqual(waves["EVD-2026-0001"], 2)
        self.assertNotIn("EVD-2026-0002", waves)   # would be Wave 3
        self.assertNotIn("EVD-2026-0003", waves)
        self.assertTrue(all((w or 0) <= 2 for w in waves.values()))

    def test_allowlisted_decision_reference_loads_full_body(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="APPROVED")
        fx.investigation("INV-2026-0001", task="TASK-2026-0001",
                         decisions=["DEC-2026-0001"])
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.item(pkg, "DEC-2026-0001")
        self.assertEqual(d.load_mode, FULL_BODY)
        self.assertIn("INVESTIGATION_DECISIONS", d.selection_reasons)
        self.assertEqual(pkg.task_actionability, ACTIONABLE)

    def test_lineage_fields_are_never_auto_traversed(self):
        fx = self.fx
        fx.handoff("HND-2026-0002", task="TASK-2026-0009")
        fx.report("RPT-2026-0002", task="TASK-2026-0009")
        fx.handoff("HND-2026-0001", previous_handoff="HND-2026-0002")
        fx.report("RPT-2026-0001", previous_report="RPT-2026-0002")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001",
                      select=["HND-2026-0001", "RPT-2026-0001"])
        self.not_selected(pkg, "HND-2026-0002")
        self.not_selected(pkg, "RPT-2026-0002")

    def test_foundation_roots_exist_before_expansion(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001")
        fx.decision("DEC-2026-0001", status="APPROVED",
                    body="# D\n\n## Alternatives\n\nRejected per EVD-2026-0001.\n")
        fx.doc("constraints.md", "# C\n\nGoverned by DEC-2026-0001.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        self.assertEqual(self.item(pkg, "DEC-2026-0001").load_mode, FULL_BODY)
        cited = self.item(pkg, "EVD-2026-0001")
        self.assertIn("DECISION_BODY_CITATION", cited.selection_reasons)
        self.assertEqual(cited.wave, 2)


# --- F06 bidirectional supersession reciprocity -----------------------------

class TestF06Reciprocity(LoaderTestCase):

    def test_forward_only_edge_is_rejected(self):
        fx = self.fx
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0002"])
        fx.decision("DEC-2026-0002", status="APPROVED")   # no supersedes
        fx.task(decisions=["DEC-2026-0001"])
        pkg = fx.load(task="TASK-2026-0001")
        d = self.diag(pkg, "DECISION_SUPERSESSION_NON_RECIPROCAL")
        self.assertEqual(d.category, CONFLICT)
        self.assertNotIn("SUPERSESSION_CLOSURE",
                         self.item(pkg, "DEC-2026-0002").selection_reasons)

    def test_backward_only_edge_is_rejected_from_the_successor_side(self):
        """Entering the pair through `supersedes` must reach the same verdict."""
        fx = self.fx
        fx.decision("DEC-2026-0001", status="PROPOSED")   # no superseded_by
        fx.decision("DEC-2026-0002", status="APPROVED",
                    supersedes=["DEC-2026-0001"], task="TASK-2026-0001")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        d = self.diag(pkg, "DECISION_SUPERSESSION_NON_RECIPROCAL")
        self.assertEqual(d.category, CONFLICT)
        self.assertEqual(d.blocking_dimension, NEITHER)
        # the one-sided predecessor never enters effective closure at all
        self.not_selected(pkg, "DEC-2026-0001")

    def test_forward_only_edge_is_diagnosed_from_both_roots(self):
        verdicts = []
        for root in ("DEC-2026-0001", "DEC-2026-0002"):
            fx = Fixture()
            self.addCleanup(fx.cleanup)
            fx.decision("DEC-2026-0001", status="SUPERSEDED",
                        superseded_by=["DEC-2026-0002"])
            fx.decision("DEC-2026-0002", status="APPROVED")
            fx.task(decisions=[root])
            pkg = fx.load(task="TASK-2026-0001")
            verdicts.append("DECISION_SUPERSESSION_NON_RECIPROCAL"
                            in self.codes(pkg))
        self.assertEqual(verdicts, [True, True])

    def test_backward_only_edge_is_diagnosed_from_both_roots(self):
        verdicts = []
        for root in ("DEC-2026-0001", "DEC-2026-0002"):
            fx = Fixture()
            self.addCleanup(fx.cleanup)
            fx.decision("DEC-2026-0001", status="PROPOSED")
            fx.decision("DEC-2026-0002", status="APPROVED",
                        supersedes=["DEC-2026-0001"])
            fx.task(decisions=[root])
            pkg = fx.load(task="TASK-2026-0001")
            verdicts.append("DECISION_SUPERSESSION_NON_RECIPROCAL"
                            in self.codes(pkg))
        self.assertEqual(verdicts, [True, True])

    def test_whitespace_wrapped_candidate_timestamp_is_noncomparable(self):
        fx = self.fx
        fx.handoff("HND-2026-0001",
                   created_at=" 2026-08-17T05:00:00Z ")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        h = self.item(pkg, "HND-2026-0001")
        self.assertNotIn("RECENT", h.annotations)
        self.assertNotIn("HISTORICAL", h.annotations)
        self.diag(pkg, "INVALID_OR_MISSING_CANDIDATE_TIMESTAMP")

    def test_reciprocal_edge_is_accepted_from_either_direction(self):
        for entry in ("predecessor", "successor"):
            with self.subTest(entry=entry):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.decision("DEC-2026-0001", status="SUPERSEDED",
                            superseded_by=["DEC-2026-0002"])
                fx.decision("DEC-2026-0002", status="APPROVED",
                            supersedes=["DEC-2026-0001"])
                root = "DEC-2026-0001" if entry == "predecessor" \
                    else "DEC-2026-0002"
                fx.task(decisions=[root])
                pkg = fx.load(task="TASK-2026-0001")
                other = "DEC-2026-0002" if entry == "predecessor" \
                    else "DEC-2026-0001"
                self.assertIn("SUPERSESSION_CLOSURE",
                              self.item(pkg, other).selection_reasons)
                self.assertNotIn("DECISION_SUPERSESSION_NON_RECIPROCAL",
                                 self.codes(pkg))

    def test_disagreeing_edges_are_rejected_from_either_root(self):
        """Both endpoints declare a supersession edge, but the two edges
        disagree. Whichever endpoint traversal roots at, the verdict is the
        same and neither node enters effective closure."""
        verdicts = []
        for root in ("DEC-2026-0001", "DEC-2026-0002"):
            with self.subTest(root=root):
                fx = Fixture()
                self.addCleanup(fx.cleanup)
                fx.decision("DEC-2026-0001", status="SUPERSEDED",
                            superseded_by=["DEC-2026-0002"])
                fx.decision("DEC-2026-0002", status="APPROVED",
                            supersedes=["DEC-2026-0003"])  # names another
                fx.decision("DEC-2026-0003", status="APPROVED")
                fx.task(decisions=[root])
                pkg = fx.load(task="TASK-2026-0001")
                verdicts.append("DECISION_SUPERSESSION_NON_RECIPROCAL"
                                in self.codes(pkg))
                closed = [i.record_id for i in pkg.selected
                          if "SUPERSESSION_CLOSURE" in i.selection_reasons]
                self.assertEqual(closed, [])
        self.assertEqual(verdicts, [True, True])


# --- F07 fragment resolution ------------------------------------------------

class TestF07Fragments(LoaderTestCase):

    def inv(self, body: str) -> Fixture:
        fx = Fixture()
        self.addCleanup(fx.cleanup)
        fx.investigation("INV-2026-0001", body=body)
        fx.task()
        return fx

    def test_h1_and_h10_are_distinct(self):
        fx = self.inv("# I\n\n### H1\n\nOne.\n\n### H10\n\nTen.\n")
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H10"])
        self.assertEqual(pkg.package_status, READY)
        frag = self.item(pkg, "INV-2026-0001", fragment="H10")
        self.assertIn("Ten.", frag.body)
        self.assertNotIn("One.", frag.body)

    def test_duplicate_real_headings_are_ambiguous_not_first_match(self):
        fx = self.inv("# I\n\n### H1\n\nFirst.\n\n### H1\n\nSecond.\n")
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        self.assertEqual(pkg.package_status, BLOCKED)
        d = self.diag(pkg, "AMBIGUOUS_FRAGMENT")
        self.assertEqual(d.category, AMBIGUITY)
        self.assertEqual(d.blocking_dimension, PACKAGE)
        self.assertNotIn("First.", str(
            self.item(pkg, "INV-2026-0001").body))

    def test_heading_inside_a_code_fence_is_not_a_heading(self):
        fx = self.inv("# I\n\n```markdown\n### H1\n\nNot real.\n```\n")
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        self.assertEqual(pkg.package_status, BLOCKED)
        self.diag(pkg, "INVALID_FRAGMENT")

    def test_real_heading_wins_over_a_fenced_decoy(self):
        fx = self.inv("# I\n\n```\n### H1\n\nDecoy.\n```\n\n"
                      "### H1\n\nGenuine.\n")
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        self.assertEqual(pkg.package_status, READY)
        frag = self.item(pkg, "INV-2026-0001", fragment="H1")
        self.assertIn("Genuine.", frag.body)
        self.assertNotIn("Decoy.", frag.body)

    def test_tilde_fences_are_honoured(self):
        fx = self.inv("# I\n\n~~~\n### H1\n~~~\n\n### H1\n\nGenuine.\n")
        pkg = fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H1"])
        self.assertEqual(pkg.package_status, READY)
        self.assertIn("Genuine.",
                      self.item(pkg, "INV-2026-0001", fragment="H1").body)

    def test_section_boundaries(self):
        body = ("# I\n\n### H1\n\nAlpha.\n\n#### H1 detail\n\nKept.\n\n"
                "### H2\n\nBeta.\n")
        text, status = find_hypothesis_fragment(body, "H1")
        self.assertEqual(status, FRAG_OK)
        self.assertIn("Alpha.", text)
        self.assertIn("Kept.", text)          # lower level stays
        self.assertNotIn("Beta.", text)       # equal level stops
        body2 = "# I\n\n### H1\n\nAlpha.\n\n## Next\n\nGamma.\n"
        text2, _ = find_hypothesis_fragment(body2, "H1")
        self.assertNotIn("Gamma.", text2)     # higher level stops

    def test_fragment_at_end_of_file(self):
        text, status = find_hypothesis_fragment("# I\n\n### H1\n\nLast.\n", "H1")
        self.assertEqual(status, FRAG_OK)
        self.assertIn("Last.", text)

    def test_missing_and_ambiguous_statuses(self):
        self.assertEqual(find_hypothesis_fragment("# I\n", "H1")[1],
                         FRAG_MISSING)
        self.assertEqual(
            find_hypothesis_fragment("### H1\n\na\n\n### H1\n\nb\n", "H1")[1],
            FRAG_AMBIGUOUS)

    def test_iter_headings_skips_fenced_content(self):
        body = "# A\n\n```\n## Fake\n```\n\n## Real\n"
        self.assertEqual([t for _, _, t in iter_headings(body)], ["A", "Real"])


# --- F08 explicit Handoff / Report selection --------------------------------

class TestF08ExplicitSelection(LoaderTestCase):

    def three(self, fx: Fixture) -> None:
        for n, ts in ((1, "2026-02-01T00:00:00Z"), (2, "2026-02-03T00:00:00Z"),
                      (3, "2026-02-02T00:00:00Z")):
            fx.handoff(f"HND-2026-000{n}", created_at=ts)
            fx.report(f"RPT-2026-000{n}", created_at=ts)

    def test_explicit_handoff_suppresses_the_multiple_warning(self):
        fx = self.fx
        self.three(fx)
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["HND-2026-0001"])
        self.no_diag(pkg, "MULTIPLE_HANDOFF_CANDIDATES")
        self.diag(pkg, "MULTIPLE_REPORT_CANDIDATES")   # untouched family
        h = self.item(pkg, "HND-2026-0001")
        self.assertEqual(h.load_mode, FULL_BODY)
        self.assertIn("CALLER_SELECTED", h.selection_reasons)

    def test_explicit_report_suppresses_the_multiple_warning(self):
        fx = self.fx
        self.three(fx)
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["RPT-2026-0003"])
        self.no_diag(pkg, "MULTIPLE_REPORT_CANDIDATES")
        self.diag(pkg, "MULTIPLE_HANDOFF_CANDIDATES")
        self.assertEqual(self.item(pkg, "RPT-2026-0003").load_mode, FULL_BODY)

    def test_explicitly_selected_record_is_not_historical_by_comparison(self):
        fx = self.fx
        self.three(fx)
        fx.task()   # no updated_at
        pkg = fx.load(task="TASK-2026-0001", select=["HND-2026-0001"])
        h = self.item(pkg, "HND-2026-0001")
        self.assertNotIn("HISTORICAL", h.annotations)
        self.assertNotIn("RECENT", h.annotations)
        # the newest candidate still carries navigation RECENT
        self.assertIn("RECENT", self.item(pkg, "HND-2026-0002").annotations)

    def test_explicit_selection_is_a_candidate_even_without_a_backlink(self):
        fx = self.fx
        fx.handoff("HND-2026-0005", task="TASK-2026-0009")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["HND-2026-0005"])
        h = self.item(pkg, "HND-2026-0005")
        self.assertEqual(h.load_mode, FULL_BODY)
        self.assertIn("HND-2026-0005", pkg.navigation.get("Handoff", []))
        self.diag(pkg, "EXPLICIT_SELECTION_TASK_MISMATCH")

    def test_navigation_order_survives_serialization(self):
        fx = self.fx
        self.three(fx)
        fx.task()
        data = fx.load(task="TASK-2026-0001").to_dict()
        self.assertEqual(data["navigation"]["Handoff"],
                         ["HND-2026-0002", "HND-2026-0003", "HND-2026-0001"])
        self.assertEqual(data["navigation"]["Report"],
                         ["RPT-2026-0002", "RPT-2026-0003", "RPT-2026-0001"])
        selected = [i["id"] for i in data["selected"]
                    if i["family"] == "Handoff"]
        self.assertEqual(selected, sorted(selected))   # canonical, not recency


# --- F09 package reporting --------------------------------------------------

class TestF09Reporting(LoaderTestCase):

    def test_every_diagnostic_carries_a_semantic_owner(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001", status="RETRACTED", task="TASK-2026-0001")
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="WITHDRAWN",
                    impact="BLOCKING")
        fx.investigation("INV-2026-0001", task="TASK-2026-0001",
                         supporting_evidence=["EVD-2026-0001"])
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0404"])
        fx.doc("current-state.md", "- TASK-2026-0001 is COMPLETED\n")
        fx.task(questions=["QST-2026-0404"], reports=["RPT-2026-0404"])
        pkg = fx.load(task="TASK-2026-0001")
        missing = [d.code for d in pkg.diagnostics if not d.semantic_owner]
        self.assertEqual(missing, [], f"null semantic_owner on {missing}")

    def test_missing_reference_owner_is_the_referring_relationship(self):
        fx = self.fx
        fx.task(questions=["QST-2026-0404"])
        pkg = fx.load(task="TASK-2026-0001")
        d = self.diag(pkg, "MISSING_REFERENCE")
        self.assertEqual(d.semantic_owner, "Task")
        self.assertEqual(d.field_or_fragment, "questions")
        self.assertEqual(d.target, "QST-2026-0404")
        self.assertEqual(d.wave, 1)

    def test_equivalent_omissions_aggregate_into_one_entry(self):
        fx = self.fx
        for n in (1, 2):
            fx.investigation(f"INV-2026-000{n}", task="TASK-2026-0001",
                             status="CONCLUDED",
                             conclusion_status="SUPPORTED",
                             body="# I\n\n## Current conclusion\n\nDone.\n")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001")
        hits = [o for o in pkg.omissions
                if o["family"] == "Investigation"
                and "on-demand" in o["reason"]]
        self.assertEqual(len(hits), 1, f"expected one aggregated row: {hits}")
        self.assertEqual(hits[0]["count"], 2)
        self.assertTrue(hits[0]["on_demand"])

    def test_omission_rows_are_unique_by_aggregation_key(self):
        fx = self.fx
        fx.task()
        data = fx.load(task="TASK-2026-0001").to_dict()
        keys = [(o["family"], o["reason"], o["on_demand"],
                 o["traversal_boundary"]) for o in data["omissions"]]
        self.assertEqual(len(keys), len(set(keys)))
        for o in data["omissions"]:
            self.assertNotIn("_key", o)

    def test_assumptions_are_complete_or_absent(self):
        fx = self.fx
        fx.task()
        clean = fx.load(task="TASK-2026-0001")
        self.assertEqual(clean.assumptions, [])
        blocked = fx.load(task="TASK-2026-0404")
        self.assertEqual(len(blocked.assumptions), 1)
        a = blocked.assumptions[0]
        for field in ("text", "sources", "basis", "effect",
                      "blocking_dimension"):
            self.assertIn(field, a)
            self.assertTrue(a[field], f"{field} must not be empty")
        self.assertEqual(a["sources"], ["TASK-2026-0404"])
        # The basis names the loader's own rule, not a document users lack.
        self.assertEqual(a["basis"], "context loader rule: root Task resolution")

    def test_caller_selection_is_a_reason_not_an_assumption(self):
        fx = self.fx
        fx.evidence("EVD-2026-0001")
        fx.task()
        pkg = fx.load(task="TASK-2026-0001", select=["EVD-2026-0001"])
        self.assertEqual(pkg.assumptions, [])
        self.assertIn("CALLER_SELECTED",
                      self.item(pkg, "EVD-2026-0001").selection_reasons)


# --- determinism and read-only across the new paths -------------------------

class TestAdversarialInvariants(LoaderTestCase):

    def rich(self, fx: Fixture) -> None:
        fx.evidence("EVD-2026-0001", status="RETRACTED", task="TASK-2026-0001")
        fx.evidence("EVD-2026-0002", task="TASK-2026-0001")
        fx.question("QST-2026-0001", task="TASK-2026-0001", status="OPEN",
                    impact="BLOCKING")
        fx.decision("DEC-2026-0001", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0002"])
        fx.decision("DEC-2026-0002", status="APPROVED",
                    supersedes=["DEC-2026-0001"])
        fx.investigation("INV-2026-0001", task="TASK-2026-0001",
                         supporting_evidence=["EVD-2026-0001"],
                         body="# I\n\n### H1\n\nAlpha.\n\n"
                              "## Current conclusion\n\nOpen.\n")
        fx.handoff("HND-2026-0001", created_at="2026-02-01T00:00:00Z")
        fx.handoff("HND-2026-0002", created_at="2026-02-01 09:00:00")
        fx.report("RPT-2026-0001", created_at="2026-02-02T00:00:00Z")
        fx.doc("current-state.md", "- TASK-2026-0001 is COMPLETED\n")
        fx.doc("architecture.md", "# A\n\nPer DEC-2026-0001.\n")
        fx.doc("constraints.md", "# C\n\nSee DEC-2026-0002.\n")
        fx.task(decisions=["DEC-2026-0001"], questions=["QST-2026-0404"],
                updated_at="2026-03-01T00:00:00Z")

    def test_repeated_runs_are_byte_identical(self):
        fx = self.fx
        self.rich(fx)
        a = fx.load(task="TASK-2026-0001", include_architecture=True,
                    select=["INV-2026-0001#H1"]).to_json()
        b = fx.load(task="TASK-2026-0001", include_architecture=True,
                    select=["INV-2026-0001#H1"]).to_json()
        self.assertEqual(a, b)
        json.loads(a)

    def test_no_runtime_timestamp_or_random_id_enters_output(self):
        import datetime as _dt
        fx = self.fx
        self.rich(fx)
        text = fx.load(task="TASK-2026-0001", include_architecture=True).to_json()
        for year in (_dt.date.today().year, _dt.date.today().year + 1):
            for stamp in (f"{year}-{_dt.date.today():%m-%d}T",):
                if stamp not in "".join(
                        p.read_text(encoding="utf-8")
                        for p in (fx.project / ".ai").rglob("*.md")):
                    self.assertNotIn(stamp, text)

    def test_read_only_across_every_failure_path(self):
        fx = self.fx
        self.rich(fx)
        # a duplicate id, a malformed record, and an invalid record
        fx.rec("EVD-2026-0002",
               {"schema_version": "1.4", "title": "dup",
                "source_type": "OBSERVATION", "status": "ACTIVE",
                "observation": "x", "collected_at": "2026-01-05T00:00:00Z",
                "collected_by": "ai:claude"}, filename="EVD-2026-0009")
        (fx.project / ".ai" / "reports" / "RPT-2026-0009.md").write_text(
            "no frontmatter\n", encoding="utf-8")
        fx.question("QST-2026-0009", schema_version="9.9")
        fx.handoff("HND-2026-0009", body="")
        fx.report("RPT-2026-0010", body="")
        fx.decision("DEC-2026-0005", status="SUPERSEDED",
                    superseded_by=["DEC-2026-0006"])
        fx.decision("DEC-2026-0006", status="APPROVED")

        before = fx.snapshot()
        fx.load()                                            # DIAGNOSTIC paths
        fx.load(task="TASK-2026-0001")                       # READY
        fx.load(task="TASK-2026-0001", include_architecture=True)
        fx.load(task="TASK-2026-0001", select=["INV-2026-0001#H9"])  # BLOCKED
        fx.load(task="TASK-2026-0404")                       # BLOCKED
        fx.load(task="TASK-2026-0001", select=["EVD-2026-0002"])     # duplicate
        fx.load(task="TASK-2026-0001", select=["HND-2026-0009"])
        fx.load(task="TASK-2026-0001", select=["RPT-2026-0010"])
        fx.load(task="TASK-2026-0001", select=["DEC-2026-0005"])
        fx.load(task="TASK-2026-0001", select=["DEC-2026-0006"])
        fx.manifest("- broken\n")
        fx.load(task="TASK-2026-0001")                       # DIAGNOSTIC_ONLY
        after = fx.snapshot()
        changed = {k for k in before.keys() & after.keys()
                   if before[k] != after[k]}
        self.assertEqual(set(after) - set(before), set())
        self.assertEqual(set(before) - set(after), set())
        # the manifest rewrite above is the fixture's own write, not the loader
        self.assertEqual(changed, {str(Path(".ai") / "manifest.yaml")})


if __name__ == "__main__":
    unittest.main(verbosity=2)

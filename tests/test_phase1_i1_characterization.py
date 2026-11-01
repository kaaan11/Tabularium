"""Phase I (I1) baseline characterization.

Pins the existing contracts that the v0.2 operator-experience work builds on,
BEFORE any product change. Everything here must keep passing unchanged after
Phase I; these are protections, not proposals:

- Task discovery/identity rules of the production loader (canonical filename
  grammar, README/dotfile exclusion, malformed retention, id-mismatch
  ambiguity, duplicate ids without a winner, unreadable storage visibility).
- The frozen consumed-invariant validator (`validate_consumed`) for Task.
- The established `_next_id` allocation pattern in tabularium/engine/continuation.
- Run refusal ordering and Task selection behavior at the product entrypoint.
- `.execution` locator semantics: which paths write it, which do not, and how
  `continue` depends on its value.

The known pre-runtime overwrite defect itself is demonstrated separately
(PHASE_I_FINDINGS.md, /tmp fixtures) and repaired with regression tests in
the I2 commit; this file pins only contracts that hold at the baseline.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.execution import (
    NO_CONTINUATION,
    PROJECT_PATH_INVALID,
    TASK_REQUIRED,
    TASK_SELECTION_REQUIRED,
    _checkpoint_path,
    _choose_task,
    continue_execution,
    remember_execution,
    run_instruction,
)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.continuation.planner import _next_id
from tabularium.engine.context_loader.confinement import ProjectScope
from tabularium.engine.context_loader.discovery import TERMINAL_TASK_STATUSES, discover
from tabularium.engine.context_loader.validate import validate_consumed


def minimal_task_frontmatter(**overrides):
    content = {
        "schema_version": "1.10", "id": "TASK-2026-0001",
        "title": "T", "objective": "o", "status": "IN_PROGRESS",
        "created_at": "2026-01-01T00:00:00Z", "created_by": "USER",
    }
    content.update(overrides)
    return content


def write_task(task_dir: Path, record_id: str, frontmatter=None, body=None):
    data = frontmatter if frontmatter is not None \
        else minimal_task_frontmatter(id=record_id)
    text = "---\n" + yaml.safe_dump(data, sort_keys=False) + "---\n"
    text += body if body is not None else f"# {record_id}\n\nbody\n"
    (task_dir / f"{record_id}.md").write_text(text, encoding="utf-8")


class ProjectFixture(unittest.TestCase):
    """An initialized, onboarded project with a Task directory."""

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i1-")
        # Worker availability is pinned to a fixture file rather than left
        # to whatever `codex` the host's PATH happens to carry: these
        # assertions are about Tabularium, not about the developer's machine.
        # Nothing spawns it - every test here either stops at preflight or
        # passes an explicit adapter. Tests that need the worker ABSENT patch
        # the same seam themselves, and their inner patch wins.
        self._native_holder = tempfile.TemporaryDirectory(
            prefix="tabu-native-fixture-")
        self.addCleanup(self._native_holder.cleanup)
        native = Path(self._native_holder.name) / "codex-fixture"
        native.write_text("", encoding="utf-8")
        native_patch = patch("tabularium.execution.find_native_executable",
                             return_value=str(native))
        native_patch.start()
        self.addCleanup(native_patch.stop)
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "I1 fixture",
            "purpose": "Baseline characterization",
            "scope": "Bounded characterization project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-01-01T00:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        self.task_dir = self.root / ".ai" / "tasks"
        self.task_dir.mkdir(exist_ok=True)

    def tearDown(self):
        self.holder.cleanup()


class DiscoveryContractTests(ProjectFixture):
    def test_canonical_filename_grammar_and_readme_exclusion(self):
        write_task(self.task_dir, "TASK-2026-0001")
        write_task(self.task_dir, "TASK-2026-0002")
        (self.task_dir / "README.md").write_text("# docs\n", encoding="utf-8")
        (self.task_dir / ".editor-lock").write_text("x", encoding="utf-8")
        (self.task_dir / "notes.txt").write_text("x", encoding="utf-8")
        repo = discover(self.root, ProjectScope(self.root))
        task_ids = [r.declared_id for r in repo.family_records("Task")]
        self.assertEqual(task_ids, ["TASK-2026-0001", "TASK-2026-0002"])

    def test_non_canonical_filename_is_retained_as_malformed_not_a_record(self):
        (self.task_dir / "TASK-26-1.md").write_text(
            "---\nid: TASK-26-1\n---\nbody\n", encoding="utf-8")
        write_task(self.task_dir, "TASK-2026-0007")
        repo = discover(self.root, ProjectScope(self.root))
        usable_ids = [r.declared_id for r in repo.family_records("Task")
                      if repo.record_usable(r)]
        self.assertEqual(usable_ids, ["TASK-2026-0007"])
        malformed = [r for r in repo.family_records("Task") if r.malformed]
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0].malformed_reason,
                         "filename is not a canonical id")

    def test_id_mismatch_is_indexed_under_both_ids_and_resolves_no_winner(self):
        rec_id = "TASK-2026-0003"
        (self.task_dir / f"{rec_id}.md").write_text(
            "---\n"
            + yaml.safe_dump(minimal_task_frontmatter(id="TASK-2026-9999"),
                             sort_keys=False)
            + "---\nbody\n", encoding="utf-8")
        repo = discover(self.root, ProjectScope(self.root))
        rec = repo.family_records("Task")[0]
        # A single mismatched file is unusable at repository level even though
        # bare `unique()` is merely count-based: identity resolution for any
        # consumer goes through identity_resolved/record_usable.
        self.assertFalse(repo.record_usable(rec))
        self.assertFalse(repo.identity_resolved(rec))
        self.assertIsNone(repo.unique("TASK-2026-0004"))

    def test_duplicate_declared_ids_never_choose_a_winner(self):
        write_task(self.task_dir, "TASK-2026-0004")
        # A second file with canonical grammar claiming the SAME declared id.
        dup = minimal_task_frontmatter(id="TASK-2026-0004")
        (self.task_dir / "TASK-2026-0005.md").write_text(
            "---\n" + yaml.safe_dump(dup, sort_keys=False) + "---\nbody\n",
            encoding="utf-8")
        repo = discover(self.root, ProjectScope(self.root))
        for rec in repo.family_records("Task"):
            if rec.declared_id == "TASK-2026-0004":
                self.assertFalse(repo.record_usable(rec))
        self.assertTrue(repo.is_duplicate("TASK-2026-0004"))

    def test_unreadable_task_directory_is_reported_not_treated_as_empty(self):
        write_task(self.task_dir, "TASK-2026-0009")
        self.task_dir.chmod(0o000)
        try:
            repo = discover(self.root, ProjectScope(self.root))
        finally:
            self.task_dir.chmod(0o755)
        self.assertIn(("Task", ".ai/tasks"), repo.unreadable_dirs)

    def test_terminal_status_set_is_frozen(self):
        self.assertEqual(TERMINAL_TASK_STATUSES,
                         {"COMPLETED", "CANCELLED", "ARCHIVED"})


class ValidateConsumedContractTests(unittest.TestCase):
    def test_minimal_valid_task_has_no_errors(self):
        errors = validate_consumed("Task", minimal_task_frontmatter(), "# b\n")
        self.assertEqual(errors, [])

    def test_required_strings_are_enforced(self):
        fm = minimal_task_frontmatter()
        del fm["objective"]
        errors = validate_consumed("Task", fm, "")
        self.assertIn("objective is required and must be a non-empty string",
                      errors)

    def test_unknown_fields_are_refused(self):
        fm = minimal_task_frontmatter(unexpected_field="x")
        errors = validate_consumed("Task", fm, "")
        self.assertTrue(any("unrecognized frontmatter fields" in e
                            for e in errors))

    def test_enum_fields_use_the_closed_vocabularies(self):
        errors = validate_consumed(
            "Task", minimal_task_frontmatter(type="WHIM"), "")
        self.assertTrue(any(e.startswith("type is not one of ")
                            for e in errors))
        errors = validate_consumed(
            "Task", minimal_task_frontmatter(priority="WHENEVER"), "")
        self.assertTrue(any(e.startswith("priority is not one of ")
                            for e in errors))

    def test_approval_provenance_rules(self):
        fm = minimal_task_frontmatter(approval_status="APPROVED")
        errors = validate_consumed("Task", fm, "")
        self.assertTrue(any("approval_status APPROVED requires approved_by "
                            "and approved_at" in e for e in errors))
        ok = minimal_task_frontmatter(approval_status="APPROVED",
                                      approved_by="human:user",
                                      approved_at="2026-01-01T00:00:00Z")
        self.assertEqual(validate_consumed("Task", ok, ""), [])
        orphan_scope = minimal_task_frontmatter(
            approved_scope={"approved_operations": ["read"]})
        errors = validate_consumed("Task", orphan_scope, "")
        self.assertTrue(any("approved_scope must be absent unless "
                            "approval_status is APPROVED" in e
                            for e in errors))

    def test_schema_version_is_pinned_to_1_10(self):
        errors = validate_consumed(
            "Task", minimal_task_frontmatter(schema_version="1.11"), "")
        self.assertTrue(any("schema_version '1.11' is not the supported" in e
                            for e in errors))


class NextIdPatternTests(unittest.TestCase):
    """The established Handoff/Report allocator Phase I generalizes."""

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i1-id-")
        self.root = Path(self.holder.name)

    def tearDown(self):
        self.holder.cleanup()

    def alloc(self, family_dir, name):
        d = self.root / ".ai" / family_dir
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text("---\n---\n", encoding="utf-8")

    def test_max_plus_one_within_the_year(self):
        self.alloc("handoffs", "HND-2026-0003.md")
        self.alloc("handoffs", "HND-2026-0001.md")
        self.assertEqual(_next_id(str(self.root), "Handoff", 2026),
                         ("HND-2026-0004", False))

    def test_gaps_are_not_filled(self):
        self.alloc("reports", "RPT-2026-0002.md")
        self.assertEqual(_next_id(str(self.root), "Report", 2026),
                         ("RPT-2026-0003", False))

    def test_years_are_independent_sequences(self):
        self.alloc("handoffs", "HND-2025-0042.md")
        self.assertEqual(_next_id(str(self.root), "Handoff", 2026),
                         ("HND-2026-0001", False))

    def test_unreadable_directory_refuses_instead_of_reallocating(self):
        self.alloc("handoffs", "HND-2026-0005.md")
        d = self.root / ".ai" / "handoffs"
        d.chmod(0o000)
        try:
            result = _next_id(str(self.root), "Handoff", 2026)
        finally:
            d.chmod(0o755)
        self.assertIsNone(result)

    def test_sequence_exhaustion_refuses(self):
        self.alloc("handoffs", "HND-2026-9999.md")
        self.assertIsNone(_next_id(str(self.root), "Handoff", 2026))

    def test_first_allocation_in_missing_directory_starts_at_one(self):
        self.assertEqual(_next_id(str(self.root), "Report", 2026),
                         ("RPT-2026-0001", False))


class RunRefusalOrderingTests(ProjectFixture):
    def test_invalid_target_is_the_first_gate(self):
        result = run_instruction("x", self.root / "missing")
        self.assertEqual(result.code, PROJECT_PATH_INVALID)

    def test_empty_instruction_refuses_before_project_health(self):
        shutil.rmtree(self.root / ".ai")
        result = run_instruction("   ", self.root)
        self.assertEqual(result.code, "INSTRUCTION_REQUIRED")


class TaskSelectionTests(ProjectFixture):
    def test_no_usable_task_refuses_with_creation_guidance(self):
        chosen, error = _choose_task(self.root, None)
        self.assertIsNone(chosen)
        self.assertEqual(error.code, TASK_REQUIRED)

    def test_single_active_task_is_selected_without_prompting(self):
        write_task(self.task_dir, "TASK-2026-0001")
        chosen, error = _choose_task(self.root, None)
        self.assertIsNone(error)
        self.assertEqual(chosen, "TASK-2026-0001")

    def test_terminal_tasks_do_not_win_selection(self):
        write_task(self.task_dir, "TASK-2026-0001",
                   minimal_task_frontmatter(status="ARCHIVED",
                                            archived_outcome="CANCELLED"))
        chosen, error = _choose_task(self.root, None)
        self.assertIsNone(chosen)
        self.assertEqual(error.code, TASK_REQUIRED)

    def test_multiple_active_tasks_require_explicit_selection(self):
        write_task(self.task_dir, "TASK-2026-0001")
        write_task(self.task_dir, "TASK-2026-0002")
        chosen, error = _choose_task(self.root, None)
        self.assertIsNone(chosen)
        self.assertEqual(error.code, TASK_SELECTION_REQUIRED)

    def test_explicit_request_bypasses_discovery(self):
        chosen, error = _choose_task(self.root, "TASK-2026-7777")
        self.assertIsNone(error)
        self.assertEqual(chosen, "TASK-2026-7777")

    def test_unreadable_storage_refuses_rather_than_guessing(self):
        write_task(self.task_dir, "TASK-2026-0001")
        self.task_dir.chmod(0o000)
        try:
            chosen, error = _choose_task(self.root, None)
        finally:
            self.task_dir.chmod(0o755)
        self.assertIsNone(chosen)
        self.assertEqual(error.code, TASK_REQUIRED)
        self.assertIn("unreadable", error.message)


try:
    import langgraph  # noqa: F401
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_RUNTIME = False

needs_runtime = unittest.skipUnless(HAS_RUNTIME, "LangGraph not installed")


class ExecutionMarkerTimingTests(ProjectFixture):
    """.execution locator facts that hold at the baseline and must survive.

    In the baseline the ONLY writer is `remember_execution`, called once from
    `run_instruction` AFTER `start_execution` returned. These tests pin:
    - a real runtime entry through the product path updates the marker;
    - a refusal raised OUT of start_execution leaves an existing marker
      untouched;
    - `continue` resolves work strictly through the marker value, so a stale
      or wrong marker hides otherwise-resumable checkpoint state.
    """

    def marker(self) -> Path:
        return _checkpoint_path(self.root).with_suffix(".execution")

    @needs_runtime
    def test_product_runtime_entry_writes_marker_with_the_execution_id(self):
        from tabularium.engine.workers import MockWorker

        write_task(self.task_dir, "TASK-2026-0001")
        self.assertFalse(self.marker().exists())
        result = run_instruction("characterize the marker", self.root,
                                 task_id="TASK-2026-0001",
                                 adapter=MockWorker())
        self.assertEqual(result.status, "COMPLETED")
        self.assertTrue(result.pipeline.executed)
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.marker().read_text(encoding="utf-8").strip(),
                         result.pipeline.execution_id)

    @needs_runtime
    def test_exception_refusal_leaves_previous_marker_untouched(self):
        """start_execution RAISING (e.g. absent observability module) is one
        of the two baseline pre-runtime refusal shapes; it never reaches the
        marker write."""
        write_task(self.task_dir, "TASK-2026-0001")
        remember_execution(self.root, "i1-previous-execution")
        hidden = {"tabularium.engine.observability": None,
                  "tabularium.engine.observability.events": None}
        with patch.dict(sys.modules, hidden):
            result = run_instruction("work", self.root,
                                     task_id="TASK-2026-0001")
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, "WORKER_UNAVAILABLE")
        self.assertEqual(self.marker().read_text(encoding="utf-8").strip(),
                         "i1-previous-execution")

    @needs_runtime
    def test_stale_marker_hides_suspended_state_from_continue(self):
        """Downstream observation (I1-D): continue trusts only the marker."""
        from tabularium.engine.execution import ExecutionPlan, inspect_execution
        from tabularium.engine.execution import start_execution
        from tabularium.engine.skills import ControlPlaneGrant
        from tabularium.engine.workers import MockWorker

        write_task(self.task_dir, "TASK-2026-0001")
        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(
            project_root=str(self.root), task_id="TASK-2026-0001",
            intent="suspended work", execution_id="i1-suspended",
            checkpoint_path=str(db), adapter=MockWorker(),
            grant=ControlPlaneGrant(approval_required=True))
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        remember_execution(self.root, "i1-suspended")
        # Simulate exactly what a later overwrite does to the shared marker.
        remember_execution(self.root, "i1-never-entered-runtime")
        result = continue_execution(self.root)
        self.assertEqual(result.status, "REFUSED")
        self.assertEqual(result.code, NO_CONTINUATION)
        # ...while the suspended state remains inspectable under its own id.
        pending = inspect_execution("i1-suspended", str(db))
        self.assertIsNotNone(pending)
        self.assertEqual(pending.runtime_status, "AWAITING_APPROVAL")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

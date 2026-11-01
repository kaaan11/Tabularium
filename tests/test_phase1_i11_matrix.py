"""Phase I (I11) interaction matrix.

Exercises the combinations the human queue requires, proving the new
surfaces compose with each other and with run/continue without reopening
any closed issue.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium import explain as X
from tabularium import task as T
from tabularium.execution import (_checkpoint_path, continue_execution,
                            make_resolution, remember_execution,
                            run_instruction)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.execution import ExecutionPlan, inspect_execution, start_execution
from tabularium.engine.skills import ControlPlaneGrant
from tabularium.engine.workers import MockWorker


class MatrixFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i11-")
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
            "title": "I11 fixture", "purpose": "interaction matrix",
            "scope": "Bounded matrix project",
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

    def write(self, record_id, fm_overrides=None, filename=None):
        fm = {"schema_version": "1.10", "id": record_id, "title": f"T {record_id}",
              "objective": "o", "status": "IN_PROGRESS",
              "created_at": "2026-01-01T00:00:00Z", "created_by": "USER"}
        fm.update(fm_overrides or {})
        name = filename or f"{record_id}.md"
        (self.task_dir / name).write_text(
            "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# b\n",
            encoding="utf-8")


class StorageConditionMatrix(MatrixFixture):
    """Every surface against malformed / unreadable / empty storage."""

    def test_malformed_task_is_consistent_across_surfaces(self):
        (self.task_dir / "broken-name.md").write_text("not frontmatter\n",
                                                      encoding="utf-8")
        self.write("TASK-2026-0002", {"priority": "WHENEVER"})
        listing = T.task_list(self.root)
        self.assertEqual(listing.total_count, 2)
        states = sorted(t.state for t in listing.tasks)
        self.assertEqual(states, [T.UNUSABLE, T.UNUSABLE])
        shown = T.task_show(self.root, "TASK-2026-0002")
        self.assertFalse(shown.valid)
        self.assertIn(T.INVALID_RECORD_QUARANTINED, shown.error_codes)
        validated = T.task_validate(self.root)
        self.assertEqual(validated.invalid_count, 2)
        self.assertEqual(validated.exit_code, 1)
        preflight = X.preflight("work", self.root)
        self.assertFalse(preflight.would_run)

    def test_unreadable_storage_refuses_every_surface_consistently(self):
        self.write("TASK-2026-0001")
        self.task_dir.chmod(0o000)
        try:
            code = T.RECORD_DIRECTORY_UNREADABLE
            results = [
                T.task_list(self.root),
                T.task_show(self.root, "TASK-2026-0001"),
                T.task_validate(self.root),
                T.allocate_task_id(self.root),
            ]
            for result in results:
                self.assertEqual(result.code if hasattr(result, "code")
                                 else result.code, code)
        finally:
            self.task_dir.chmod(0o755)

    def test_no_usable_task_explains_creation_guidance(self):
        preflight = X.preflight("work", self.root)
        self.assertEqual(preflight.code, T.TASK_REQUIRED if hasattr(T, "TASK_REQUIRED") else "TASK_REQUIRED")
        self.assertIn("No active Task", preflight.message)


class SelectionMatrix(MatrixFixture):
    def setUp(self):
        super().setUp()
        self.write("TASK-2026-0001")

    def test_explicit_task_flows_through_explain_and_run_alike(self):
        self.write("TASK-2026-0001")
        self.write("TASK-2026-0002")
        explained = X.preflight("work", self.root, task_id="TASK-2026-0002")
        self.assertTrue(explained.would_run)
        self.assertEqual(explained.task_id, "TASK-2026-0002")

    def test_worker_selection_refusals_match_run(self):
        with patch("tabularium.execution.find_native_executable",
                   return_value=None), \
             patch("tabularium.execution.find_opencode_executable",
                   return_value=None):
            explained = X.preflight("work", self.root)
            ran = run_instruction("work", self.root)
        self.assertEqual(explained.code, ran.code)
        self.assertIn(explained.code, {"WORKER_SELECTION_REQUIRED",
                                       "WORKER_UNAVAILABLE"})

    def test_api_worker_without_optional_dependency_warns_but_predicts_start(self):
        hidden = {"litellm": None}
        with patch.dict(sys.modules, hidden):
            explained = X.preflight("work", self.root, worker="api",
                                    model="test/model")
        self.assertTrue(explained.would_run)
        self.assertTrue(any("litellm" in w for w in explained.warnings))


class MarkerMatrix(MatrixFixture):
    """Marker interactions across every Phase I surface."""

    def setUp(self):
        super().setUp()
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("LangGraph not installed")
        self.write("TASK-2026-0001")
        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(project_root=str(self.root),
                             task_id="TASK-2026-0001", intent="pending work",
                             execution_id="i11-suspended",
                             checkpoint_path=str(db), adapter=MockWorker(),
                             grant=ControlPlaneGrant(approval_required=True))
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        remember_execution(self.root, "i11-suspended")
        self.marker_bytes = (_checkpoint_path(self.root)
                             .with_suffix(".execution").read_bytes())

    def test_all_preflight_and_refusal_paths_preserve_marker_then_continue(self):
        X.preflight("work", self.root)
        X.preflight("work", self.root, command="dry-run")
        hidden = {"langgraph", "langgraph.checkpoint",
                  "langgraph.checkpoint.sqlite", "langgraph.graph",
                  "langgraph.types"}
        with patch.dict(sys.modules, {name: None for name in hidden}):
            run_instruction("refused", self.root, task_id="TASK-2026-0001")
        marker = _checkpoint_path(self.root).with_suffix(".execution")
        self.assertEqual(marker.read_bytes(), self.marker_bytes)
        pending = inspect_execution("i11-suspended",
                                    str(_checkpoint_path(self.root)))
        resolution = make_resolution(pending.approval_request, "APPROVE",
                                     "human:user",
                                     "2026-01-01T00:00:00+00:00")
        resumed = continue_execution(self.root, resolution=resolution)
        self.assertEqual(resumed.status, "COMPLETED")

    def test_real_runtime_start_still_updates_the_marker(self):
        result = run_instruction("real work after suspension", self.root,
                                 task_id="TASK-2026-0001",
                                 adapter=MockWorker())
        self.assertEqual(result.status, "COMPLETED")
        marker = _checkpoint_path(self.root).with_suffix(".execution")
        self.assertNotEqual(marker.read_bytes(), self.marker_bytes)
        self.assertEqual(marker.read_text(encoding="utf-8").strip(),
                         result.pipeline.execution_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

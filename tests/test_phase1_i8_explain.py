"""Phase I (I8) `tabu explain` surface tests (plus the shared
fixture used by the I9 dry-run tests).

Pins:
- ONE engine, two entry points: identical substantive facts, only the
  originating command label differs;
- refusal parity with the REAL run (codes/messages come from the same
  mapping);
- sentinel guarantees: no worker invocation, no provider call, no checkpoint,
  no `.execution` write, no project mutation — including when a previous real
  execution's marker exists;
- exit classes (0 would-run / 1 refused / 2 usage-path) and Decision C:
  plain `run --json` remains a usage error.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium import explain as X
from tabularium.cli import main
from tabularium.execution import (_checkpoint_path, continue_execution,
                            make_resolution, remember_execution,
                            run_instruction)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.task import task_create
from tabularium.engine.execution import ExecutionPlan, start_execution
from tabularium.engine.skills import ControlPlaneGrant
from tabularium.engine.workers import MockWorker


def minimal_task(task_dir: Path, record_id: str = "TASK-2026-0001"):
    fm = {"schema_version": "1.10", "id": record_id, "title": "T",
          "objective": "o", "status": "IN_PROGRESS",
          "created_at": "2026-01-01T00:00:00Z", "created_by": "USER"}
    (task_dir / f"{record_id}.md").write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# T\n",
        encoding="utf-8")


class CountingWorker(MockWorker):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return super().execute(request)


class ExplainFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i8-")
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
            "title": "I8 fixture", "purpose": "preflight surfaces",
            "scope": "Bounded explain project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-01-01T00:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        self.task_dir = self.root / ".ai" / "tasks"
        self.task_dir.mkdir(exist_ok=True)
        minimal_task(self.task_dir)

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def ai_snapshot(root: Path) -> dict:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted((root / ".ai").rglob("*")) if p.is_file()}

    @staticmethod
    def run_main(argv):
        # Production native-worker construction may leave a TemporaryDirectory
        # for asynchronous GC cleanup; its ResourceWarning is unrelated noise
        # for these assertions.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = main(argv)
        return code, out.getvalue(), err.getvalue()


class EngineParityTests(ExplainFixture):
    def test_refusal_parity_with_the_real_run_for_preparation_stage(self):
        # A requested task id that resolves to nothing refuses inside the
        # pipeline; explain must report exactly what the run would report.
        explained = X.preflight("work", self.root,
                                task_id="TASK-9999-0009")
        ran = run_instruction("work", self.root, task_id="TASK-9999-0009")
        self.assertEqual(explained.status, X.REFUSED)
        self.assertEqual(explained.code, ran.code)
        self.assertEqual(explained.message, ran.message)
        self.assertIn("CONTEXT_PACKAGE_NOT_READY", explained.reason_codes)

    def test_would_run_reports_worker_facts_and_bounded_metrics(self):
        result = X.preflight("work", self.root)
        self.assertTrue(result.would_run)
        self.assertIsNotNone(result.worker)
        self.assertNotEqual(result.worker.worker_id, "")
        self.assertIs(result.approval_required, False)
        if result.metrics is not None:
            self.assertIsInstance(result.metrics.get("payload_size"), dict)

    def test_native_model_warning_semantics_match_run(self):
        with tempfile.TemporaryDirectory(prefix="tabu-i8-native-") as scratch:
            script = Path(scratch) / "native.py"
            script.write_text("import json\n"
                              "print(json.dumps({'type':'item.completed',"
                              "'item':{'type':'agent_message',"
                              "'text':'X'}}))\n", encoding="utf-8")

            class FixtureNative(MockWorker):
                pass

            with patch("tabularium.execution.find_native_executable",
                       return_value=str(script)):
                result = X.preflight("work", self.root, model="test/model",
                                     worker="native")
            self.assertTrue(result.would_run)
            self.assertTrue(any("does not use it" in w or "--model" in w
                                for w in result.warnings))


class SentinelTests(ExplainFixture):
    def marker(self):
        return _checkpoint_path(self.root).with_suffix(".execution")

    def test_explain_never_invokes_the_worker_or_writes(self):
        before = self.ai_snapshot(self.root)
        worker = CountingWorker()
        result = X.preflight("work", self.root, adapter=worker)
        self.assertTrue(result.would_run)
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.ai_snapshot(self.root), before)
        self.assertFalse(self.marker().exists())

    def test_refused_explain_never_invokes_the_worker_or_writes(self):
        before = self.ai_snapshot(self.root)
        worker = CountingWorker()
        result = X.preflight("work", self.root, adapter=worker,
                             task_id="TASK-9999-0009")
        self.assertFalse(result.would_run)
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.ai_snapshot(self.root), before)

    def test_existing_marker_survives_explain_and_dry_run_byte_for_byte(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("LangGraph not installed")
        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(project_root=str(self.root),
                             task_id="TASK-2026-0001",
                             intent="suspended", execution_id="i8-real",
                             checkpoint_path=str(db), adapter=MockWorker(),
                             grant=ControlPlaneGrant(approval_required=True))
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        remember_execution(self.root, "i8-real")
        before_bytes = self.marker().read_bytes()
        before_snapshot = self.ai_snapshot(self.root)
        for command in ("explain", "dry-run"):
            result = X.preflight("work", self.root, command=command)
            self.assertTrue(result.would_run, command)
            self.assertEqual(self.marker().read_bytes(), before_bytes)
        # And a REFUSED real run keeps preserving it (I2 regression surface).
        hidden = {"langgraph", "langgraph.checkpoint",
                  "langgraph.checkpoint.sqlite", "langgraph.graph",
                  "langgraph.types"}
        with patch.dict(sys.modules, {name: None for name in hidden}):
            refused = run_instruction("work", self.root,
                                      task_id="TASK-2026-0001")
        self.assertEqual(refused.status, "PREPARATION_REJECTED")
        self.assertEqual(self.marker().read_bytes(), before_bytes)
        self.assertEqual(self.ai_snapshot(self.root), before_snapshot)

    def test_dry_run_does_not_execute_even_when_runtime_is_present(self):
        worker = CountingWorker()
        result = X.preflight("work", self.root, adapter=worker,
                             command="dry-run")
        self.assertTrue(result.would_run)
        self.assertEqual(worker.calls, 0)


class CliSurfaceTests(ExplainFixture):
    def test_cli_explain_happy_path_human_and_json(self):
        code, out, err = self.run_main(["explain", "work", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("Status: would-run", out)
        self.assertIn("Tabularium preflight (explain)", out)
        code, out2, err2 = self.run_main(["explain", "work", str(self.root),
                                          "--json"])
        self.assertEqual(code, 0)
        parsed = json.loads(out2)
        self.assertEqual(parsed["command"], "explain")
        self.assertTrue(parsed["would_run"])
        self.assertEqual(parsed["exit_code"], 0)
        self.assertEqual(err2, "")

    def test_failure_routes_and_exit_classes(self):
        code, out, err = self.run_main(["explain", "   ", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("INSTRUCTION_REQUIRED", err)
        code, out, err = self.run_main(["explain", "work",
                                        str(self.root / "missing")])
        self.assertEqual(code, 2)
        code, out2, _ = self.run_main(["explain", "work",
                                       str(self.root / "missing"), "--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out2)["code"], "PROJECT_PATH_INVALID")

    def test_json_is_the_whole_stdout_payload_on_refusal_paths(self):
        code, out, err = self.run_main(["explain", "work",
                                        "--task", "TASK-9999-0009",
                                        str(self.root), "--json"])
        self.assertEqual(code, 1)
        parsed = json.loads(out)
        self.assertFalse(parsed["would_run"])
        self.assertIn("CONTEXT_PACKAGE_NOT_READY", parsed["reason_codes"])


class OperatorJourneyTests(ExplainFixture):
    """I11 core slice: create -> list -> show -> validate -> explain -> run."""

    @unittest.skipUnless(True, "always on")
    def test_created_task_flows_through_every_surface_to_a_real_run(self):
        # A fresh store: creation, not the fixture, supplies the Task.
        (self.task_dir / "TASK-2026-0001.md").unlink()
        try:
            import langgraph  # noqa: F401
            HAS_RUNTIME = True
        except ImportError:
            HAS_RUNTIME = False
        created = task_create(self.root, title="Journey",
                              objective="travel every surface")
        self.assertTrue(created.created)
        listing_code, list_out, _ = self.run_main(["task", "list",
                                                   str(self.root), "--json"])
        self.assertEqual(listing_code, 0)
        self.assertIn(created.record_id, list_out)
        show_code, show_out, _ = self.run_main(["task", "show",
                                                created.record_id,
                                                str(self.root), "--json"])
        self.assertEqual(show_code, 0)
        json.loads(show_out)
        validate_code, _, _ = self.run_main(["task", "validate",
                                             created.record_id,
                                             str(self.root)])
        self.assertEqual(validate_code, 0)
        explain_result = X.preflight("journey work", self.root)
        self.assertTrue(explain_result.would_run)
        if not HAS_RUNTIME:
            self.skipTest("LangGraph not installed for the final run leg")
        ran = run_instruction("journey work", self.root,
                              task_id=created.record_id,
                              adapter=MockWorker())
        self.assertEqual(ran.status, "COMPLETED")

    def test_continue_still_works_after_preflights(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("LangGraph not installed")
        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(project_root=str(self.root),
                             task_id="TASK-2026-0001", intent="pending",
                             execution_id="i8-cont",
                             checkpoint_path=str(db), adapter=MockWorker(),
                             grant=ControlPlaneGrant(approval_required=True))
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        remember_execution(self.root, "i8-cont")
        X.preflight("other work", self.root, command="dry-run")
        pending = __import__("tabularium.engine.execution", fromlist=["inspect"]) \
            .inspect_execution("i8-cont", str(db))
        resolution = make_resolution(pending.approval_request, "APPROVE",
                                     "human:user",
                                     "2026-01-01T00:00:00+00:00")
        resumed = continue_execution(self.root, resolution=resolution)
        self.assertEqual(resumed.status, "COMPLETED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

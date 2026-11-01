"""First production caller for the governed DAG (DEC-2026-0010, A2).

`--workflow {single,dag}` (default `single`) routes `tabu run` — and the TUI
`R` flow — through the already-ratified `MultiAgentWorkflow.run_protected`
with the SAME caller-selected adapter. Replay-store activation stays a rule,
not code: DRL-001 remains OPEN.

Real objects throughout: real onboarded projects, the real `run_instruction`
entrypoint, the real checkpoint on disk. The properties under test: defaults
never change, unknown workflows are refused before any gate runs, vetoes
suspend to the existing human boundary, and resume never re-runs the worker.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tabularium.execution import (
    WORKFLOW_SELECTION_INVALID,
    continue_execution,
    make_resolution,
    run_instruction,
)


class CountingWorker:
    """Deterministic stub with fixed output and an execution counter."""

    def __init__(self, output: str, worker_id: str = "stub:counting"):
        from tabularium.engine.workers import MockWorker

        self._inner = MockWorker()
        self._output = output
        self._worker_id = worker_id
        self.executions = 0

    @property
    def descriptor(self):
        from tabularium.engine.workers import WorkerDescriptor

        return WorkerDescriptor(worker_id=self._worker_id,
                                category="LOCAL", capabilities=("STRUCTURED_OUTPUT",))

    def accepts(self, request):
        return self._inner.accepts(request)

    def execute(self, request):
        from tabularium.engine.workers import WorkerResult

        self.executions += 1
        return WorkerResult(request_id=request.request_id,
                            worker_id=self._worker_id,
                            status="COMPLETED", output=self._output)


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-dagcall-")
        self.root = Path(self.holder.name)
        from tabularium.init import initialize
        from tabularium.onboarding import build_authorizations, onboard_project

        self.assertEqual(initialize(self.root).status, "INITIALIZED")
        declarations = {
            "title": "DAG caller fixture",
            "purpose": "Exercise the --workflow flag",
            "scope": "Bounded caller project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-09-06T22:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        from tabularium import task as task_surface

        created = task_surface.task_create(
            self.root, title="Caller work", objective="Run me",
            type_="REVIEW")
        self.assertIsNone(created.code)

    def tearDown(self):
        self.holder.cleanup()


class RefusalMatrixTests(unittest.TestCase):
    def test_unknown_workflows_are_refused_before_any_gate(self):
        with tempfile.TemporaryDirectory(prefix="tabu-dagref-") as raw:
            for bad in ("multi", "swarm", "auto", "DAGraph", "none", 123):
                with self.subTest(workflow=bad):
                    result = run_instruction("work", Path(raw),
                                             workflow=bad)
                    self.assertEqual(result.status, "REFUSED")
                    self.assertEqual(result.code, WORKFLOW_SELECTION_INVALID)

    def test_workflow_spelling_is_case_and_space_tolerant(self):
        with tempfile.TemporaryDirectory(prefix="tabu-dagref-") as raw:
            for ok in ("dag", "DAG", " Dag ", "single", "SINGLE"):
                with self.subTest(workflow=ok):
                    # Passes workflow validation: refusal comes from a LATER
                    # gate (empty project), never the workflow gate.
                    result = run_instruction("work", Path(raw), workflow=ok)
                    self.assertNotEqual(result.code, WORKFLOW_SELECTION_INVALID)


class DefaultPathTests(WorkflowFixture):
    def test_default_is_single_and_unchanged(self):
        worker = CountingWorker("all good")
        result = run_instruction("Do work", self.root, adapter=worker)
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.pipeline.governance.get("workflow"), "single")
        self.assertNotIn("inter_agent_bus", result.pipeline.governance)
        self.assertEqual(worker.executions, 1)

    def test_explicit_single_matches_default(self):
        worker = CountingWorker("all good")
        result = run_instruction("Do work", self.root, adapter=worker,
                                 workflow="single")
        self.assertEqual(result.status, "COMPLETED")
        self.assertNotIn("inter_agent_bus", result.pipeline.governance)


class DagExecutionTests(WorkflowFixture):
    def test_dag_completes_with_bus_in_governance(self):
        worker = CountingWorker("all good")
        result = run_instruction("Do work", self.root, adapter=worker,
                                 workflow="dag")
        self.assertEqual(result.status, "COMPLETED")
        gov = result.pipeline.governance
        self.assertEqual(gov.get("workflow"), "dag")
        roles = [m.get("sender_role") for m in gov.get("inter_agent_bus", [])]
        self.assertIn("PLANNER", roles)
        self.assertIn("WORKER", roles)
        self.assertIn("AUDITOR", roles)
        self.assertEqual(result.pipeline.output, "all good")
        self.assertEqual(worker.executions, 1)

    def test_dag_veto_suspends_to_the_human_boundary(self):
        worker = CountingWorker("key sk-abc123 leaked", worker_id="stub:leaky")
        result = run_instruction("Risky work", self.root, adapter=worker,
                                 workflow="dag")
        self.assertEqual(result.status, "AWAITING_APPROVAL")
        self.assertIsNotNone(result.approval_request)
        request = result.approval_request
        reason = request.get("reason", "") if isinstance(request, dict) \
            else getattr(request, "reason", "")
        self.assertIn("veto", reason.casefold())

    def test_dag_approval_completes_without_rerunning_the_worker(self):
        worker = CountingWorker("key sk-abc123 leaked", worker_id="stub:leaky")
        pending = run_instruction("Risky work", self.root, adapter=worker,
                                  workflow="dag")
        self.assertEqual(pending.status, "AWAITING_APPROVAL")
        self.assertEqual(worker.executions, 1)
        resolution = make_resolution(pending.approval_request, "APPROVE",
                                     "human:user", "2026-09-06T22:00:00+00:00")
        done = continue_execution(self.root, resolution=resolution)
        self.assertEqual(done.status, "COMPLETED")
        self.assertEqual(done.pipeline.output, "key sk-abc123 leaked")
        self.assertEqual(worker.executions, 1)

    def test_dag_rejection_stays_rejected(self):
        worker = CountingWorker("key sk-abc123 leaked", worker_id="stub:leaky")
        pending = run_instruction("Risky work", self.root, adapter=worker,
                                  workflow="dag")
        resolution = make_resolution(pending.approval_request, "REJECT",
                                     "human:user", "2026-09-06T22:00:00+00:00")
        denied = continue_execution(self.root, resolution=resolution)
        self.assertEqual(denied.status, "REJECTED")
        self.assertEqual(worker.executions, 1)


class PipelineLevelTests(WorkflowFixture):
    def test_unknown_workflow_rejected_at_prepare(self):
        from tabularium.engine.execution import ExecutionPlan, start_execution
        from tabularium.engine.workers import MockWorker

        plan = ExecutionPlan(
            project_root=str(self.root), task_id="TASK-2026-0001",
            intent="work", execution_id="dagcall-bad",
            checkpoint_path=str(self.root / "t.db"),
            workflow="swarm", adapter=MockWorker())
        result = start_execution(plan)
        self.assertEqual(result.status, "PREPARATION_REJECTED")
        self.assertIn("WORKFLOW_SELECTION_INVALID", result.reason_codes)

    def test_lock_contention_fails_closed_without_hanging(self):
        from tabularium.engine.execution import ExecutionPlan, start_execution
        from tabularium.engine.runtime.lock import execution_collision_guard
        from tabularium.engine.workers import MockWorker

        plan = ExecutionPlan(
            project_root=str(self.root), task_id="TASK-2026-0001",
            intent="work", execution_id="dagcall-collision",
            checkpoint_path=str(self.root / "t.db"),
            workflow="dag", adapter=MockWorker())
        with execution_collision_guard(plan.checkpoint_path,
                                       plan.execution_id):
            result = start_execution(plan)
        self.assertEqual(result.status, "PREPARATION_REJECTED")
        self.assertIn("RUNTIME_START_FAILED", result.reason_codes)


class CliPlumbingTests(WorkflowFixture):
    def test_workflow_flag_parses_with_single_default(self):
        from tabularium.cli import build_parser

        args = build_parser().parse_args(["run", "work", str(self.root)])
        self.assertIsNone(args.workflow)
        args = build_parser().parse_args(
            ["run", "work", str(self.root), "--workflow", "dag"])
        self.assertEqual(args.workflow, "dag")

    def test_bogus_workflow_refused_from_the_cli(self):
        from tabularium.cli import main

        code = main(["run", "work", str(self.root),
                     "--workflow", "bogus"])
        self.assertEqual(code, 1)

    def test_dry_run_refuses_bogus_workflow(self):
        from tabularium import explain
        from tabularium.engine.workers import MockWorker

        flight = explain.preflight("work", self.root, adapter=MockWorker(),
                                   workflow="bogus", command="dry-run")
        self.assertFalse(flight.would_run)
        self.assertIn("WORKFLOW_SELECTION_INVALID", flight.reason_codes)


class TuiPassthroughTests(WorkflowFixture):
    def test_runner_rejects_bogus_and_runs_dag(self):
        from tabularium.engine.tui import runner

        bad = runner.start_run(self.root, "work", workflow="bogus")
        refused = runner.wait(bad, timeout=60)
        self.assertEqual(refused.status, "REFUSED")
        self.assertEqual(refused.code, WORKFLOW_SELECTION_INVALID)

        good = runner.start_run(self.root, "Do work",
                                adapter=CountingWorker("all good"),
                                workflow="dag")
        done = runner.wait(good, timeout=120)
        self.assertEqual(done.status, "COMPLETED")


if __name__ == "__main__":
    unittest.main()

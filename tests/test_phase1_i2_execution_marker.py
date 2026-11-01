"""Phase I (I2) regression: pre-runtime refusals preserve `.execution`.

Human decision D: a refused/pre-runtime execution attempt must NOT replace
the marker identifying the last execution that actually entered runtime.
The derived semantic boundary is pipeline result vocabulary: every
PipelineResult with status != PREPARATION_REJECTED came back through
`_from_runtime`, i.e. the runtime accepted the execution (`executed` is
True); a PREPARATION_REJECTED result never reached one.

The mutation counterpart (moving the marker write back before the runtime
boundary) is killed by `test_suspended_approval_survives_a_later_refused_run`
and `test_product_completion_updates_marker_then_refusal_preserves_it`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.execution import (
    NO_CONTINUATION,
    _checkpoint_path,
    continue_execution,
    make_resolution,
    remember_execution,
    run_instruction,
)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.execution import ExecutionPlan, inspect_execution, start_execution
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
    """MockWorker that counts executions; proves nothing ran pre-runtime."""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return super().execute(request)


try:
    import langgraph  # noqa: F401
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_RUNTIME = False

needs_runtime = unittest.skipUnless(HAS_RUNTIME, "LangGraph not installed")


class ExecutionMarkerPreservationTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i2-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "I2 fixture", "purpose": "Marker preservation",
            "scope": "Bounded regression project",
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

    def marker(self) -> Path:
        return _checkpoint_path(self.root).with_suffix(".execution")

    def marker_value(self):
        return self.marker().read_text(encoding="utf-8").strip()

    @staticmethod
    def hide_langgraph():
        hidden = {"langgraph", "langgraph.checkpoint",
                  "langgraph.checkpoint.sqlite", "langgraph.graph",
                  "langgraph.types"}
        return patch.dict(sys.modules, {name: None for name in hidden})

    def suspend_real_execution(self, execution_id):
        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(
            project_root=str(self.root), task_id="TASK-2026-0001",
            intent="real suspended work", execution_id=execution_id,
            checkpoint_path=str(db), adapter=MockWorker(),
            grant=ControlPlaneGrant(approval_required=True))
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        # The product writes exactly this locator after a runtime entry.
        remember_execution(self.root, execution_id)

    @needs_runtime
    def test_suspended_approval_survives_a_later_refused_run(self):
        self.suspend_real_execution("i2-real-execution")
        worker = CountingWorker()
        with self.hide_langgraph():
            refused = run_instruction("refused before runtime", self.root,
                                      task_id="TASK-2026-0001",
                                      adapter=worker)
        self.assertEqual(refused.status, "PREPARATION_REJECTED")
        self.assertEqual(refused.code, "RUNTIME_START_FAILED")
        self.assertFalse(refused.pipeline.executed)
        # No worker ran merely to allocate or write the locator.
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.marker_value(), "i2-real-execution")
        # ...and the previously suspended work remains continuable.
        pending = inspect_execution("i2-real-execution",
                                    str(_checkpoint_path(self.root)))
        resolution = make_resolution(pending.approval_request, "APPROVE",
                                     "human:user",
                                     "2026-01-01T00:00:00+00:00")
        resumed = continue_execution(self.root, resolution=resolution)
        self.assertEqual(resumed.status, "COMPLETED")

    @needs_runtime
    def test_product_completion_updates_marker_then_refusal_preserves_it(self):
        first = run_instruction("first real work", self.root,
                                task_id="TASK-2026-0001",
                                adapter=CountingWorker())
        self.assertEqual(first.status, "COMPLETED")
        self.assertEqual(self.marker_value(), first.pipeline.execution_id)
        with self.hide_langgraph():
            refused = run_instruction("second, refused attempt", self.root,
                                      task_id="TASK-2026-0001",
                                      adapter=CountingWorker())
        self.assertEqual(refused.status, "PREPARATION_REJECTED")
        self.assertEqual(self.marker_value(), first.pipeline.execution_id)
        again = run_instruction("third, real attempt", self.root,
                                task_id="TASK-2026-0001",
                                adapter=CountingWorker())
        self.assertEqual(again.status, "COMPLETED")
        self.assertEqual(self.marker_value(), again.pipeline.execution_id)

    @needs_runtime
    def test_context_rejection_shape_preserves_previous_marker(self):
        self.suspend_real_execution("i2-context-shape")
        with patch("tabularium.engine.execution.pipeline.load_context",
                   side_effect=ValueError("deterministic fixture failure")):
            refused = run_instruction("work", self.root,
                                      task_id="TASK-2026-0001",
                                      adapter=MockWorker())
        self.assertEqual(refused.status, "PREPARATION_REJECTED")
        self.assertEqual(refused.code, "PROJECT_NOT_FOUND")
        self.assertEqual(self.marker_value(), "i2-context-shape")

    @needs_runtime
    def test_refused_attempt_without_prior_execution_writes_no_marker(self):
        self.assertFalse(self.marker().exists())
        with self.hide_langgraph():
            refused = run_instruction("work", self.root,
                                      task_id="TASK-2026-0001",
                                      adapter=MockWorker())
        self.assertEqual(refused.status, "PREPARATION_REJECTED")
        self.assertFalse(self.marker().exists())

    @needs_runtime
    def test_runtime_entered_failure_still_owns_the_marker(self):
        """A runtime that ACCEPTED the execution owns the locator even when
        the work itself later FAILS — unlike a preparation refusal."""
        from tabularium.engine.workers.model import FAILED, WorkerResult

        class Failing(MockWorker):
            def execute(self, request):
                return WorkerResult(request_id=request.request_id,
                                    worker_id=self.descriptor.worker_id,
                                    status=FAILED,
                                    limitations=("fixture",))

        result = run_instruction("doomed work", self.root,
                                 task_id="TASK-2026-0001", adapter=Failing())
        self.assertEqual(result.status, "FAILED")
        self.assertTrue(self.marker().exists())
        self.assertEqual(self.marker_value(),
                         result.pipeline.execution_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

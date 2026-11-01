"""Phase I (I7) explain engine tests.

`explain_execution` runs pipeline stages 1-5 exactly as a real execution
would and stops before stage 6. Sentinels prove what must NEVER happen:
no worker invocation, no provider contact, no checkpoint, no `.execution`
marker, no authoritative write anywhere. Parity pins prove the facts it
returns are the SAME facts a real preparation produces.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.execution import (ExecutionPlan, PipelineResult,
                               PREFLIGHT_READY, PREFLIGHT_REFUSED,
                               explain_execution, start_execution)
from tabularium.engine.skills import ControlPlaneGrant
from tabularium.engine.workers import MockWorker
from tabularium.execution import run_instruction


def minimal_task(task_dir: Path, record_id: str = "TASK-2026-0001"):
    fm = {"schema_version": "1.10", "id": record_id, "title": "T",
          "objective": "o", "status": "IN_PROGRESS",
          "created_at": "2026-01-01T00:00:00Z", "created_by": "USER"}
    (task_dir / f"{record_id}.md").write_text(
        "---\n" + yaml.safe_dump(fm, sort_keys=False) + "---\n# T\n",
        encoding="utf-8")


class CountingWorker(MockWorker):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return super().execute(request)


class ExplainEngineFixture(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-i7-")
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
            "title": "I7 fixture", "purpose": "explain engine",
            "scope": "Bounded preflight project",
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
        self.workdir = Path(tempfile.mkdtemp(prefix="tabu-i7-db-"))
        self.addCleanup(self.cleanup_workdir)
        # The checkpoint location an execution WOULD use; explain must leave
        # it nonexistent.
        self.db = self.workdir / f"{abs(hash(str(self.root)))}.db"

    def cleanup_workdir(self):
        import shutil
        shutil.rmtree(self.workdir, ignore_errors=True)

    def tearDown(self):
        self.holder.cleanup()

    def plan(self, adapter=None, execution_id="i7-explain", **overrides):
        base = {"project_root": str(self.root),
                "task_id": "TASK-2026-0001",
                "intent": "explain this intent",
                "execution_id": execution_id,
                "checkpoint_path": str(self.db),
                "adapter": adapter if adapter is not None else CountingWorker()}
        base.update(overrides)
        return ExecutionPlan(**base)

    @staticmethod
    def ai_snapshot(root: Path) -> dict:
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted((root / ".ai").rglob("*")) if p.is_file()}


class ExplainParityTests(ExplainEngineFixture):
    """Explain returns the same facts the real preparation produces."""

    def test_ready_preflight_carries_worker_facts_and_identical_metrics(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("LangGraph not installed")
        worker = CountingWorker()
        preflight = explain_execution(self.plan(adapter=worker))
        self.assertEqual(preflight.status, PREFLIGHT_READY)
        self.assertTrue(preflight.would_run)
        self.assertEqual(preflight.worker_id, "mock:echo")
        self.assertEqual(preflight.worker_category, "LOCAL")
        self.assertIsNotNone(preflight.metrics)
        real = start_execution(self.plan(
            adapter=CountingWorker(), execution_id="i7-real-run"))
        self.assertEqual(real.status, "COMPLETED")
        self.assertEqual(preflight.metrics, real.metrics)
        # A completed run means approval was not required; preflight said so.
        self.assertIs(preflight.approval_required, False)

    def test_refused_preflight_uses_pipeline_reason_vocabulary_verbatim(self):
        worker = CountingWorker(worker_id="strict:one",
                                capabilities=("STRUCTURED_OUTPUT",))
        plan = self.plan(adapter=worker, required_capabilities=("NOPE",))
        preflight = explain_execution(plan)
        self.assertEqual(preflight.status, PREFLIGHT_REFUSED)
        self.assertFalse(preflight.would_run)
        real = start_execution(self.plan(
            adapter=CountingWorker(worker_id="strict:real",
                                   capabilities=("STRUCTURED_OUTPUT",)),
            required_capabilities=("NOPE",), execution_id="i7-real-refusal"))
        self.assertEqual(preflight.reason_codes, real.reason_codes)
        self.assertIn("WORKER_INCOMPATIBLE", preflight.reason_codes)


class ExplainSentinelTests(ExplainEngineFixture):
    def test_ready_explain_never_invokes_the_worker_or_writes_anything(self):
        before = self.ai_snapshot(self.root)
        worker = CountingWorker()
        preflight = explain_execution(self.plan(adapter=worker))
        self.assertEqual(preflight.status, PREFLIGHT_READY)
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.ai_snapshot(self.root), before)
        self.assertFalse(self.db.exists())
        self.assertFalse(list(self.workdir.iterdir()))

    def test_refused_explain_never_invokes_the_worker_or_writes_anything(self):
        before = self.ai_snapshot(self.root)
        worker = CountingWorker()
        plan = self.plan(adapter=worker, task_id="TASK-9999-0001")
        preflight = explain_execution(plan)
        self.assertEqual(preflight.status, PREFLIGHT_REFUSED)
        self.assertEqual(worker.calls, 0)
        self.assertEqual(self.ai_snapshot(self.root), before)
        self.assertFalse(list(self.workdir.iterdir()))

    def test_absent_runtime_dependency_predicts_the_real_refusal(self):
        """Truthfulness: a REAL run refuses RUNTIME_START_FAILED when the
        governed runtime is absent; preflight must predict exactly that —
        without entering stage 6 or invoking any worker."""
        hidden = {"langgraph", "langgraph.checkpoint",
                  "langgraph.checkpoint.sqlite", "langgraph.graph",
                  "langgraph.types"}
        worker = CountingWorker()
        with patch.dict(sys.modules, {name: None for name in hidden}):
            preflight = explain_execution(self.plan(adapter=worker))
        self.assertEqual(preflight.status, PREFLIGHT_REFUSED)
        self.assertEqual(preflight.reason_codes, ("RUNTIME_START_FAILED",))
        # ...and the prediction matches what the real run reports.
        try:
            import langgraph  # noqa: F401
            has_runtime = True
        except ImportError:
            has_runtime = False
        if not has_runtime:
            real = run_instruction("work", self.root,
                                   task_id="TASK-2026-0001")
            self.assertEqual(real.code, "RUNTIME_START_FAILED")
            self.assertEqual(worker.calls, 0)
            return
        from tabularium.execution import run_instruction
        saved = dict(sys.modules)
        try:
            for name in hidden:
                sys.modules[name] = None
            real = run_instruction("work", self.root,
                                   task_id="TASK-2026-0001")
        finally:
            sys.modules.update(saved)
        self.assertEqual(real.code, "RUNTIME_START_FAILED")
        self.assertFalse(real.pipeline.executed)

    def test_explain_creates_no_execution_marker(self):
        from tabularium.execution import _checkpoint_path
        worker = CountingWorker()
        explain_execution(self.plan(adapter=worker))
        marker = _checkpoint_path(self.root).with_suffix(".execution")
        self.assertFalse(marker.exists())


class PreflightContractTests(ExplainEngineFixture):
    def test_to_dict_is_stable_and_json_clean(self):
        preflight = explain_execution(self.plan())
        payload = preflight.to_dict()
        expected_keys = {"status", "would_run", "task_id", "reason_codes",
                         "worker_id", "worker_category", "resolved_model",
                         "approval_required", "metrics", "diagnostics"}
        self.assertEqual(set(payload), expected_keys)
        import json

        json.dumps(payload, ensure_ascii=False)  # must be serializable

    def test_api_worker_without_model_refuses_with_model_code(self):
        from tabularium.engine.workers.api import APIWorker
        worker = APIWorker(model_target=None)
        preflight = explain_execution(self.plan(adapter=worker))
        self.assertEqual(preflight.status, PREFLIGHT_REFUSED)
        self.assertIn("MODEL_TARGET_REQUIRED", preflight.reason_codes)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

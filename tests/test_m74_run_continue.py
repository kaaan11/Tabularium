"""Focused M7.4-Lite product-flow tests."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
import shutil
from unittest.mock import patch
from pathlib import Path

import yaml

from tabularium.execution import (NO_CONTINUATION, WORKER_SELECTION_REQUIRED,
                            _checkpoint_path, continue_execution,
                            make_resolution, remember_execution,
                            run_instruction, _select_worker)
from tabularium.diagnostics import doctor_project, status_project
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.execution import ExecutionPlan, inspect_execution, start_execution
from tabularium.engine.skills import ControlPlaneGrant
from tabularium.engine.workers import MockWorker
from tabularium.engine.workers import WorkerRequest
from tabularium.engine.workers.api import APIWorker, ModelTarget
from tabularium.engine.workers.cli import NativeCLIWorker


try:
    import langgraph  # noqa: F401
    HAS_RUNTIME = True
except ImportError:  # pragma: no cover - environment dependent
    HAS_RUNTIME = False

needs_runtime = unittest.skipUnless(HAS_RUNTIME, "LangGraph not installed")


class RunContinueTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-m74-")
        self.root = Path(self.holder.name)
        initialize(self.root)

    def tearDown(self):
        self.holder.cleanup()

    def onboard(self):
        declarations = {
            "title": "M7.4 fixture",
            "purpose": "Exercise the governed run entrypoint",
            "scope": "A bounded product-flow test project",
        }
        draft = onboard_project(self.root, declarations)
        self.assertIsNotNone(draft.proposal)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"),
        )
        self.assertEqual(result.status, "ONBOARDED")
        task_dir = self.root / ".ai" / "tasks"
        task_dir.mkdir(exist_ok=True)
        content = {
            "schema_version": "1.10", "id": "TASK-2026-0001",
            "title": "Review fixture", "objective": "Review the fixture",
            "status": "IN_PROGRESS", "created_at": "2026-01-01T00:00:00Z",
            "created_by": "USER", "plan": "Inspect the bounded fixture.",
        }
        (task_dir / "TASK-2026-0001.md").write_text(
            "---\n" + yaml.safe_dump(content, sort_keys=False) +
            "---\n# Task\n\nReview the fixture.\n", encoding="utf-8")

    @staticmethod
    def response(**kwargs):
        return {"choices": [{"message": {"content": "worker output"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4,
                           "total_tokens": 14}, "model": "test/model", **kwargs}

    def api_worker(self, completion=None):
        return APIWorker(model_target=ModelTarget("test/model"),
                         completion_fn=completion or (lambda **kwargs: self.response()))

    @staticmethod
    def snapshot(root):
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file()}

    @needs_runtime
    def test_run_uses_real_api_adapter_and_projection_without_project_mutation(self):
        self.onboard()
        before = self.snapshot(self.root)
        result = run_instruction("Review the fixture", self.root,
                                 adapter=self.api_worker())
        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.pipeline.worker_result["worker_id"], "api-default")
        self.assertIsNotNone(result.pipeline.metrics)
        self.assertGreaterEqual(result.pipeline.metrics.governed_context["estimated_tokens"], 0)
        self.assertGreaterEqual(result.pipeline.metrics.payload_size["estimated_tokens"], 0)
        self.assertEqual(self.snapshot(self.root), before)

    def test_run_refuses_uninitialized_project(self):
        shutil.rmtree(self.root / ".ai")
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, "NOT_INITIALIZED")

    def test_run_refuses_not_onboarded_project(self):
        result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, "NOT_ONBOARDED")

    def test_missing_worker_configuration_is_controlled(self):
        self.onboard()
        with patch("tabularium.execution.find_native_executable", return_value=None), \
             patch("tabularium.execution.find_opencode_executable", return_value=None):
            result = run_instruction("Review this", self.root)
        self.assertEqual(result.code, WORKER_SELECTION_REQUIRED)

    @needs_runtime
    def test_worker_failure_is_controlled(self):
        self.onboard()
        def fail(**kwargs):
            raise RuntimeError("provider unavailable")
        result = run_instruction("Review this", self.root,
                                 adapter=self.api_worker(fail))
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.code, "WORKER_EXECUTION_FAILED")
        self.assertNotIn("Traceback", result.message)

    @needs_runtime
    def test_continue_resumes_existing_approval_boundary(self):
        self.onboard()
        db = _checkpoint_path(self.root)
        plan = ExecutionPlan(
            project_root=str(self.root), task_id="TASK-2026-0001",
            intent="Review this", execution_id="m74-approval",
            checkpoint_path=str(db), adapter=MockWorker(),
            grant=ControlPlaneGrant(approval_required=True),
        )
        started = start_execution(plan)
        self.assertEqual(started.status, "AWAITING_APPROVAL")
        remember_execution(self.root, "m74-approval")
        pending = inspect_execution("m74-approval", str(db))
        resolution = make_resolution(pending.approval_request, "APPROVE",
                                     "human:user",
                                     "2026-08-19T12:00:00+00:00")
        result = continue_execution(self.root, resolution=resolution)
        self.assertEqual(result.status, "COMPLETED")

    def test_continue_without_checkpoint_is_controlled(self):
        self.onboard()
        result = continue_execution(self.root)
        self.assertEqual(result.code, NO_CONTINUATION)

    @needs_runtime
    def test_status_and_doctor_remain_read_only_after_run(self):
        self.onboard()
        before_status = status_project(self.root).to_dict()
        before_doctor = doctor_project(self.root).to_dict()
        run_instruction("Review this", self.root, adapter=self.api_worker())
        self.assertEqual(status_project(self.root).to_dict(), before_status)
        self.assertEqual(doctor_project(self.root).to_dict(), before_doctor)

    def test_explicit_worker_selection_is_deterministic_and_has_no_fallback(self):
        with patch("tabularium.execution.find_native_executable", return_value=None):
            selected, error = _select_worker(self.root, "native", "", None)
        self.assertIsNone(selected)
        self.assertEqual(error.code, "WORKER_UNAVAILABLE")

        selected, error = _select_worker(self.root, "api", "test/model", None)
        self.assertIsNone(error)
        self.assertEqual(selected.descriptor.worker_id, "api-default")

        selected, error = _select_worker(self.root, "unknown", "", None)
        self.assertIsNone(selected)
        self.assertEqual(error.code, "WORKER_SELECTION_INVALID")

    def test_native_worker_receives_only_the_worker_request_payload(self):
        with tempfile.TemporaryDirectory(prefix="tabu-native-test-") as scratch:
            script = Path(scratch) / "native.py"
            script.write_text(
                "import json, sys\n"
                "sys.stdin.read()\n"
                "print('api_key=SECRET_SHOULD_NOT_SURFACE', file=sys.stderr)\n"
                "print(json.dumps({'type':'item.completed','item':{"
                "'type':'agent_message','text':'NATIVE_OK'}}))\n",
                encoding="utf-8")

            class FixtureNative(NativeCLIWorker):
                def build_argv(self, request):
                    return [sys.executable, str(script)]

            request = WorkerRequest(
                request_id="native-1", intent="return a safe answer",
                payload={"required": {"task": {"id": "TASK-2026-0001"}}},
                task_id="TASK-2026-0001")
            result = FixtureNative(cwd=scratch).execute(request)
            self.assertEqual(result.status, "COMPLETED")
            self.assertEqual(result.worker_id, "codex-cli")
            self.assertIn("NATIVE_OK", result.output)
            self.assertNotIn(str(self.root), result.output)
            self.assertNotIn("SECRET_SHOULD_NOT_SURFACE", repr(result))

    @needs_runtime
    def test_native_continuation_keeps_worker_identity(self):
        self.onboard()
        with tempfile.TemporaryDirectory(prefix="tabu-native-test-") as scratch:
            script = Path(scratch) / "native.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'type':'item.completed','item':{"
                "'type':'agent_message','text':'NATIVE_OK'}}))\n",
                encoding="utf-8")

            class FixtureNative(NativeCLIWorker):
                def build_argv(self, request):
                    return [sys.executable, str(script)]

            db = _checkpoint_path(self.root)
            plan = ExecutionPlan(
                project_root=str(self.root), task_id="TASK-2026-0001",
                intent="Review this", execution_id="m74-native-approval",
                checkpoint_path=str(db), adapter=FixtureNative(cwd=scratch),
                grant=ControlPlaneGrant(approval_required=True),
            )
            started = start_execution(plan)
            self.assertEqual(started.status, "AWAITING_APPROVAL")
            self.assertEqual(started.worker_result["worker_id"], "codex-cli")
            remember_execution(self.root, "m74-native-approval")
            pending = inspect_execution("m74-native-approval", str(db))
            resolution = make_resolution(
                pending.approval_request, "APPROVE", "human:user",
                "2026-08-19T12:00:00+00:00")
            continued = continue_execution(self.root, resolution=resolution)
            self.assertEqual(continued.status, "COMPLETED")
            self.assertEqual(continued.pipeline.worker_result["worker_id"],
                             "codex-cli")


class RuntimeDependencyGuidanceTests(unittest.TestCase):
    """D3: an absent governed-runtime dependency keeps its stable machine code
    but gains bounded guidance quoting the documented install syntax."""

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-d3-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "D3 fixture",
            "purpose": "Exercise runtime-absence guidance",
            "scope": "A bounded product-flow test project",
        }
        draft = onboard_project(self.root, declarations)
        result = onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"))
        self.assertEqual(result.status, "ONBOARDED")
        task_dir = self.root / ".ai" / "tasks"
        task_dir.mkdir(exist_ok=True)
        content = {
            "schema_version": "1.10", "id": "TASK-2026-0001",
            "title": "T", "objective": "o", "status": "IN_PROGRESS",
            "created_at": "2026-01-01T00:00:00Z", "created_by": "USER",
            "plan": "p",
        }
        (task_dir / "TASK-2026-0001.md").write_text(
            "---\n" + yaml.safe_dump(content, sort_keys=False) +
            "---\n# Task\n\nbody\n", encoding="utf-8")

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def hide_langgraph():
        """Force the lazy runtime loader to see the dependency as absent."""
        hidden = {"langgraph", "langgraph.checkpoint",
                  "langgraph.checkpoint.sqlite", "langgraph.graph",
                  "langgraph.types"}
        return patch.dict(sys.modules,
                          {name: None for name in hidden})

    def test_missing_runtime_names_documented_install_syntax(self):
        with self.hide_langgraph():
            result = run_instruction("Review this", self.root,
                                     adapter=MockWorker())
        self.assertEqual(result.status, "PREPARATION_REJECTED")
        self.assertEqual(result.code, "RUNTIME_START_FAILED")
        self.assertIn('pip install -e ".[runtime]"', result.message)
        self.assertIn("tabu doctor", result.message)
        self.assertNotIn("Traceback", result.message)
        self.assertLess(len(result.message), 1000)
        # the refusal happened at runtime start; no worker ever executed
        self.assertIsNotNone(result.pipeline)
        self.assertFalse(result.pipeline.executed)

    def test_other_runtime_failures_keep_generic_guidance(self):
        from tabularium.execution import _from_pipeline
        from tabularium.engine.execution import PipelineResult
        result = _from_pipeline(
            self.root, PipelineResult(
                status="PREPARATION_REJECTED", execution_id="d3-exec",
                task_id="TASK-2026-0001",
                reason_codes=("RUNTIME_START_FAILED",),
                diagnostics=({"code": "RUNTIME_START_FAILED",
                              "detail": "ValueError: unusable checkpoint"},)))
        self.assertEqual(result.code, "RUNTIME_START_FAILED")
        self.assertNotIn("pip install", result.message)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

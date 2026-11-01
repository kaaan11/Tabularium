"""GP2B / M5 pin: model data never reaches Native execution semantics.

Phase F independent review mutation M5 survived the suite: handing
model_target to CLI_CODING executions would put the model into the native
execution's request-data contract, and nothing observed that level. The
stable isolation contract pinned here, at each seam where model data could
leak into a Native run:

- product layer: a native run's ExecutionPlan.model_target stays None even
  when --model/TABU_MODEL was supplied (the model belongs to API/model
  execution only);
- prepared-request layer: the WorkerRequest built by the governed pipeline
  and handed to the runtime carries NO model data;
- adapter boundary: the request reconstructed by the runtime and delivered
  across the frozen WorkerAdapter boundary also carries none — so no single
  upstream regression can silently put a model into Native execution input;
- the operator is still warned that --model does not apply to Native.

All observations drive the REAL production path (run_instruction -> M4.2
pipeline -> runtime) with capture wrappers around the existing seams plus a
CLI_CODING capture adapter; assertions are on semantic fields, not internal
object layout.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.execution import MODEL_IGNORED_BY_NATIVE_WARNING, run_instruction
from tabularium.engine.execution import start_execution as real_start_execution
from tabularium.engine.runtime import LangGraphRuntime
from tabularium.engine.workers.model import (CLI_CODING, CANCELLABLE, COMPLETED,
                                   STRUCTURED_OUTPUT, STREAMS_OUTPUT,
                                   WorkerDescriptor, WorkerRequest,
                                   WorkerResult)


class CapturingNativeAdapter:
    """CLI_CODING adapter recording the request the runtime actually hands over."""

    def __init__(self):
        self._descriptor = WorkerDescriptor(
            worker_id="capturing-native", category=CLI_CODING,
            capabilities=(STREAMS_OUTPUT, STRUCTURED_OUTPUT, CANCELLABLE))
        self.captured = None

    @property
    def descriptor(self):
        return self._descriptor

    def accepts(self, request):
        return True, ()

    def execute(self, request):
        self.captured = request
        return WorkerResult(request.request_id, self._descriptor.worker_id,
                            COMPLETED, output="CAPTURED")

    def cancel(self, request_id):
        return False


class NativeModelIsolationTests(unittest.TestCase):
    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-g-m5-")
        self.root = Path(self.holder.name)
        self._prepare_project()

    def tearDown(self):
        self.holder.cleanup()

    def _prepare_project(self):
        from tabularium.init import initialize
        from tabularium.onboarding import build_authorizations, onboard_project
        initialize(self.root)
        declarations = {
            "title": "G fixture",
            "purpose": "Pin native request-data isolation from model data",
            "scope": "A bounded offline fixture",
        }
        draft = onboard_project(self.root, declarations)
        onboard_project(
            self.root, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"))
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

    def _run_observed(self, *, model_arg=None, env_model=None):
        """Run one native instruction observing every isolation seam."""
        spy = CapturingNativeAdapter()
        seen = {"plans": [], "prepared_requests": []}

        def observing_start(plan, *args, **kwargs):
            seen["plans"].append(plan)
            return real_start_execution(plan, *args, **kwargs)

        original_runtime_start = LangGraphRuntime.start

        def observing_runtime_start(runtime_self, execution_id, request,
                                    *args, **kwargs):
            seen["prepared_requests"].append(request)
            return original_runtime_start(runtime_self, execution_id,
                                          request, *args, **kwargs)

        env = dict(os.environ)
        env.pop("TABU_MODEL", None)
        env.pop("TABU_NATIVE_EXECUTABLE", None)
        if env_model is not None:
            env["TABU_MODEL"] = env_model
        with patch.dict(os.environ, env, clear=True), \
                patch("tabularium.execution.start_execution",
                      side_effect=observing_start), \
                patch.object(LangGraphRuntime, "start",
                             observing_runtime_start):
            result = run_instruction("Review this", self.root,
                                     model=model_arg, adapter=spy)
        return result, seen["plans"], seen["prepared_requests"], spy.captured

    def test_explicit_model_never_reaches_native_semantics(self):
        result, plans, prepared, delivered = self._run_observed(
            model_arg="test/model")
        self.assertEqual(result.status, "COMPLETED")
        # Human decision preserved: warn about --model, proceed unchanged.
        self.assertEqual(result.warnings,
                         (MODEL_IGNORED_BY_NATIVE_WARNING,))
        # Product layer: the native plan carries no model choice.
        self.assertEqual(len(plans), 1)
        self.assertIsNone(plans[0].model_target)
        # Prepared request: no model data enters the governed request.
        self.assertEqual(len(prepared), 1)
        self.assertIsInstance(prepared[0], WorkerRequest)
        self.assertEqual(prepared[0].model_requirements, {})
        # Adapter boundary: what the worker receives is also model-free.
        self.assertIsNotNone(delivered)
        self.assertEqual(delivered.model_requirements, {})

    def test_environment_model_never_reaches_native_semantics(self):
        result, plans, prepared, delivered = self._run_observed(
            env_model="env/model")
        self.assertEqual(result.warnings,
                         (MODEL_IGNORED_BY_NATIVE_WARNING,))
        self.assertIsNone(plans[0].model_target)
        self.assertEqual(prepared[0].model_requirements, {})
        self.assertEqual(delivered.model_requirements, {})

    def test_no_model_keeps_every_seam_model_free(self):
        result, plans, prepared, delivered = self._run_observed()
        self.assertEqual(result.warnings, ())
        self.assertIsNone(plans[0].model_target)
        self.assertEqual(prepared[0].model_requirements, {})
        self.assertEqual(delivered.model_requirements, {})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

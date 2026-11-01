"""F5: an explicitly supplied model combined with the Native worker warns.

Human decision (Phase F): --worker native + explicit --model must WARN —
not silently ignore, not reject. Native execution proceeds unchanged with
its exit status intact; the warning states that --model applies to
API/model execution only. No warning is emitted when no model was supplied,
or when the API worker receives a relevant model.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from tabularium.cli import main as cli_main
from tabularium.execution import (MODEL_IGNORED_BY_NATIVE_WARNING, ProductResult,
                            run_instruction)
from tabularium.init import initialize
from tabularium.onboarding import build_authorizations, onboard_project
from tabularium.engine.workers import MockWorker
from tabularium.engine.workers.api import APIWorker, ModelTarget
from tabularium.engine.workers.cli import NativeCLIWorker


def api_worker(**kwargs) -> APIWorker:
    def completion(**kw):
        return {"choices": [{"message": {"content": "done"}}],
                "usage": {}, "model": "test/model"}
    return APIWorker(model_target=ModelTarget("test/model"),
                     completion_fn=completion, **kwargs)


class ModelWarningSelectionTests(unittest.TestCase):
    """Warning decision at the product layer (no CLI process needed)."""

    def setUp(self):
        self.holder = tempfile.TemporaryDirectory(prefix="tabu-f5-")
        self.root = Path(self.holder.name)
        initialize(self.root)
        declarations = {
            "title": "F5 fixture",
            "purpose": "Exercise the native --model warning",
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

    def tearDown(self):
        self.holder.cleanup()

    @staticmethod
    def native_adapter() -> NativeCLIWorker:
        return NativeCLIWorker(executable="/nonexistent/codex",
                               cwd=tempfile.gettempdir())

    def test_native_with_explicit_model_warns_but_completes(self):
        result = run_instruction("Review this", self.root, model="test/model",
                                 adapter=self.native_adapter())
        self.assertEqual(result.warnings, (MODEL_IGNORED_BY_NATIVE_WARNING,))
        self.assertIn("--model applies to API/model execution only",
                      result.warnings[0])
        self.assertIn("native worker does not use it", result.warnings[0])

    def test_native_without_model_does_not_warn(self):
        result = run_instruction("Review this", self.root,
                                 adapter=self.native_adapter())
        self.assertEqual(result.warnings, ())

    def test_api_worker_with_relevant_model_does_not_warn(self):
        result = run_instruction("Review this", self.root,
                                 adapter=api_worker(), model="test/model")
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.status, "COMPLETED")

    def test_non_native_injected_worker_does_not_warn(self):
        result = run_instruction("Review this", self.root, adapter=MockWorker(),
                                 model="test/model")
        self.assertEqual(result.warnings, ())

    def test_model_via_environment_is_also_surfaced_for_native(self):
        env = dict(os.environ, TABU_MODEL="test/model")
        with patch.dict(os.environ, env, clear=True):
            result = run_instruction("Review this", self.root,
                                     adapter=self.native_adapter())
        self.assertEqual(result.warnings, (MODEL_IGNORED_BY_NATIVE_WARNING,))

    def test_warning_survives_a_failed_native_execution(self):
        result = run_instruction("Review this", self.root, model="test/model",
                                 adapter=self.native_adapter())
        # /nonexistent/codex cannot spawn: execution fails, exit semantics
        # unchanged, and the operator still learns why --model did nothing.
        self.assertNotEqual(result.status, "COMPLETED")
        self.assertEqual(result.warnings, (MODEL_IGNORED_BY_NATIVE_WARNING,))
        self.assertNotEqual(result.code, "COMPLETED")

    def test_product_result_default_keeps_existing_construction_sites_valid(
            self):
        result = ProductResult("REFUSED", "ANY_CODE", "message", "/tmp")
        self.assertEqual(result.warnings, ())


class ModelWarningCliTests(unittest.TestCase):
    """End-to-end through the real CLI using a fixture native executable."""

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="tabu-f5-cli-")
        self.bin_dir = Path(self.scratch.name) / "bin"
        self.bin_dir.mkdir()
        self.fixture = self.bin_dir / "codex"
        self.fixture.write_text(
            "#!/bin/sh\n"
            "cat > /dev/null\n"
            f"{sys.executable} -c \"import json; "
            "print(json.dumps({'type': 'item.completed', 'item': "
            "{'type': 'agent_message', 'text': 'NATIVE_OK'}}))\"\n",
            encoding="utf-8")
        self.fixture.chmod(self.fixture.stat().st_mode | stat.S_IEXEC)
        self.project = Path(self.scratch.name) / "proj"
        self.project.mkdir()
        initialize(self.project)
        declarations = {
            "title": "F5 CLI fixture",
            "purpose": "Exercise the native --model warning end to end",
            "scope": "A bounded offline fixture",
        }
        draft = onboard_project(self.project, declarations)
        onboard_project(
            self.project, declarations,
            build_authorizations(draft.proposal, "human:user",
                                 "2026-08-19T12:00:00+00:00"))
        task_dir = self.project / ".ai" / "tasks"
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
        self.scratch.cleanup()

    def run_cli(self, *argv: str):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli_main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_cli_run_warns_and_exits_zero_with_native_output(self):
        env = dict(os.environ, TABU_NATIVE_EXECUTABLE=str(self.fixture))
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self.run_cli(
                "run", "Review this", str(self.project),
                "--worker", "native", "--model", "test/model")
        self.assertEqual(code, 0)
        self.assertIn("NATIVE_OK", out)
        self.assertIn(MODEL_IGNORED_BY_NATIVE_WARNING, err)
        self.assertIn("Warning:", err)

    def test_cli_run_without_model_stays_silent(self):
        env = dict(os.environ, TABU_NATIVE_EXECUTABLE=str(self.fixture))
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            code, out, err = self.run_cli(
                "run", "Review this", str(self.project),
                "--worker", "native")
        self.assertEqual(code, 0)
        self.assertIn("NATIVE_OK", out)
        self.assertNotIn("--model", err)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

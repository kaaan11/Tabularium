"""Tests for OpenCode CLI worker adapter (DEC-2026-0007)."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tabularium.engine.projection.model import ProjectedPayload
from tabularium.engine.workers import WorkerRequest
from tabularium.engine.workers.cli.opencode_worker import (
    OPENCODE_WORKER_ID,
    OpenCodeCLIWorker,
    find_opencode_executable,
)
from tabularium.engine.workers.cli.bounded_process import Capture


class TestOpenCodeWorker(unittest.TestCase):
    def setUp(self):
        self.payload = ProjectedPayload(sections=(("required", {"intent": "test"}),))
        self.request = WorkerRequest(
            request_id="req-opencode-01",
            intent="Run inspection",
            payload=self.payload,
            task_id="TASK-2026-0009",
        )

    def test_find_opencode_executable_contract(self):
        # Host-independent: discovery returns either nothing or an
        # opencode path — never a non-opencode binary.
        exe = find_opencode_executable()
        self.assertTrue(exe is None or exe.endswith("opencode"))

    def test_env_override_selects_configured_executable(self):
        with tempfile.TemporaryDirectory(prefix="tabu-opencode-exe-") as raw:
            fake = Path(raw) / "opencode"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            with patch.dict(os.environ, {"TABU_OPENCODE_EXECUTABLE": str(fake)}):
                self.assertEqual(find_opencode_executable(), str(fake))

    def test_worker_descriptor(self):
        worker = OpenCodeCLIWorker()
        self.assertEqual(worker.descriptor.worker_id, OPENCODE_WORKER_ID)
        self.assertEqual(worker.descriptor.category, "CLI_CODING")

    def test_accepts_valid_request(self):
        worker = OpenCodeCLIWorker()
        accepted, reasons = worker.accepts(self.request)
        self.assertTrue(accepted)
        self.assertEqual(reasons, ())

    def test_refuses_empty_payload(self):
        worker = OpenCodeCLIWorker()
        bad_req = WorkerRequest(
            request_id="req-02",
            intent="test",
            payload=None,
        )
        accepted, reasons = worker.accepts(bad_req)
        self.assertFalse(accepted)
        self.assertIn("EMPTY_PAYLOAD", reasons)

    def test_build_argv(self):
        worker = OpenCodeCLIWorker(executable="/bin/opencode", model="openrouter/anthropic/claude-3.5-sonnet")
        argv = worker.build_argv("Hello world")
        self.assertEqual(argv[0], "/bin/opencode")
        self.assertEqual(argv[1], "run")
        self.assertEqual(argv[2], "Hello world")
        self.assertIn("--format", argv)
        self.assertIn("json", argv)
        self.assertIn("--pure", argv)
        self.assertIn("-m", argv)
        self.assertIn("openrouter/anthropic/claude-3.5-sonnet", argv)

    @patch("tabularium.engine.workers.cli.opencode_worker.BoundedProcess")
    def test_execute_parses_json_stream(self, mock_popen):
        mock_proc = MagicMock()
        mock_proc.run.return_value = Capture("EXITED", 0,
            '{"type":"step_start","timestamp":1000,"part":{}}\n'
            '{"type":"text","part":{"text":"Hello from "}}\n'
            '{"type":"text","part":{"text":"OpenCode!"}}\n'
            '{"type":"step_finish","part":{"tokens":{"input":10,"output":5,"total":15}}}\n',
        )
        mock_popen.return_value = mock_proc

        # Explicit fake executable that exists: Popen is mocked, so nothing
        # spawns and the test does not depend on an opencode binary on
        # the host (execute() only requires an existing file).
        with tempfile.TemporaryDirectory(prefix="tabu-opencode-exec-") as raw:
            fake = Path(raw) / "opencode"
            fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            worker = OpenCodeCLIWorker(executable=str(fake))
            result = worker.execute(self.request)

        self.assertEqual(result.status, "COMPLETED")
        self.assertEqual(result.output, "Hello from OpenCode!")
        self.assertEqual(result.usage.get("total_tokens"), 15)
        self.assertEqual(result.worker_id, OPENCODE_WORKER_ID)


if __name__ == "__main__":
    unittest.main()

"""v0.2.1 regression: native-worker availability follows the argv, not PATH.

The defect this pins kept CI red for the whole v0.2 program. `execute()`
asked whether PATH discovery had found a `codex` binary, while the command
that would actually run came from `build_argv` — so a subclass supplying its
own argv (the documented fixture seam) was refused for lacking an unrelated
binary, and the suite passed only on machines that happened to have one.

Both directions are pinned here, and both are independent of the host: the
discovery seam is patched in each test rather than inherited from PATH.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tabularium.engine.workers import WorkerRequest
from tabularium.engine.workers.cli import NativeCLIWorker

DISCOVERY = "tabularium.engine.workers.cli.worker.find_native_executable"


def _request() -> WorkerRequest:
    return WorkerRequest(
        request_id="v021-seam", intent="x",
        payload={"required": {"task": {"id": "TASK-2026-0001"}}},
        task_id="TASK-2026-0001")


class NativeWorkerSeamTests(unittest.TestCase):
    def test_overridden_argv_runs_without_a_discovered_executable(self):
        with patch(DISCOVERY, return_value=None), \
                tempfile.TemporaryDirectory(prefix="tabu-v021-") as scratch:
            script = Path(scratch) / "fixture.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({'type': 'item.completed', 'item':"
                " {'type': 'agent_message', 'text': 'SEAM_OK'}}))\n",
                encoding="utf-8")

            class Fixture(NativeCLIWorker):
                def build_argv(self, request):
                    return [sys.executable, str(script)]

            worker = Fixture(cwd=scratch)
            # Discovery genuinely found nothing: availability must come from
            # the argv alone.
            self.assertIsNone(worker.executable)
            result = worker.execute(_request())

        self.assertEqual(result.status, "COMPLETED")
        self.assertIn("SEAM_OK", result.output)

    def test_base_worker_without_an_executable_still_refuses(self):
        """The refusal every real caller depends on is unchanged."""
        with patch(DISCOVERY, return_value=None):
            worker = NativeCLIWorker()
            self.assertIsNone(worker.executable)
            result = worker.execute(_request())

        self.assertEqual(result.status, "FAILED")
        self.assertEqual([d.get("code") for d in result.diagnostics],
                         ["NATIVE_WORKER_UNAVAILABLE"])
        self.assertEqual(result.output, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

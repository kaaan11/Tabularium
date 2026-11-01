"""Tests for the durable tool replay store (DRL-001).

Validates that ToolAuthorization bindings:
1. Cannot be replayed across process boundaries when backed by JsonToolReplayStore.
2. Cannot live inside `.ai/` (fails with ValueError).
3. Fail closed if the replay store is corrupted.
4. Support concurrent locking across processes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tabularium.engine.skills.model import EffectiveConstraints  # noqa: E402
from tabularium.engine.tools import (  # noqa: E402
    JsonToolReplayStore,
    ToolAuthorization,
    ToolRequest,
    ToolReplayStoreFailure,
    run_tool_cycle,
)
from tabularium.engine.tools import model as TM  # noqa: E402
from tabularium.engine.tools.local_fixture import FixtureWriteTool  # noqa: E402


class TestDurableToolReplayStore(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tool-replay-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store_path = self.tmp / "replays" / "tool_replays.json"
        self.tool_root = self.tmp / "tool_work"
        self.tool_root.mkdir(parents=True)
        self.write_tool = FixtureWriteTool(self.tool_root)

    def write_request(self, name="out.txt", content="payload") -> ToolRequest:
        return ToolRequest(
            tool_request_id="tr-durable-1",
            execution_id="exec-durable-1",
            task_id="TASK-2026-0001",
            tool_class=TM.WRITE,
            tool_name="fixture_file_write",
            operation="write_text",
            arguments={"path": name, "content": content},
            requested_scope={"root": str(self.tool_root)},
            reason="durable tool replay test",
        )

    def authorization(self, request: ToolRequest) -> ToolAuthorization:
        return ToolAuthorization(
            tool_request_id=request.tool_request_id,
            execution_id=request.execution_id,
            tool_name=request.tool_name,
            operation=request.operation,
            arguments_digest=request.arguments_digest,
            authorized_by="human:user",
            authorized_at="2026-08-31T12:00:00Z",
            scope=request.requested_scope,
            binding=request.binding(),
        )

    def test_cannot_live_inside_ai(self):
        project = self.tmp / "project"
        ai_dir = project / ".ai"
        ai_dir.mkdir(parents=True)
        inside_path = ai_dir / "tool_replay.json"
        with self.assertRaises(ValueError):
            JsonToolReplayStore(inside_path, project_root=project)

    def test_single_process_durable_replay_prevention(self):
        store = JsonToolReplayStore(self.store_path)
        req = self.write_request()
        auth = self.authorization(req)
        constraints = EffectiveConstraints(
            worker_categories=("LOCAL",),
            context_classes=(),
            tool_classes=(TM.WRITE,),
            approval_required=False,
            persistence="NONE",
            output_contract="TEXT",
        )

        # First run succeeds
        res1 = run_tool_cycle(req, self.write_tool, constraints, auth, replay_store=store)
        self.assertEqual(res1.status, TM.EXECUTED)
        self.assertTrue(store.is_consumed(req.binding()))

        # Second run with same auth and fresh used_bindings fails via replay_store
        res2 = run_tool_cycle(req, self.write_tool, constraints, auth, used_bindings=(), replay_store=store)
        self.assertEqual(res2.status, TM.DENIED)
        self.assertIn(TM.TOOL_AUTHORIZATION_ALREADY_USED, res2.reason_codes)

    def test_cross_process_durable_replay_prevention(self):
        """Proves that a fresh Python process reading the same store denies the reused authorization."""
        store = JsonToolReplayStore(self.store_path)
        req = self.write_request()
        auth = self.authorization(req)
        constraints = EffectiveConstraints(
            worker_categories=("LOCAL",),
            context_classes=(),
            tool_classes=(TM.WRITE,),
            approval_required=False,
            persistence="NONE",
            output_contract="TEXT",
        )

        # Process 1 executes the tool
        res1 = run_tool_cycle(req, self.write_tool, constraints, auth, replay_store=store)
        self.assertEqual(res1.status, TM.EXECUTED)

        # Process 2 runs in a completely separate interpreter
        script = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
from tabularium.engine.tools import JsonToolReplayStore, run_tool_cycle, ToolRequest, ToolAuthorization
from tabularium.engine.tools.local_fixture import FixtureWriteTool
from tabularium.engine.skills.model import EffectiveConstraints
import tabularium.engine.tools.model as TM

store = JsonToolReplayStore({str(self.store_path)!r})
tool = FixtureWriteTool({str(self.tool_root)!r})
req = ToolRequest(
    tool_request_id={req.tool_request_id!r},
    execution_id={req.execution_id!r},
    task_id={req.task_id!r},
    tool_class=TM.WRITE,
    tool_name="fixture_file_write",
    operation="write_text",
    arguments={{"path": "out.txt", "content": "payload"}},
    requested_scope={{"root": {str(self.tool_root)!r}}},
    reason="cross process test",
)
auth = ToolAuthorization.from_dict({auth.to_dict()!r})
constraints = EffectiveConstraints(
    worker_categories=("LOCAL",), context_classes=(),
    tool_classes=(TM.WRITE,), approval_required=False,
    persistence="NONE", output_contract="TEXT"
)

result = run_tool_cycle(req, tool, constraints, auth, used_bindings=(), replay_store=store)
assert result.status == TM.DENIED, f"expected DENIED, got {{result.status}}"
assert TM.TOOL_AUTHORIZATION_ALREADY_USED in result.reason_codes
print("CROSS_PROCESS_REPLAY_DENIED")
"""
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, f"STDOUT: {proc.stdout}\nSTDERR: {proc.stderr}")
        self.assertIn("CROSS_PROCESS_REPLAY_DENIED", proc.stdout)

    def test_broken_store_fails_closed(self):
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text("NOT_VALID_JSON{", encoding="utf-8")
        store = JsonToolReplayStore(self.store_path)
        req = self.write_request()
        auth = self.authorization(req)
        constraints = EffectiveConstraints(
            worker_categories=("LOCAL",), context_classes=(),
            tool_classes=(TM.WRITE,), approval_required=False,
            persistence="NONE", output_contract="TEXT",
        )
        res = run_tool_cycle(req, self.write_tool, constraints, auth, replay_store=store)
        self.assertEqual(res.status, TM.DENIED)
        self.assertIn(TM.TOOL_GATE_FAILED, res.reason_codes)

    def test_concurrent_processes_get_exactly_one_execution(self):
        """Proves that 4 concurrent processes trying to use the same authorization get exactly 1 EXECUTED."""
        req = self.write_request()
        auth = self.authorization(req)

        runner_script = f"""
import sys, time
sys.path.insert(0, {str(ROOT)!r})
from tabularium.engine.tools import JsonToolReplayStore, run_tool_cycle, ToolRequest, ToolAuthorization
from tabularium.engine.tools.local_fixture import FixtureWriteTool
from tabularium.engine.skills.model import EffectiveConstraints
import tabularium.engine.tools.model as TM

class SlowWriteTool(FixtureWriteTool):
    def execute(self, request):
        time.sleep(0.3)
        return super().execute(request)

store = JsonToolReplayStore({str(self.store_path)!r})
tool = SlowWriteTool({str(self.tool_root)!r})
req = ToolRequest(
    tool_request_id={req.tool_request_id!r},
    execution_id={req.execution_id!r},
    task_id={req.task_id!r},
    tool_class=TM.WRITE,
    tool_name="fixture_file_write",
    operation="write_text",
    arguments={{"path": "out.txt", "content": "payload"}},
    requested_scope={{"root": {str(self.tool_root)!r}}},
    reason="concurrent test",
)
auth = ToolAuthorization.from_dict({auth.to_dict()!r})
constraints = EffectiveConstraints(
    worker_categories=("LOCAL",), context_classes=(),
    tool_classes=(TM.WRITE,), approval_required=False,
    persistence="NONE", output_contract="TEXT"
)

result = run_tool_cycle(req, tool, constraints, auth, used_bindings=(), replay_store=store)
print(result.status)
"""
        import concurrent.futures
        def run_one():
            proc = subprocess.run([sys.executable, "-c", runner_script], capture_output=True, text=True)
            return proc.stdout.strip()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            futs = [ex.submit(run_one) for _ in range(4)]
            results = [f.result() for f in futs]

        executed_count = sum(1 for r in results if r == TM.EXECUTED)
        self.assertEqual(executed_count, 1, f"Expected exactly 1 EXECUTED, got {results}")

    def test_store_without_a_claim_api_refuses(self):
        """A replay defense that cannot be exercised must not silently pass."""
        class NoClaimApiStore:
            def is_consumed(self, binding):
                return False

        req = self.write_request()
        constraints = EffectiveConstraints(
            worker_categories=("LOCAL",),
            context_classes=(),
            tool_classes=(TM.WRITE,),
            approval_required=False,
            persistence="NONE",
            output_contract="TEXT",
        )
        result = run_tool_cycle(req, self.write_tool, constraints=constraints,
                                authorization=self.authorization(req),
                                replay_store=NoClaimApiStore())
        self.assertEqual(result.status, TM.DENIED)
        self.assertIn(TM.TOOL_GATE_FAILED,
                      [d.get("code") for d in result.diagnostics])


if __name__ == "__main__":
    unittest.main()

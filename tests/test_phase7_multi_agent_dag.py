"""Tests for Phase 7: Multi-Agent State Graph, Inter-Agent Bus, and Concurrency Locks (DEC-2026-0006)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tabularium.engine.runtime import (
    AWAITING_APPROVAL,
    COMPLETED,
    AgentRole,
    CheckpointLockTimeoutError,
    ExecutionCollisionError,
    ExecutionState,
    InterAgentBus,
    MessageType,
    MultiAgentWorkflow,
    checkpoint_lock,
    execution_collision_guard,
)


class MockDescriptor:
    category = "CLI"


class MockWorkerResult:
    def __init__(self, output: str, status: str = "COMPLETED") -> None:
        self.output = output
        self.status = status
        self.worker_id = "mock-worker-01"
        self.usage = {"tokens": 120}
        self.limitations = []
        self.diagnostics = []


class MockWorkerAdapter:
    def __init__(self, output: str = "Healthy normal task completion") -> None:
        self.output = output
        self.descriptor = MockDescriptor()

    def execute(self, request):
        return MockWorkerResult(output=self.output)


class TestInterAgentBus(unittest.TestCase):
    """Test Inter-Agent Context Bus protocol."""

    def test_send_and_filter_messages(self):
        bus = InterAgentBus()
        bus.send(AgentRole.PLANNER, AgentRole.WORKER, MessageType.PROPOSAL, "Decompose task")
        bus.send(AgentRole.WORKER, AgentRole.AUDITOR, MessageType.EXECUTION, "Code diff output")
        bus.send(AgentRole.AUDITOR, AgentRole.GATE, MessageType.ADVISORY, "All clear")

        self.assertEqual(len(bus.to_list()), 3)

        planner_msgs = bus.get_messages(sender_role=AgentRole.PLANNER)
        self.assertEqual(len(planner_msgs), 1)
        self.assertEqual(planner_msgs[0].content, "Decompose task")

        auditor_msgs = bus.get_messages(recipient_role=AgentRole.GATE)
        self.assertEqual(len(auditor_msgs), 1)
        self.assertEqual(auditor_msgs[0].message_type, MessageType.ADVISORY.value)
        self.assertFalse(bus.has_veto_recommendation())

    def test_serialization_round_trip(self):
        bus = InterAgentBus()
        bus.send(AgentRole.PLANNER, AgentRole.WORKER, MessageType.PROPOSAL, "Round-trip test")
        data = bus.to_list()

        restored = InterAgentBus.from_list(data)
        self.assertEqual(len(restored.to_list()), 1)
        self.assertEqual(restored.to_list()[0]["content"], "Round-trip test")


class TestMultiAgentDAGWorkflow(unittest.TestCase):
    """Test Multi-Agent DAG orchestration and Principle 7 compliance."""

    def setUp(self):
        self.state = ExecutionState(
            execution_id="exec-dag-001",
            request_id="req-001",
            task_id="TASK-2026-0007",
            intent="orchestrate multi-agent DAG",
            payload={"sections": []},
        )

    def test_clean_flow_completes(self):
        workflow = MultiAgentWorkflow()
        adapter = MockWorkerAdapter(output="Standard compliant implementation")
        result = workflow.run(self.state, adapter)

        self.assertEqual(result.runtime_status, COMPLETED)
        bus_data = result.governance.get("inter_agent_bus")
        self.assertIsInstance(bus_data, list)
        self.assertEqual(len(bus_data), 3)  # Planner, Worker, Auditor

    def test_auditor_veto_triggers_human_approval_boundary(self):
        """Principle 7: AI critique does not make decisions, but flags for human approval."""
        workflow = MultiAgentWorkflow()
        # Output contains secret marker which triggers auditor advisory veto
        adapter = MockWorkerAdapter(output="Output containing sk-live-secret-key-12345")
        result = workflow.run(self.state, adapter)

        self.assertEqual(result.runtime_status, AWAITING_APPROVAL)
        self.assertTrue(result.approval_required)
        self.assertIsNotNone(result.approval_request)
        self.assertIn("Auditor advisory veto", result.approval_request["reason"])

        bus = InterAgentBus.from_list(result.governance.get("inter_agent_bus"))
        self.assertTrue(bus.has_veto_recommendation())


class TestCheckpointConcurrencyLocks(unittest.TestCase):
    """Test DRL-003 SQLite checkpoint lock and execution collision guard."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.checkpoint = Path(self.tmp.name) / "checkpoints.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_checkpoint_lock_mutual_exclusion(self):
        with checkpoint_lock(self.checkpoint, timeout=1.0):
            # Attempting to acquire a second lock on the same path with short timeout must raise timeout
            with self.assertRaises(CheckpointLockTimeoutError):
                with checkpoint_lock(self.checkpoint, timeout=0.1, poll_interval=0.02):
                    pass

    def test_execution_collision_guard_prevents_simultaneous_id(self):
        with execution_collision_guard(self.checkpoint, "exec-dup-01"):
            # Same execution_id concurrently must fail closed
            with self.assertRaises(ExecutionCollisionError):
                with execution_collision_guard(self.checkpoint, "exec-dup-01"):
                    pass

        # Once released, the same execution_id can be acquired again
        with execution_collision_guard(self.checkpoint, "exec-dup-01"):
            pass

    def test_run_protected_executes_successfully(self):
        workflow = MultiAgentWorkflow(checkpoint_path=str(self.checkpoint))
        state = ExecutionState(
            execution_id="exec-prot-001",
            request_id="req-prot-001",
            task_id="TASK-2026-0007",
            intent="protected run",
            payload={"sections": []},
        )
        adapter = MockWorkerAdapter(output="Protected output")
        result = workflow.run_protected(state, adapter)
        self.assertEqual(result.runtime_status, COMPLETED)


if __name__ == "__main__":
    unittest.main()

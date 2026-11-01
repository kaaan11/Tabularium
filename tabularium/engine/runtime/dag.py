"""Multi-Agent State Graph and Governed DAG Workflow (Phase 7 / DEC-2026-0006).

Implements the governed multi-agent execution pipeline:
    START -> Planner -> Worker -> Auditor -> Gate -> (Human Approval if flagged) -> Complete -> END

Enforces:
- Principle 1: No unratified authority.
- Principle 4: Human override is absolute.
- Principle 7: AI opinions do not become decisions (Auditor output is strictly advisory).
- DRL-003: Checkpoint concurrency locking and execution collision guards.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from . import model as M
from . import steps as S
from .lock import checkpoint_lock, execution_collision_guard
from .multi_agent import AgentRole, InterAgentBus, MessageType


def default_planner(task_objective: str, intent: str, bus: InterAgentBus) -> str:
    """Deterministic default planner producing structured execution breakdown."""
    plan = f"Plan for '{intent}': decomposed execution targeting objective '{task_objective}'"
    bus.send(
        sender_role=AgentRole.PLANNER,
        recipient_role=AgentRole.WORKER,
        message_type=MessageType.PROPOSAL,
        content=plan,
    )
    return plan


def default_auditor(worker_output: str, bus: InterAgentBus) -> bool:
    """Auditor scanning worker output for obvious policy or secret red flags.

    Returns True if approved; False if an advisory veto recommendation is emitted.
    In accordance with Principle 7, a veto is advisory and surfaces to the human gate.
    """
    secret_markers = ("sk-", "api_key", "bearer ", "password=", "secret=")
    lower_out = worker_output.lower()
    if any(marker in lower_out for marker in secret_markers):
        bus.send(
            sender_role=AgentRole.AUDITOR,
            recipient_role=AgentRole.GATE,
            message_type=MessageType.VETO_RECOMMENDATION,
            content="Detected potential secret or token in candidate worker output",
            metadata={"veto_code": "SECRET_FLAG"},
        )
        return False

    bus.send(
        sender_role=AgentRole.AUDITOR,
        recipient_role=AgentRole.GATE,
        message_type=MessageType.ADVISORY,
        content="Auditor review complete: no constitutional violations detected.",
    )
    return True


class MultiAgentWorkflow:
    """Governed Multi-Agent DAG workflow coordinator."""

    def __init__(self,
                 checkpoint_path: str | None = None,
                 planner_fn: Callable[[str, str, InterAgentBus], str] | None = None,
                 auditor_fn: Callable[[str, InterAgentBus], bool] | None = None) -> None:
        self.checkpoint_path = checkpoint_path
        self.planner_fn = planner_fn or default_planner
        self.auditor_fn = auditor_fn or default_auditor

    def run(self,
            state: M.ExecutionState,
            worker_adapter: Any) -> M.ExecutionState:
        """Run the multi-agent DAG pipeline from an initial ExecutionState."""
        # 1. Structural input validation
        curr = S.validate_input(state)
        if curr.is_terminal:
            return curr

        # Initialize or restore Inter-Agent Bus from state metadata
        gov = dict(curr.governance)
        bus_data = gov.get("inter_agent_bus")
        bus = InterAgentBus.from_list(bus_data) if isinstance(bus_data, list) else InterAgentBus()

        # 2. Planner Node
        objective = curr.task_id or "Task"
        self.planner_fn(objective, curr.intent, bus)

        # 3. Worker Node
        curr = S.execute_worker(curr, worker_adapter)
        if curr.is_terminal:
            gov["inter_agent_bus"] = bus.to_list()
            return replace(curr, governance=gov)

        worker_out = (curr.worker_result or {}).get("output", "")
        bus.send(
            sender_role=AgentRole.WORKER,
            recipient_role=AgentRole.AUDITOR,
            message_type=MessageType.EXECUTION,
            content=worker_out,
        )

        # 4. Auditor Node (Principle 7: advisory critique)
        self.auditor_fn(worker_out, bus)

        # 5. Gate Node: Inspect bus and approval requirements
        has_veto = bus.has_veto_recommendation()
        requires_human = curr.approval_required or has_veto

        gov["inter_agent_bus"] = bus.to_list()
        curr = replace(curr, governance=gov)

        if requires_human:
            curr = replace(curr, approval_required=True)
            curr = S.approval_boundary(curr)
            if has_veto and curr.approval_request:
                req = dict(curr.approval_request)
                req["reason"] = "Human approval required (Auditor advisory veto flagged)"
                curr = replace(curr, approval_request=req)
            return curr

        # 6. Completion
        return S.complete(curr)

    def run_protected(self,
                      state: M.ExecutionState,
                      worker_adapter: Any) -> M.ExecutionState:
        """Run pipeline protected by DRL-003 checkpoint lock and collision guard."""
        if not self.checkpoint_path:
            return self.run(state, worker_adapter)

        with checkpoint_lock(self.checkpoint_path):
            with execution_collision_guard(self.checkpoint_path, state.execution_id):
                return self.run(state, worker_adapter)

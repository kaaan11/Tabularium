"""Governed execution runtime (M4.1).

The first production execution-runtime boundary. LangGraph provides
scheduling, suspend/resume, and durable checkpointing; Tabularium keeps authority,
approval semantics, and every governance decision.

Importing this package does NOT import LangGraph — the dependency is loaded
lazily on first use, so the M2 loader, projection, skills, and workers all
work with the runtime dependency absent.

What M4.1 provides: start / resume / inspect over a durable local checkpoint,
a first-class AWAITING_APPROVAL runtime status, and approval resolution that
fails closed.

What it does NOT provide: canonical approval recording in project truth, the
persistence gate, Task/Handoff/Report persistence, tool execution, worker
routing, or model selection. Those are later M4 units.

Dependency: tabularium/engine/requirements-runtime.txt (never tabularium/engine/requirements.txt).
"""

from .dag import MultiAgentWorkflow, default_auditor, default_planner
from .langgraph_runtime import LangGraphRuntime
from .lock import (CheckpointLockTimeoutError, ExecutionCollisionError,
                   checkpoint_lock, execution_collision_guard)
from .multi_agent import (AgentRole, InterAgentBus, InterAgentMessage,
                          MessageType)
from .approval import (APPROVAL_REASON_CODES, DECISIONS, ApprovalScope,
                       ApprovalVerdict, HumanApprovalResolution,
                       valid_timestamp)
from .approval import APPROVE, REJECT
from .model import (AWAITING_APPROVAL, CANCEL, CANCELLED, COMPLETED, FAILED,
                    REASON_CODES, REJECTED, RESOLUTIONS, RUNNING,
                    RUNTIME_STATUSES, TERMINAL_STATUSES, ApprovalRequest,
                    ExecutionState, RuntimeResult)

__all__ = ["LangGraphRuntime", "ExecutionState", "RuntimeResult",
           "ApprovalRequest", "HumanApprovalResolution", "ApprovalScope",
           "ApprovalVerdict", "valid_timestamp", "DECISIONS",
           "APPROVAL_REASON_CODES",
           "RUNNING", "AWAITING_APPROVAL", "COMPLETED", "REJECTED", "FAILED",
           "CANCELLED", "RUNTIME_STATUSES", "TERMINAL_STATUSES",
           "APPROVE", "REJECT", "CANCEL", "RESOLUTIONS", "REASON_CODES",
           "MultiAgentWorkflow", "default_planner", "default_auditor",
           "InterAgentBus", "InterAgentMessage", "AgentRole", "MessageType",
           "checkpoint_lock", "execution_collision_guard",
           "CheckpointLockTimeoutError", "ExecutionCollisionError"]

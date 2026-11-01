"""Governed execution pipeline (M4.2).

Orchestration glue that wires the approved layers into one path:

    project → M2 loader → Context Package → skill governance
            → M3 projection → WorkerRequest → M4.1 runtime

The pipeline owns no governance. It sequences components that already own
theirs, and it refuses explicitly rather than falling back: it never picks
another task, worker, model, or profile, never drops a requested skill, and
never lowers an approval requirement.

What M4.2 does NOT do: automatic task discovery, worker routing, model
routing, canonical approval recording, the persistence gate, tool execution,
observability, or memory.

Read-only relative to project truth. Nothing here writes to `.ai/`.
"""

from .model import (AWAITING_APPROVAL, CANCELLED, COMPLETED, FAILED,
                    PIPELINE_REASON_CODES, PIPELINE_STATUSES,
                    PREFLIGHT_READY, PREFLIGHT_REFUSED,
                    PREPARATION_REJECTED, REJECTED, ContextMetrics,
                    ExecutionPlan, PipelineResult, PreflightResult)
from .pipeline import (explain_execution, inspect_execution,
                       resume_execution, start_execution)

__all__ = ["start_execution", "resume_execution", "inspect_execution",
           "explain_execution",
           "ExecutionPlan", "PipelineResult", "PreflightResult",
           "ContextMetrics",
           "PREPARATION_REJECTED", "AWAITING_APPROVAL", "COMPLETED",
           "REJECTED", "CANCELLED", "FAILED", "PIPELINE_STATUSES",
           "PIPELINE_REASON_CODES", "PREFLIGHT_READY", "PREFLIGHT_REFUSED"]

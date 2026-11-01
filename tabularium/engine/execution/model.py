"""Pipeline input, result, and reason codes (M4.2).

The execution layer is ORCHESTRATION GLUE between already-approved components.
It owns no governance of its own: M2 owns context, M3 projection owns
reduction, M3.4 skill governance owns narrowing, M3.3 owns the worker
boundary, and M4.1 owns the execution lifecycle. This module only names the
things that cross between them.

A PipelineResult is not a WorkerResult and not a RuntimeResult. It says what
happened to one PREPARATION-AND-EXECUTION attempt, and it carries no framework
object.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- pipeline status --------------------------------------------------------
#
# Deliberately minimal, and deliberately distinct from both worker status and
# runtime status.

PREPARATION_REJECTED = "PREPARATION_REJECTED"   # never reached a worker
AWAITING_APPROVAL = "AWAITING_APPROVAL"
COMPLETED = "COMPLETED"
REJECTED = "REJECTED"
CANCELLED = "CANCELLED"
FAILED = "FAILED"

PIPELINE_STATUSES = (PREPARATION_REJECTED, AWAITING_APPROVAL, COMPLETED,
                     REJECTED, CANCELLED, FAILED)

# --- preflight (explain) statuses -------------------------------------------
#
# A preflight result describes what a preparation WOULD decide. It never
# claims an execution happened.

PREFLIGHT_READY = "READY"
PREFLIGHT_REFUSED = "REFUSED"

# --- reason codes -----------------------------------------------------------
#
# Codes owned by an approved component are REUSED, not duplicated: projection
# refusals, skill governance denials, worker refusal codes, and runtime codes
# all surface with their original names. The codes below name only what the
# pipeline itself decides.

CONTEXT_PACKAGE_NOT_READY = "CONTEXT_PACKAGE_NOT_READY"
TASK_NOT_ACTIONABLE = "TASK_NOT_ACTIONABLE"
TASK_TARGET_REQUIRED = "TASK_TARGET_REQUIRED"
PROJECT_NOT_FOUND = "PROJECT_NOT_FOUND"
PROJECTION_FAILED = "PROJECTION_FAILED"
SKILL_NOT_FOUND = "SKILL_NOT_FOUND"
SKILL_MALFORMED = "SKILL_MALFORMED"
SKILL_CONSTRAINT_VIOLATION = "SKILL_CONSTRAINT_VIOLATION"
WORKER_INCOMPATIBLE = "WORKER_INCOMPATIBLE"
WORKER_REQUIRED = "WORKER_REQUIRED"
MODEL_TARGET_REQUIRED = "MODEL_TARGET_REQUIRED"
OUTPUT_CONTRACT_MISMATCH = "OUTPUT_CONTRACT_MISMATCH"
CHECKPOINT_PATH_REQUIRED = "CHECKPOINT_PATH_REQUIRED"
CHECKPOINT_INSIDE_PROJECT = "CHECKPOINT_INSIDE_PROJECT"
RUNTIME_START_FAILED = "RUNTIME_START_FAILED"
POLICY_VIOLATION = "POLICY_VIOLATION"
MODEL_ASSIGNMENT_MISMATCH = "MODEL_ASSIGNMENT_MISMATCH"
SCOPE_RISK_EXCEEDED = "SCOPE_RISK_EXCEEDED"
SECRET_DETECTED_IN_CONTEXT = "SECRET_DETECTED_IN_CONTEXT"
POLICY_INPUT_INVALID = "POLICY_INPUT_INVALID"
WORKFLOW_SELECTION_INVALID = "WORKFLOW_SELECTION_INVALID"

PIPELINE_REASON_CODES = (
    CONTEXT_PACKAGE_NOT_READY, TASK_NOT_ACTIONABLE, TASK_TARGET_REQUIRED,
    PROJECT_NOT_FOUND, PROJECTION_FAILED, SKILL_NOT_FOUND, SKILL_MALFORMED,
    SKILL_CONSTRAINT_VIOLATION, WORKER_INCOMPATIBLE, WORKER_REQUIRED,
    MODEL_TARGET_REQUIRED, OUTPUT_CONTRACT_MISMATCH,
    CHECKPOINT_PATH_REQUIRED, CHECKPOINT_INSIDE_PROJECT, RUNTIME_START_FAILED,
    POLICY_VIOLATION, MODEL_ASSIGNMENT_MISMATCH, SCOPE_RISK_EXCEEDED,
    SECRET_DETECTED_IN_CONTEXT, POLICY_INPUT_INVALID,
    WORKFLOW_SELECTION_INVALID)


@dataclass(frozen=True)
class ExecutionPlan:
    """Everything the caller must decide before an execution can be prepared.

    Every choice is EXPLICIT. The pipeline performs no discovery, no routing,
    and no defaulting that could change cost, capability, or authority: no task
    is guessed, no worker is chosen, no model is resolved, and no profile is
    switched to make an execution succeed.

    Credentials never appear here. Provider authentication is the operator
    environment's business (M3.6), and a secret in a pipeline input would end
    up in runtime state.
    """

    project_root: str
    task_id: str
    intent: str
    execution_id: str
    checkpoint_path: str

    # Worker choice is the caller's; there is no routing in M4.2.
    adapter: Any = None
    required_category: str | None = None
    required_capabilities: tuple[str, ...] = ()

    # API workers need a resolved model. It is never guessed (M3.6).
    model_target: Any = None

    # Explicit skill selection only — no catalog, no automatic selection.
    skill_path: str | None = None

    profile: str = "HOT"
    ai_os_root: str | None = None

    # Execution orchestration: "single" (default LangGraph path) or "dag"
    # (the governed MultiAgentWorkflow, DEC-2026-0010). Explicit opt-in only;
    # the pipeline never upgrades a run on its own.
    workflow: str = "single"

    # Control-plane grant. A skill may only narrow this (M3.4).
    grant: Any = None
    approval_required: bool = False

    def to_metadata(self) -> dict[str, Any]:
        """Non-secret description of the plan, safe to carry in state."""
        return {"task_id": self.task_id, "profile": self.profile,
                "skill": self.skill_path or "", "intent": self.intent}


@dataclass(frozen=True)
class ContextMetrics:
    """Product-level observability for one preparation.

    `effective_allowed` is what skill governance permitted; `projected` is what
    survived the budget. Both are reported so a caller can tell narrowing from
    truncation.
    """

    available_context: dict[str, int] = field(default_factory=dict)
    governed_context: dict[str, int] = field(default_factory=dict)
    payload_size: dict[str, int] = field(default_factory=dict)
    context_avoided: dict[str, int] = field(default_factory=dict)
    reduction_pct: float | None = None
    effective_allowed_classes: tuple[str, ...] = ()
    projected_sections: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"available_context": dict(self.available_context),
                "governed_context": dict(self.governed_context),
                "payload_size": dict(self.payload_size),
                "context_avoided": dict(self.context_avoided),
                "reduction_pct": self.reduction_pct,
                "effective_allowed_classes": list(self.effective_allowed_classes),
                "projected_sections": list(self.projected_sections)}


@dataclass(frozen=True)
class PreflightResult:
    """Stable product facts from the preparation stages, without executing.

    Produced by `explain_execution`: stages 1-5 of the pipeline run exactly
    as they would for a real execution (same loader, same governance checks,
    same projection), and the result stops there — no worker process, no
    provider call, no checkpoint, no runtime state. `reason_codes` reuse the
    pipeline vocabulary verbatim so a refusal reads identically before and
    after the runtime boundary.
    """

    status: str
    task_id: str = ""
    reason_codes: tuple[str, ...] = ()
    worker_id: str = ""
    worker_category: str = ""
    # The resolved model that WOULD be used, when an API worker is selected
    # with one; always empty for native workers.
    resolved_model: str = ""
    approval_required: bool | None = None
    metrics: ContextMetrics | None = None
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def would_run(self) -> bool:
        return self.status == PREFLIGHT_READY

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "would_run": self.would_run,
            "task_id": self.task_id,
            "reason_codes": list(self.reason_codes),
            "worker_id": self.worker_id,
            "worker_category": self.worker_category,
            "resolved_model": self.resolved_model,
            "approval_required": self.approval_required,
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "diagnostics": [dict(d) for d in self.diagnostics],
        }


@dataclass(frozen=True)
class PipelineResult:
    """What happened to one execution attempt. No framework object inside.

    `output` is populated only on COMPLETED, matching the M4.1 rule: a
    rejected, cancelled, or suspended execution must never present unapproved
    work as its answer.
    """

    status: str
    execution_id: str = ""
    task_id: str = ""
    reason_codes: tuple[str, ...] = ()
    metrics: ContextMetrics | None = None
    approval_request: Any = None
    worker_result: dict[str, Any] | None = None
    output: str = ""
    governance: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[dict[str, Any], ...] = ()

    @property
    def executed(self) -> bool:
        """Whether a worker ran at all.

        A PREPARATION_REJECTED result never reached one, which is the
        distinction that matters when reading a failure.
        """
        return self.status != PREPARATION_REJECTED

    @property
    def awaiting_approval(self) -> bool:
        return self.status == AWAITING_APPROVAL

    def to_dict(self) -> dict[str, Any]:
        request = self.approval_request
        return {
            "status": self.status,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "reason_codes": list(self.reason_codes),
            "metrics": self.metrics.to_dict() if self.metrics else None,
            "approval_request": request.to_dict()
            if hasattr(request, "to_dict") else request,
            "worker_result": self.worker_result,
            "output": self.output,
            "governance": dict(self.governance),
            "diagnostics": [dict(d) for d in self.diagnostics],
        }

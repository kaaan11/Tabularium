"""Tabularium-owned runtime execution state and reason codes (M4.1).

RUNTIME STATE IS NOT PROJECT TRUTH.

This state records where one execution stands. It is operational continuity,
not a record: it is not a Task, not a Context Package, not a WorkerRequest, not
a WorkerResult, and never an M1 record. A checkpoint containing it creates no
Evidence, no Decision, and no approval.

It is also not LangGraph state. No framework object appears in any field, so
the state serializes as plain JSON and survives a runtime swap unchanged.

RUNTIME STATUS IS NOT WORKER STATUS. `WorkerResult.status` says what the
worker did; `runtime_status` says where the execution stands. An execution can
hold a COMPLETED worker result and still be AWAITING_APPROVAL, because the
worker finishing is not permission to proceed.

"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any

# --- runtime status ---------------------------------------------------------

RUNNING = "RUNNING"
AWAITING_APPROVAL = "AWAITING_APPROVAL"
COMPLETED = "COMPLETED"
REJECTED = "REJECTED"
FAILED = "FAILED"
CANCELLED = "CANCELLED"

RUNTIME_STATUSES = (RUNNING, AWAITING_APPROVAL, COMPLETED, REJECTED, FAILED,
                    CANCELLED)

TERMINAL_STATUSES = (COMPLETED, REJECTED, FAILED, CANCELLED)

# --- approval resolution vocabulary -----------------------------------------
#
# A runtime resume is NOT an approval. These are the only values that carry
# approval meaning, and only `resolve_approval` reads them.

# Runtime-level outcomes of an approval boundary. The DECISION vocabulary a
# human may express lives in `approval.py` and is APPROVE/REJECT only; CANCEL
# here is a runtime action (`runtime.cancel`), never a human verdict.
APPROVE = "APPROVE"
REJECT = "REJECT"
CANCEL = "CANCEL"

RESOLUTIONS = (APPROVE, REJECT, CANCEL)

# --- reason codes -----------------------------------------------------------
#
# Tabularium-owned and stable. LangGraph exception prose is never surfaced as a
# reason code; sanitized detail travels in diagnostics.

INVALID_RUNTIME_INPUT = "INVALID_RUNTIME_INPUT"
WORKER_REJECTED = "WORKER_REJECTED"
WORKER_FAILED = "WORKER_FAILED"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
APPROVAL_GRANTED = "APPROVAL_GRANTED"
APPROVAL_DENIED = "APPROVAL_DENIED"
EXECUTION_CANCELLED = "EXECUTION_CANCELLED"
INVALID_APPROVAL_RESOLUTION = "INVALID_APPROVAL_RESOLUTION"
RESUME_WITHOUT_RESOLUTION = "RESUME_WITHOUT_RESOLUTION"
RESOLUTION_REQUEST_MISMATCH = "RESOLUTION_REQUEST_MISMATCH"
NOTHING_TO_RESUME = "NOTHING_TO_RESUME"
EXECUTION_NOT_FOUND = "EXECUTION_NOT_FOUND"
CHECKPOINT_ERROR = "CHECKPOINT_ERROR"
RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
RUNTIME_ERROR = "RUNTIME_ERROR"
WORKER_UNAVAILABLE = "WORKER_UNAVAILABLE"

REASON_CODES = (
    INVALID_RUNTIME_INPUT, WORKER_REJECTED, WORKER_FAILED, APPROVAL_REQUIRED,
    APPROVAL_GRANTED, APPROVAL_DENIED, EXECUTION_CANCELLED,
    INVALID_APPROVAL_RESOLUTION, RESUME_WITHOUT_RESOLUTION,
    RESOLUTION_REQUEST_MISMATCH, NOTHING_TO_RESUME, EXECUTION_NOT_FOUND,
    CHECKPOINT_ERROR, RUNTIME_UNAVAILABLE, RUNTIME_ERROR, WORKER_UNAVAILABLE)

# Schema version for the checkpointed state shape. Explicit so a later change
# is a decision rather than a silent incompatibility with checkpoints on disk.
STATE_VERSION = "1.0"


@dataclass(frozen=True)
class ApprovalRequest:
    """What the runtime hands back when it suspends.

    Data only. It confers nothing, decides nothing, and is not an approval
    record. `request_id` exists so a resolution can be checked against the
    request actually pending, rather than approving whatever is open.
    """

    request_id: str
    execution_id: str
    action: str
    task_id: str = ""
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ApprovalRequest | None":
        if not isinstance(data, dict) or not data.get("request_id"):
            return None
        return cls(request_id=data["request_id"],
                   execution_id=data.get("execution_id", ""),
                   action=data.get("action", ""),
                   task_id=data.get("task_id", ""),
                   reason=data.get("reason", ""),
                   detail=dict(data.get("detail") or {}))


@dataclass
class ExecutionState:
    """The whole runtime state for one execution. Plain, JSON-serializable.

    Deliberately absent: chat history, framework memory, planner state, agent
    identity, project root, `.ai/` content, credentials, and any Context
    Package. What the worker may see already lives inside `payload`, which the
    projector produced upstream.
    """

    execution_id: str = ""
    state_version: str = STATE_VERSION

    # --- prepared input (constructed upstream, never here) ---
    request_id: str = ""
    task_id: str = ""
    intent: str = ""
    payload: Any = None
    required_category: str | None = None
    required_capabilities: tuple[str, ...] = ()
    governance: dict[str, Any] = field(default_factory=dict)

    # --- execution outcome ---
    runtime_status: str = RUNNING
    worker_id: str = ""
    worker_category: str = ""
    worker_result: dict[str, Any] | None = None

    # --- approval boundary ---
    approval_required: bool = False
    approval_request: dict[str, Any] | None = None
    approval_resolution: dict[str, Any] | None = None
    # Every request id that has already been decided. Replay protection lives
    # in durable state rather than in memory, so a second resolution fails
    # closed even in a fresh process.
    resolved_request_ids: list[str] = field(default_factory=list)

    diagnostics: list[dict[str, str]] = field(default_factory=list)

    # -- helpers ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["required_capabilities"] = list(self.required_capabilities)
        # `asdict` recurses into nested dataclasses, which would turn a
        # ProjectedPayload into its internal `{"sections": [[name, value]…]}`
        # dump. The payload must cross the checkpoint in its CANONICAL shape,
        # because that is what a worker is handed on both the first attempt
        # and after a resume.
        payload = self.payload
        if hasattr(payload, "to_dict"):
            data["payload"] = payload.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionState":
        """Load, dropping unknown keys.

        LangGraph injects `__interrupt__` into the mapping it returns. Dropping
        unknown keys keeps framework bookkeeping out of Tabularium state without any
        framework-specific handling.
        """
        known = set(cls.__dataclass_fields__)
        clean = {k: v for k, v in (data or {}).items() if k in known}
        caps = clean.get("required_capabilities")
        if caps is not None:
            clean["required_capabilities"] = tuple(caps)
        return cls(**clean)

    def with_diagnostic(self, code: str, detail: str = "") -> "ExecutionState":
        return replace(self, diagnostics=self.diagnostics
                       + [{"code": code, "detail": detail}])

    @property
    def is_terminal(self) -> bool:
        return self.runtime_status in TERMINAL_STATUSES


@dataclass(frozen=True)
class RuntimeResult:
    """What a runtime call returns. Contains no framework object.

    A caller sees Tabularium types only: it never constructs a StateGraph, a
    Command, an interrupt, or a checkpointer to use this runtime.
    """

    execution_id: str
    runtime_status: str
    reason_codes: tuple[str, ...] = ()
    approval_request: ApprovalRequest | None = None
    worker_result: dict[str, Any] | None = None
    output: str = ""
    diagnostics: tuple[dict[str, str], ...] = ()

    @property
    def awaiting_approval(self) -> bool:
        return self.runtime_status == AWAITING_APPROVAL

    @property
    def completed(self) -> bool:
        return self.runtime_status == COMPLETED

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "runtime_status": self.runtime_status,
            "reason_codes": list(self.reason_codes),
            "approval_request": self.approval_request.to_dict()
            if self.approval_request else None,
            "worker_result": self.worker_result,
            "output": self.output,
            "diagnostics": [dict(d) for d in self.diagnostics],
        }


def result_from_state(state: ExecutionState,
                      extra_codes: tuple[str, ...] = ()) -> RuntimeResult:
    """Project runtime state into the caller-facing result."""
    return RuntimeResult(
        execution_id=state.execution_id,
        runtime_status=state.runtime_status,
        reason_codes=tuple(dict.fromkeys(
            tuple(d["code"] for d in state.diagnostics) + tuple(extra_codes))),
        approval_request=ApprovalRequest.from_dict(state.approval_request),
        worker_result=state.worker_result,
        # OUTPUT IS ONLY SURFACED ON COMPLETION. A rejected, cancelled, failed,
        # or still-suspended execution keeps its worker_result for inspection
        # and provenance, but must not present unapproved work as the
        # execution's output — that is how an unapproved result quietly
        # becomes an answer.
        output=((state.worker_result or {}).get("output", "")
                if state.runtime_status == COMPLETED else ""),
        diagnostics=tuple(dict(d) for d in state.diagnostics))

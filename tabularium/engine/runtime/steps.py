"""Tabularium-owned execution steps. FRAMEWORK-NEUTRAL (M4.1).

Every governance decision the runtime makes lives here, and nothing in this
module imports LangGraph. The framework schedules these functions; it never
replaces their judgement.

The invariant this module exists to enforce:

    A RUNTIME RESUMING IS NOT AN APPROVAL.

`resolve_approval` is the only thing that turns a resolution into meaning, and
it demands an explicit, valid, matching decision. A bare resume, a missing
resolution, `True`, an unknown string, or a decision aimed at a different
request all fail closed.

This module also does not construct governed context: it receives an already
prepared payload and never reads `.ai/`, runs the M2 loader, projects, or
selects a skill, worker, or model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from . import approval as A
from . import model as M


def validate_input(state: M.ExecutionState) -> M.ExecutionState:
    """VALIDATE_RUNTIME_INPUT. Structural only — this is not governance."""
    missing = [name for name in ("execution_id", "request_id", "intent")
               if not getattr(state, name)]
    if state.payload is None:
        missing.append("payload")
    if missing:
        return replace(
            state, runtime_status=M.FAILED,
            diagnostics=state.diagnostics + [
                {"code": M.INVALID_RUNTIME_INPUT,
                 "detail": f"missing or empty: {', '.join(sorted(missing))}"}])
    return state


def _as_payload(payload):
    """Restore the canonical ProjectedPayload the M3.3 contract promises.

    A payload crosses the checkpoint as its canonical `{section: content}`
    mapping (M4.2), which is what makes the state plain JSON. Handing that raw
    mapping to a worker would quietly break the contract in
    `WorkerRequest.payload`: a worker calling `section_names()` on a dict sees
    NOTHING rather than failing loudly, so a governance-relevant emptiness
    would look like an ordinary empty payload.

    Rebuilding here keeps the worker-facing type identical whether an
    execution ran straight through or resumed from a checkpoint. Section ORDER
    is preserved: the canonical mapping keeps insertion order, and the
    projector's ordering is what produced it.
    """
    if payload is None or not isinstance(payload, dict):
        return payload
    from tabularium.engine.projection.model import ProjectedPayload

    return ProjectedPayload(sections=tuple(payload.items()))


def execute_worker(state: M.ExecutionState, adapter) -> M.ExecutionState:
    """EXECUTE_WORKER through the frozen M3.3 WorkerAdapter boundary.

    The payload is passed through EXACTLY as received. The runtime adds no
    context, injects no constitution or protocol text, and rehydrates nothing
    from project files.
    """
    if adapter is None:
        return replace(state, runtime_status=M.FAILED,
                       diagnostics=state.diagnostics + [
                           {"code": M.WORKER_UNAVAILABLE,
                            "detail": "no worker adapter was supplied"}])

    from tabularium.engine.workers import WorkerRequest

    request = WorkerRequest(
        request_id=state.request_id, intent=state.intent,
        payload=_as_payload(state.payload), task_id=state.task_id or None,
        required_category=state.required_category,
        required_capabilities=tuple(state.required_capabilities),
        governance=dict(state.governance))
    result = adapter.execute(request)

    outcome = {"status": result.status, "output": result.output,
               "worker_id": result.worker_id,
               "usage": dict(result.usage),
               "limitations": list(result.limitations)}
    worker_diagnostics = [
        {"code": str(d.get("code", "WORKER_DIAGNOSTIC")),
         "detail": str(d.get("detail", ""))}
        for d in result.diagnostics
        if isinstance(d, dict)
    ]
    updated = replace(state, worker_id=result.worker_id,
                      worker_category=adapter.descriptor.category,
                      worker_result=outcome,
                      diagnostics=state.diagnostics + worker_diagnostics)

    if result.status == "REJECTED":
        return replace(updated, runtime_status=M.FAILED,
                       diagnostics=updated.diagnostics + [
                           {"code": M.WORKER_REJECTED,
                            "detail": result.worker_id}])
    if result.status != "COMPLETED":
        return replace(updated, runtime_status=M.FAILED,
                       diagnostics=updated.diagnostics + [
                           {"code": M.WORKER_FAILED, "detail": result.status}])
    return updated


def approval_boundary(state: M.ExecutionState) -> M.ExecutionState:
    """CHECK_APPROVAL_BOUNDARY.

    When approval is required the execution becomes AWAITING_APPROVAL — a
    first-class RUNTIME status, so the worker's own result keeps meaning
    "what the worker did" and never doubles as human authority state.
    """
    if not state.approval_required:
        return state
    # Request identity is stable and derived from the execution plus the
    # number of boundaries already decided, so a SECOND boundary in the same
    # execution gets a DIFFERENT id and an old resolution cannot satisfy it.
    sequence = len(state.resolved_request_ids) + 1
    request = M.ApprovalRequest(
        request_id=f"{state.execution_id}:approval-{sequence}",
        execution_id=state.execution_id,
        task_id=state.task_id,
        action="continue past the approval boundary",
        reason="the control plane or the selected skill requires human "
               "approval before this execution may complete",
        detail={"worker_id": state.worker_id,
                "proposed_output": (state.worker_result or {}).get("output", ""),
                "scope": dict(state.governance.get("approval_scope") or {})})
    return replace(state, runtime_status=M.AWAITING_APPROVAL,
                   approval_request=request.to_dict(),
                   diagnostics=state.diagnostics + [
                       {"code": M.APPROVAL_REQUIRED,
                        "detail": request.request_id}])


def resolve_approval(state: M.ExecutionState,
                     resolution: Any) -> M.ExecutionState:
    """THE GOVERNANCE GATE. Only a VALIDATED HUMAN resolution passes.

    Approval is a human act with provenance. Nothing else satisfies this gate:
    not a bare `"APPROVE"`, not `True`, not a dict without an actor and a
    timestamp, not worker output, not skill text, and not the fact that a
    runtime was resumed. Every ambiguous input fails closed and leaves the
    request pending so a correct decision can still be supplied.

    SCOPE NOTE: this authorizes the RUNTIME to continue past THIS boundary.
    It is not a canonical approval record in project truth — nothing here
    writes `approved_by`/`approved_at` to a Task or creates any M1 record.
    M4.4 owns persistence.
    """
    pending = M.ApprovalRequest.from_dict(state.approval_request)

    verdict = A.validate(resolution, pending, state.execution_id,
                         task_id=state.task_id,
                         resolved_request_ids=tuple(state.resolved_request_ids))
    if not verdict.valid:
        return _halt(state, verdict.reason_codes[0],
                     "; ".join(verdict.reason_codes))

    human = verdict.resolution
    recorded = human.to_dict()
    resolved = list(state.resolved_request_ids) + [human.approval_request_id]

    if human.decision == A.APPROVE:
        return replace(state, approval_resolution=recorded,
                       resolved_request_ids=resolved,
                       runtime_status=M.RUNNING,
                       diagnostics=state.diagnostics + [
                           {"code": M.APPROVAL_GRANTED,
                            "detail": f"{human.approval_request_id} by "
                                      f"{human.decided_by}"}])

    return replace(state, approval_resolution=recorded,
                   resolved_request_ids=resolved,
                   runtime_status=M.REJECTED,
                   diagnostics=state.diagnostics + [
                       {"code": M.APPROVAL_DENIED,
                        "detail": f"{human.approval_request_id} by "
                                  f"{human.decided_by}"}])


def complete(state: M.ExecutionState) -> M.ExecutionState:
    """COMPLETE. Reachable only with approval in force when one was required."""
    if state.approval_required and (
            (state.approval_resolution or {}).get("decision") != A.APPROVE):
        return _halt(state, M.APPROVAL_REQUIRED,
                     "completion blocked: no approval in force")
    return replace(state, runtime_status=M.COMPLETED)


def cancel(state: M.ExecutionState) -> M.ExecutionState:
    """Mark a non-terminal execution cancelled.

    A cancelled execution is never mistaken for approved or completed: it
    carries its own terminal status and its own reason code.
    """
    if state.is_terminal:
        return state
    return replace(state, runtime_status=M.CANCELLED,
                   diagnostics=state.diagnostics + [
                       {"code": M.EXECUTION_CANCELLED,
                        "detail": "cancelled by caller"}])


def _halt(state: M.ExecutionState, code: str, detail: str) -> M.ExecutionState:
    """Fail closed, preserving the pending request so it can be retried."""
    return replace(state, runtime_status=M.AWAITING_APPROVAL
                   if state.approval_request else M.FAILED,
                   diagnostics=state.diagnostics + [
                       {"code": code, "detail": detail}])

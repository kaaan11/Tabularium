"""Governed execution pipeline (M4.2).

Wires the approved layers into one preparation-and-execution path:

    project → M2 loader → Context Package
            → skill governance (effective constraints)
            → M3 projection (narrowed BEFORE projecting)
            → WorkerRequest
            → M4.1 runtime → RuntimeResult

WHAT THIS LAYER OWNS: nothing but sequencing and refusal. Every judgement
belongs to the component that already owns it, and this module calls that
component rather than re-implementing it.

NO SILENT FALLBACK. The pipeline never picks another task, worker, model, or
profile, never drops a requested skill, and never lowers an approval
requirement to make an execution succeed. Failure stays explicit.

STILL READ-ONLY relative to project truth. Nothing here writes to `.ai/`,
records an approval, or creates an M1 record.
"""

from __future__ import annotations

import os
import time
from typing import Any

from tabularium.engine.context_loader import load_context
from tabularium.engine.policies import evaluate_policies
from tabularium.engine.projection import project
from tabularium.engine.projection.budget import DEFAULT_LIMITS
from tabularium.engine.skills import (ControlPlaneGrant, check_worker, evaluate,
                            load_skill)
from tabularium.engine.workers import WorkerRequest

from . import model as P

READY = "READY"
ACTIONABLE = "ACTIONABLE"
REQUIRED_SECTION = "required"
API_MODEL = "API_MODEL"


def start_execution(plan: P.ExecutionPlan,
                    collector: Any = None) -> P.PipelineResult:
    """Prepare and execute one Task through the governed pipeline.

    Returns a structured result at every stop. A preparation refusal means no
    worker ran and no runtime execution began.

    `collector` is OPTIONAL observability (M5.1) and defaults to a no-op.
    Tracing is descriptive: it observes this pipeline and can never change what
    it decides, so behavior with a collector is identical to behavior without
    one.
    """
    from tabularium.engine.observability import Tracer
    from tabularium.engine.observability import events as EV

    tracer = Tracer(collector, plan.execution_id, plan.task_id)
    tracer.mark(EV.EXECUTION_STARTED, component=EV.PIPELINE,
                attributes={"profile": plan.profile,
                            "skill": bool(plan.skill_path)})
    result = _start_execution(plan, tracer, EV)
    tracer.terminal(result.status, component=EV.PIPELINE,
                    status=(EV.ERROR if result.status in
                            (P.FAILED, P.PREPARATION_REJECTED) else EV.OK),
                    reason_codes=tuple(result.reason_codes))
    return result


def _start_execution(plan: P.ExecutionPlan, tracer, EV) -> P.PipelineResult:
    prepared = _prepare(plan, tracer, EV)
    if isinstance(prepared, P.PipelineResult):
        return prepared

    request, metrics, governance, approval_required = prepared

    # --- 6. hand over to the runtime ---------------------------------------
    if getattr(plan, "workflow", "single") == "dag":
        return _start_dag_execution(plan, request, metrics, governance,
                                    approval_required, tracer, EV)

    # The pipeline does NOT call the adapter itself; the runtime owns the
    # execution lifecycle, the checkpoint, and the approval interrupt.
    from tabularium.engine.runtime import LangGraphRuntime

    # An ATTEMPT event: the pipeline is about to hand this to the runtime.
    # Per the taxonomy rule, `*_STARTED` describes an attempt and may precede
    # the outcome; only `*_COMPLETED` requires a confirmed result.
    tracer.mark(EV.WORKER_STARTED, component=EV.WORKER,
                attributes={"worker_id": plan.adapter.descriptor.worker_id,
                            "category": plan.adapter.descriptor.category})
    started_worker = time.monotonic()
    try:
        runtime = LangGraphRuntime(checkpoint_path=plan.checkpoint_path)
        outcome = runtime.start(plan.execution_id, request,
                                approval_required=approval_required,
                                adapter=plan.adapter)
    except Exception as exc:  # noqa: BLE001
        return _reject(plan, P.RUNTIME_START_FAILED, _detail(exc),
                       metrics=metrics)

    result = _from_runtime(plan, outcome, metrics, governance)
    _trace_runtime_outcome(tracer, EV, result, _ms(started_worker))
    return result


def _start_dag_execution(plan: P.ExecutionPlan, request, metrics,
                         governance, approval_required, tracer, EV):
    """Run one execution through the governed multi-agent DAG (DEC-2026-0010).

    The already-ratified `MultiAgentWorkflow.run_protected` executes with the
    SAME caller-selected adapter: deterministic planner/auditor, advisory bus,
    vetoes to the existing human boundary, checkpoint locks held. The final
    state is persisted into the checkpoint so `inspect` and `continue` work —
    but resume NEVER re-enters the LangGraph graph for a dag execution (that
    would re-run the worker); see `resume_execution`.
    """
    from tabularium.engine.runtime import LangGraphRuntime, MultiAgentWorkflow
    from tabularium.engine.runtime.langgraph_runtime import _state_from_request

    tracer.mark(EV.WORKER_STARTED, component=EV.WORKER,
                attributes={"worker_id": plan.adapter.descriptor.worker_id,
                            "category": plan.adapter.descriptor.category})
    started_worker = time.monotonic()
    try:
        state = _state_from_request(plan.execution_id, request,
                                    approval_required)
        if state is None:
            return _reject(plan, P.RUNTIME_START_FAILED,
                           "a prepared WorkerRequest is required")
        final = MultiAgentWorkflow(
            checkpoint_path=plan.checkpoint_path).run_protected(
                state, plan.adapter)
        # Persist the whole state (bus included) so inspection and the
        # dag-aware resume path below observe exactly what ran.
        runtime = LangGraphRuntime(checkpoint_path=plan.checkpoint_path)
        outcome = runtime._write_terminal(plan.execution_id, final)
    except ImportError as exc:
        return _reject(plan, P.RUNTIME_START_FAILED,
                       "the governed runtime requires LangGraph; install the "
                       "Tabularium 'runtime' extra",
                       detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        return _reject(plan, P.RUNTIME_START_FAILED, _detail(exc),
                       metrics=metrics)

    gov = dict(governance)
    bus = (final.governance or {}).get("inter_agent_bus")
    if isinstance(bus, list):
        gov["inter_agent_bus"] = bus
    result = _from_runtime(plan, outcome, metrics, gov)
    _trace_runtime_outcome(tracer, EV, result, _ms(started_worker))
    return result


def _is_dag_checkpoint(state) -> bool:
    """Whether a checkpointed state belongs to a dag execution."""
    gov = getattr(state, "governance", None) or {}
    return isinstance(gov, dict) and gov.get("workflow") == "dag"


def _resume_dag_execution(execution_id: str, checkpoint_path: str,
                          state, approval: Any, task_id: str,
                          collector: Any = None) -> P.PipelineResult:
    """Resume a suspended DAG execution WITHOUT re-entering the graph.

    Re-invoking the LangGraph graph here would re-run the worker (observed
    2026-09-06: validate → execute re-executed with no adapter). The worker
    already ran inside `run_protected`; only the human decision is pending,
    so the pending state is resolved and completed with the same Tabularium-owned
    steps the DAG itself uses — no framework involved.
    """
    from tabularium.engine.observability import Tracer
    from tabularium.engine.observability import events as EV
    from tabularium.engine.runtime import LangGraphRuntime
    from tabularium.engine.runtime import steps as S

    tracer = Tracer(collector, execution_id, task_id)
    runtime = LangGraphRuntime(checkpoint_path=checkpoint_path)
    try:
        verdict = S.resolve_approval(state, approval)
        if (verdict.approval_resolution or {}).get("decision") != "APPROVE":
            # Denied, cancelled, or refused: persist the outcome without
            # asking any runtime to continue at all.
            outcome = runtime._write_terminal(execution_id, verdict)
        else:
            outcome = runtime._write_terminal(execution_id, S.complete(verdict))
    except Exception as exc:  # noqa: BLE001
        failure = P.PipelineResult(
            status=P.FAILED, execution_id=execution_id, task_id=task_id,
            reason_codes=(P.RUNTIME_START_FAILED,),
            diagnostics=({"code": P.RUNTIME_START_FAILED,
                          "detail": _detail(exc)},))
        tracer.terminal(failure.status, component=EV.RUNTIME, status=EV.ERROR,
                        reason_codes=failure.reason_codes)
        return failure

    plan_stub = P.ExecutionPlan(project_root="", task_id=task_id, intent="",
                                execution_id=execution_id,
                                checkpoint_path=checkpoint_path)
    gov = dict(getattr(state, "governance", None) or {})
    result = _from_runtime(plan_stub, outcome, None, gov)
    if result.status != P.AWAITING_APPROVAL:
        tracer.mark(EV.APPROVAL_RESOLVED, component=EV.APPROVAL,
                    attributes={"outcome": result.status})
    tracer.terminal(result.status, component=EV.RUNTIME,
                    status=(EV.ERROR if result.status == P.FAILED else EV.OK),
                    reason_codes=tuple(result.reason_codes))
    return result


def explain_execution(plan: P.ExecutionPlan,
                    collector: Any = None) -> P.PreflightResult:
    """Run ONLY the preparation stages of the governed pipeline.

    Stages 1-5 execute exactly as a real execution would — same loader, same
    governance checks, same worker-compatibility gate, same projection, same
    WorkerRequest construction — and then stop BEFORE stage 6: no worker
    process starts, no provider is contacted, no checkpoint or runtime state
    exists afterward. The result carries stable product facts, never CLI
    presentation details.

    Truthfulness rule: a real run whose governed-runtime dependency is absent
    REFUSES at stage 6 with RUNTIME_START_FAILED before any execution begins.
    Preflight predicts that refusal from the same fact (a read-only module
    probe, the same method `tabu doctor` uses for optional capabilities)
    rather than reporting a would-run that cannot happen.
    """
    from tabularium.engine.observability import Tracer
    from tabularium.engine.observability import events as EV

    tracer = Tracer(collector, plan.execution_id, plan.task_id)
    prepared = _prepare(plan, tracer, EV)
    if isinstance(prepared, P.PipelineResult):
        return P.PreflightResult(
            status=P.PREFLIGHT_REFUSED, task_id=plan.task_id,
            reason_codes=prepared.reason_codes,
            metrics=prepared.metrics,
            diagnostics=prepared.diagnostics)

    _request, metrics, _governance, approval_required = prepared

    if not _governed_runtime_dependency_present():
        # Mirror the exact refusal shape stage 6 would produce: the lazy
        # runtime loader's ImportError becomes RUNTIME_START_FAILED with this
        # bounded detail, and downstream guidance matches verbatim.
        return P.PreflightResult(
            status=P.PREFLIGHT_REFUSED, task_id=plan.task_id,
            reason_codes=(P.RUNTIME_START_FAILED,),
            metrics=metrics,
            diagnostics=({"code": P.RUNTIME_START_FAILED,
                          "detail": _RUNTIME_DEPENDENCY_PROBE_DETAIL},))

    descriptor = plan.adapter.descriptor
    api_model = getattr(descriptor, "category", "") == API_MODEL
    return P.PreflightResult(
        status=P.PREFLIGHT_READY, task_id=plan.task_id,
        worker_id=getattr(descriptor, "worker_id", ""),
        worker_category=getattr(descriptor, "category", ""),
        resolved_model=(plan.model_target.model
                        if api_model and plan.model_target is not None
                        else ""),
        approval_required=approval_required,
        metrics=metrics)


# The modules the lazy runtime loader (tabularium.engine.runtime.langgraph_runtime)
# imports on first use. Probed read-only; nothing is imported here beyond
# what find_spec itself resolves.
_RUNTIME_DEPENDENCY_MODULES = (
    "langgraph", "langgraph.checkpoint", "langgraph.checkpoint.sqlite",
    "langgraph.graph", "langgraph.types")
# Matches the real loader's ImportError text so downstream guidance and
# reason-code handling behave identically for predicted and actual refusals.
_RUNTIME_DEPENDENCY_PROBE_DETAIL = (
    "ImportError: the governed runtime requires LangGraph; install the "
    "Tabularium 'runtime' extra (pyproject optional-dependencies). Every other "
    "Tabularium path works without it.")


def _governed_runtime_dependency_present() -> bool:
    """Read-only presence probe for the governed runtime's modules."""
    import importlib.util

    for name in _RUNTIME_DEPENDENCY_MODULES:
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, AttributeError, ValueError):
            return False
    return True


def _prepare(plan: P.ExecutionPlan, tracer, EV):
    """Stages 1-5: everything before the runtime boundary.

    Returns either a PREPARATION_REJECTED PipelineResult or the prepared
    `(WorkerRequest, metrics, governance, approval_required)` tuple. No
    worker runs and no runtime state is created here; this function holds no
    governance of its own — it sequences checks owned by M2, M3.4, M3.3,
    projection, and the caller's own requirements.
    """
    guard = _validate_plan(plan)
    if guard is not None:
        return guard

    # --- 1. governed context, from the real M2 loader ----------------------
    started = time.monotonic()
    try:
        package = load_context(plan.project_root, task=plan.task_id,
                               ai_os_root=plan.ai_os_root)
    except Exception as exc:  # noqa: BLE001 - a governed refusal, not a crash
        return _reject(plan, P.PROJECT_NOT_FOUND, _detail(exc))
    tracer.mark(EV.CONTEXT_LOADED, component=EV.LOADER,
                duration_ms=_ms(started),
                attributes={"package_status": package.package_status,
                            "selected_records": len(package.selected)})

    gate = _context_gate(plan, package)
    if gate is not None:
        return gate
    tracer.mark(EV.CONTEXT_GOVERNED, component=EV.LOADER,
                attributes={"actionable": True})

    # --- 2. effective constraints, BEFORE any projection -------------------
    constraints, denial = _effective_constraints(plan)
    if denial is not None:
        return denial
    tracer.mark(EV.SKILL_APPLIED, component=EV.SKILLS,
                attributes={"effective_context_classes":
                            list(constraints.context_classes),
                            "approval_required": constraints.approval_required,
                            "persistence": constraints.persistence,
                            "tool_classes": list(constraints.tool_classes)})

    # --- 3. worker compatibility, BEFORE execution -------------------------
    descriptor = plan.adapter.descriptor
    compatibility = check_worker(constraints, descriptor)
    if not compatibility.compatible:
        return _reject(plan, P.WORKER_INCOMPATIBLE,
                       ", ".join(compatibility.reason_codes))

    # The caller's own requirements are checked HERE rather than left to the
    # adapter's `accepts`. An adapter is free to be permissive, and a
    # mismatch discovered inside the runtime would already have started an
    # execution and written a checkpoint.
    if plan.required_category and plan.required_category != descriptor.category:
        return _reject(plan, P.WORKER_INCOMPATIBLE,
                       f"required category {plan.required_category} but the "
                       f"worker is {descriptor.category}")
    missing = sorted(set(plan.required_capabilities)
                     - set(descriptor.capabilities))
    if missing:
        return _reject(plan, P.WORKER_INCOMPATIBLE,
                       f"worker lacks required capabilities: "
                       f"{', '.join(missing)}")
    if plan.adapter.descriptor.category == API_MODEL and \
            plan.model_target is None:
        # Never guessed: choosing a model is a cost and capability decision
        # that belongs to routing, which does not exist yet (M3.6).
        return _reject(plan, P.MODEL_TARGET_REQUIRED,
                       "an API worker requires an explicitly resolved model")

    # --- 4. projection, restricted to the permitted classes ----------------
    projected_at = time.monotonic()
    projection = project(package, intent=plan.intent, profile=plan.profile,
                         allowed_sections=constraints.context_classes)
    if not projection.projected:
        return _reject(plan, P.PROJECTION_FAILED,
                       ", ".join(projection.reason_codes),
                       extra_codes=projection.reason_codes)

    metrics = _metrics(projection, constraints)
    tracer.mark(EV.CONTEXT_PROJECTED, component=EV.PROJECTION,
                duration_ms=_ms(projected_at),
                attributes={"available_chars":
                            metrics.available_context.get("chars"),
                            "governed_chars":
                            metrics.governed_context.get("chars"),
                            "payload_chars": metrics.payload_size.get("chars"),
                            "payload_tokens":
                            metrics.payload_size.get("estimated_tokens"),
                            "reduction_pct": metrics.reduction_pct,
                            "projected_sections":
                            list(metrics.projected_sections)})

    # --- 5. Policy enforcement ---------------------------------------------
    policy_verdict = evaluate_policies(plan, package, projection, constraints)
    if not policy_verdict.allowed:
        details = "; ".join(v.detail for v in policy_verdict.violations)
        return _reject(
            plan,
            P.POLICY_VIOLATION,
            f"policy enforcement refused execution: {details}",
            extra_codes=tuple(dict.fromkeys(v.code for v in policy_verdict.violations)),
            diagnostics=tuple({"code": v.code, "detail": f"{v.policy}: {v.detail}"}
                              for v in policy_verdict.violations),
        )

    # --- 6. WorkerRequest from the PROJECTED PAYLOAD -----------------------
    approval_required = _effective_approval(
        plan, constraints, policy_approval=policy_verdict.requires_approval
    )
    governance = {
        "approval_required": approval_required,
        "policy_approval_reasons": list(policy_verdict.approval_reasons),
        "persistence": constraints.persistence,
        "output_contract": constraints.output_contract,
        "profile": plan.profile,
        "task_id": plan.task_id,
        "workflow": getattr(plan, "workflow", "single"),
    }
    request = WorkerRequest(
        request_id=plan.execution_id,
        intent=plan.intent,
        payload=projection.payload,          # never the Context Package
        task_id=plan.task_id,
        required_category=plan.required_category,
        required_capabilities=tuple(plan.required_capabilities),
        model_requirements=({"resolved_model": plan.model_target}
                            if plan.model_target is not None else {}),
        budget={"profile": plan.profile,
                "limit_tokens": DEFAULT_LIMITS.get(plan.profile)},
        governance=dict(governance),
        correlation={"execution_id": plan.execution_id,
                     "task_id": plan.task_id})

    tracer.mark(EV.WORKER_PREPARED, component=EV.PIPELINE,
                attributes={"required_category": plan.required_category or "",
                            "worker_id": plan.adapter.descriptor.worker_id,
                            "approval_required": approval_required})
    return request, metrics, governance, approval_required


def resume_execution(execution_id: str, checkpoint_path: str,
                     approval: Any = None, adapter: Any = None,
                     task_id: str = "",
                     collector: Any = None) -> P.PipelineResult:
    """Resume a suspended execution with a HUMAN APPROVAL RESOLUTION.

    `approval` is a `HumanApprovalResolution` (or its dict form) carrying the
    decision plus who made it, when, and over what scope. The caller never
    constructs a LangGraph object.

    Validation belongs entirely to the runtime, which owns it. Nothing is
    re-validated here: a second validator would be a second place where
    "resume means approve" could drift into existence.

    This authorizes the RUNTIME to continue past this boundary. It is not a
    canonical approval record in project truth — M4.4 owns persistence.
    """
    from tabularium.engine.observability import Tracer
    from tabularium.engine.observability import events as EV
    from tabularium.engine.runtime import LangGraphRuntime

    tracer = Tracer(collector, execution_id, task_id)
    try:
        runtime = LangGraphRuntime(checkpoint_path=checkpoint_path)
        current = runtime.inspect_execution(execution_id)
        if current is not None and not current.is_terminal and \
                current.runtime_status == P.AWAITING_APPROVAL and \
                _is_dag_checkpoint(current):
            return _resume_dag_execution(execution_id, checkpoint_path,
                                         current, approval, task_id,
                                         collector)
        outcome = runtime.resume(execution_id, approval, adapter=adapter)
    except Exception as exc:  # noqa: BLE001
        failure = P.PipelineResult(
            status=P.FAILED, execution_id=execution_id, task_id=task_id,
            reason_codes=(P.RUNTIME_START_FAILED,),
            diagnostics=({"code": P.RUNTIME_START_FAILED,
                          "detail": _detail(exc)},))
        tracer.terminal(failure.status, component=EV.RUNTIME, status=EV.ERROR,
                        reason_codes=failure.reason_codes)
        return failure

    plan_stub = P.ExecutionPlan(project_root="", task_id=task_id, intent="",
                                execution_id=execution_id,
                                checkpoint_path=checkpoint_path)
    result = _from_runtime(plan_stub, outcome, None, {})
    # Emitted only after the runtime accepted the resolution: a rejected or
    # malformed resolution never reaches here as APPROVAL_RESOLVED.
    if result.status != P.AWAITING_APPROVAL:
        tracer.mark(EV.APPROVAL_RESOLVED, component=EV.APPROVAL,
                    attributes={"outcome": result.status})
    tracer.terminal(result.status, component=EV.RUNTIME,
                    status=(EV.ERROR if result.status == P.FAILED else EV.OK),
                    reason_codes=tuple(result.reason_codes))
    return result


def inspect_execution(execution_id: str, checkpoint_path: str):
    """Read-only view of a suspended or finished execution."""
    from tabularium.engine.runtime import LangGraphRuntime

    return LangGraphRuntime(
        checkpoint_path=checkpoint_path).inspect_execution(execution_id)


# --- stages -----------------------------------------------------------------


def _validate_plan(plan: P.ExecutionPlan) -> P.PipelineResult | None:
    if not plan.task_id or not str(plan.task_id).strip():
        # M4.2 requires an EXPLICIT target. Automatic "continue the project"
        # discovery is a later unit, and guessing a Task would be exactly the
        # silent decision this layer must not make.
        return _reject(plan, P.TASK_TARGET_REQUIRED,
                       "an explicit task_id is required; automatic task "
                       "discovery is not implemented")
    if plan.adapter is None:
        return _reject(plan, P.WORKER_REQUIRED,
                       "a WorkerAdapter is required; the pipeline performs no "
                       "worker routing")
    if not plan.checkpoint_path or not str(plan.checkpoint_path).strip():
        return _reject(plan, P.CHECKPOINT_PATH_REQUIRED,
                       "an explicit checkpoint path is required")
    if getattr(plan, "workflow", "single") not in ("single", "dag"):
        # Explicit opt-in only: the pipeline never guesses an orchestration.
        return _reject(plan, P.WORKFLOW_SELECTION_INVALID,
                       f"unknown workflow '{getattr(plan, 'workflow', None)}'; "
                       "choose 'single' or 'dag'")
    if _inside_project(plan.project_root, plan.checkpoint_path):
        # Operational state must not live where project truth lives.
        return _reject(plan, P.CHECKPOINT_INSIDE_PROJECT,
                       "the checkpoint path is inside the project .ai/ tree")
    return None


def _context_gate(plan: P.ExecutionPlan, package) -> P.PipelineResult | None:
    """Nothing reaches a worker unless M2 says the context is usable."""
    # Blocking diagnostics are the rejection evidence. Unreadable record
    # directories travel with them even though the loader marks them
    # non-blocking: an unresolvable root Task behind an unlistable directory
    # must stay distinguishable from an id that genuinely resolves to nothing.
    diagnostics = tuple(
        {"code": d.code, "detail": d.reason[:160],
         "sources": [str(source) for source in d.sources]}
        for d in package.diagnostics
        if d.blocking_dimension != "NEITHER"
        or d.code == "RECORD_DIRECTORY_UNREADABLE")

    if package.package_status != READY:
        return _reject(plan, P.CONTEXT_PACKAGE_NOT_READY,
                       f"package_status is {package.package_status}",
                       diagnostics=diagnostics)
    if package.task_actionability != ACTIONABLE:
        return _reject(plan, P.TASK_NOT_ACTIONABLE,
                       f"task_actionability is {package.task_actionability}",
                       diagnostics=diagnostics)
    return None


def _effective_constraints(plan: P.ExecutionPlan):
    """Control-plane grant INTERSECT skill limits. A skill can only narrow."""
    grant = plan.grant or ControlPlaneGrant()

    if plan.skill_path is None:
        # No skill selected: the control-plane grant stands on its own.
        from tabularium.engine.skills.model import EffectiveConstraints
        constraints = EffectiveConstraints(
            worker_categories=tuple(grant.worker_categories),
            context_classes=tuple(grant.context_classes),
            tool_classes=tuple(grant.tool_classes),
            approval_required=bool(grant.approval_required),
            persistence=grant.persistence,
            output_contract=None)
        return constraints, _require_governance_section(plan, constraints)

    loaded = load_skill(plan.skill_path)
    if not loaded.valid:
        code = (P.SKILL_NOT_FOUND
                if "NOT_A_SKILL_PACKAGE" in loaded.reason_codes
                else P.SKILL_MALFORMED)
        return None, _reject(plan, code, ", ".join(loaded.reason_codes),
                             extra_codes=loaded.reason_codes)

    verdict = evaluate(loaded.package.sidecar, grant)
    if not verdict.allowed:
        return None, _reject(plan, P.SKILL_CONSTRAINT_VIOLATION,
                             ", ".join(verdict.reason_codes),
                             extra_codes=verdict.reason_codes)
    return verdict.constraints, _require_governance_section(
        plan, verdict.constraints)


def _require_governance_section(plan, constraints) -> P.PipelineResult | None:
    """`required` must be permitted, or the execution is refused.

    The `required` section carries package status, task actionability, root
    Task identity, approval state, and blocking diagnostics. Projecting
    without it would let a worker act without knowing whether it is permitted
    to, so an effective class set that excludes it is refused rather than
    quietly overridden.
    """
    if REQUIRED_SECTION not in constraints.context_classes:
        return _reject(plan, P.SKILL_CONSTRAINT_VIOLATION,
                       "effective context classes exclude the mandatory "
                       "'required' governance section")
    return None


def _effective_approval(plan: P.ExecutionPlan, constraints,
                        policy_approval: bool = False) -> bool:
    """Lower bound: ANY source requiring approval makes approval required.

    A skill declaring `approval_required: false` can never clear a
    control-plane, caller, or policy requirement.
    """
    grant = plan.grant or ControlPlaneGrant()
    return bool(plan.approval_required
                or getattr(grant, "approval_required", False)
                or constraints.approval_required
                or policy_approval)


def _metrics(projection, constraints) -> P.ContextMetrics:
    economy = projection.economy
    return P.ContextMetrics(
        available_context=dict(economy.available_context),
        governed_context=dict(economy.governed_context),
        payload_size=dict(economy.payload_size),
        context_avoided=dict(economy.context_avoided),
        reduction_pct=economy.reduction_pct,
        effective_allowed_classes=tuple(constraints.context_classes),
        projected_sections=tuple(projection.payload.section_names()))


def _from_runtime(plan, outcome, metrics, governance) -> P.PipelineResult:
    """Map a RuntimeResult onto the pipeline vocabulary."""
    mapping = {"COMPLETED": P.COMPLETED,
               "AWAITING_APPROVAL": P.AWAITING_APPROVAL,
               "REJECTED": P.REJECTED,
               "CANCELLED": P.CANCELLED,
               "FAILED": P.FAILED}
    status = mapping.get(outcome.runtime_status, P.FAILED)
    return P.PipelineResult(
        status=status,
        execution_id=outcome.execution_id or plan.execution_id,
        task_id=plan.task_id,
        reason_codes=tuple(outcome.reason_codes),
        metrics=metrics,
        approval_request=outcome.approval_request,
        worker_result=outcome.worker_result,
        # Output only on completion — the runtime already gates this, and the
        # pipeline does not widen it.
        output=outcome.output if status == P.COMPLETED else "",
        governance=dict(governance),
        diagnostics=tuple(outcome.diagnostics))


# --- helpers ----------------------------------------------------------------


def _reject(plan, code: str, detail: str, *, extra_codes=(),
            diagnostics=(), metrics=None) -> P.PipelineResult:
    """A preparation refusal. No worker ran; no runtime execution began."""
    return P.PipelineResult(
        status=P.PREPARATION_REJECTED,
        execution_id=plan.execution_id,
        task_id=plan.task_id,
        reason_codes=tuple(dict.fromkeys((code,) + tuple(extra_codes))),
        metrics=metrics,
        diagnostics=((({"code": code, "detail": detail},)) + tuple(diagnostics)))


def _inside_project(project_root: str, checkpoint_path: str) -> bool:
    """Whether a checkpoint would land inside the project's `.ai/` tree."""
    if not project_root:
        return False
    try:
        ai_root = os.path.realpath(os.path.join(project_root, ".ai"))
        target = os.path.realpath(checkpoint_path)
    except (OSError, ValueError):
        return False
    return target == ai_root or target.startswith(ai_root + os.sep)


def _ms(started: float) -> int:
    """Elapsed milliseconds from a monotonic start. Never a wall clock."""
    return int((time.monotonic() - started) * 1000)


def _trace_runtime_outcome(tracer, EV, result, worker_ms: int) -> None:
    """Emit worker/approval events AFTER the runtime already reported them.

    Nothing here is speculative: each event describes a fact the runtime has
    already committed to its checkpoint. An execution suspended for approval
    reports the pending request, not a completion it has not reached.
    """
    if result.status == P.AWAITING_APPROVAL:
        pending = result.approval_request
        request_id = getattr(pending, "request_id", "") or (
            pending.get("request_id", "") if isinstance(pending, dict) else "")
        tracer.mark(EV.WORKER_COMPLETED, component=EV.WORKER,
                    duration_ms=worker_ms,
                    attributes={"worker_id": (result.worker_result or {}).get(
                        "worker_id", "")})
        tracer.mark(EV.APPROVAL_REQUESTED, component=EV.APPROVAL,
                    status=EV.PENDING,
                    approval_request_id=str(request_id))
        return
    outcome = result.worker_result or {}
    if outcome:
        completed = outcome.get("status") == "COMPLETED"
        # Token counts and cost come from the worker's own usage report, which
        # for an API worker is what the PROVIDER returned (M3.6). Nothing is
        # estimated here: an absent count stays absent rather than becoming 0,
        # and no price table exists to guess a cost from.
        usage = outcome.get("usage") or {}
        attributes = {"worker_id": outcome.get("worker_id", ""),
                      "worker_status": outcome.get("status", "")}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens",
                    "cost_usd", "model"):
            if usage.get(key) is not None:
                attributes[key] = usage[key]
        tracer.mark(EV.WORKER_COMPLETED if completed else EV.WORKER_FAILED,
                    component=EV.WORKER, duration_ms=worker_ms,
                    status=(EV.OK if completed else EV.ERROR),
                    attributes=attributes)


def _detail(exc: BaseException, limit: int = 200) -> str:
    """Bounded, secret-scrubbed detail. Never a raw provider or framework dump."""
    hints = ("sk-", "api_key", "authorization", "bearer ", "secret", "password")
    text = " ".join(str(exc).split())
    if any(hint in text.lower() for hint in hints):
        return f"{type(exc).__name__} (detail withheld)"
    return f"{type(exc).__name__}: {text}"[:limit]

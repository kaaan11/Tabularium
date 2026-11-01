"""Preflight product surface: ``tabu explain`` and ``run --dry-run``.

Both commands are alternate views of ONE engine: they evaluate every gate
a real ``run`` performs — target, instruction, project health, Task selection,
worker selection, then pipeline stages 1-5 through `explain_execution` — and
stop before stage 6. No worker process starts, no provider is contacted,
nothing is written anywhere (including `.execution`). Refusal codes,
messages, and recovery guidance are produced by the same mapping the real
run uses (`_from_pipeline` over a synthetic PREPARATION_REJECTED result), so
an operator is told exactly what a run would have said.

Human and JSON renderings carry the same stable fact set. Raw projected
payloads, environment values, credentials, and exception internals are never
exposed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tabularium.engine.execution import (ExecutionPlan, PipelineResult,
                               PREFLIGHT_READY, PREFLIGHT_REFUSED,
                               PREPARATION_REJECTED)
from tabularium.engine.workers.api import ModelTarget

from .diagnostics import status_project
from .execution import (MODEL_IGNORED_BY_NATIVE_WARNING, INVALID_PROJECT,
                        PROJECT_PATH_INVALID, WORKER_UNAVAILABLE,
                        _checkpoint_path, _choose_task, _from_pipeline,
                        _health_gate, _refusal, _select_worker, _target)

# The instruction gate's stable code is a literal at the run entrypoint.
INSTRUCTION_REQUIRED = "INSTRUCTION_REQUIRED"

WOULD_RUN = "WOULD_RUN"
REFUSED = "REFUSED"

# A preflight never enters runtime, so it never consumes the wall-clock id
# space real executions use. This sentinel exists only to satisfy the plan
# contract and is never persisted.
_PREFLIGHT_EXECUTION_ID = "preflight"

_API_MODEL_CATEGORY = "API_MODEL"
_NATIVE_WARNING_CATEGORY = "CLI_CODING"


@dataclass(frozen=True)
class GateFact:
    """One ordered preflight gate outcome; presentation-neutral."""

    gate: str
    outcome: str          # PASSED / REFUSED / SKIPPED
    detail: str = ""

    def to_dict(self) -> dict[str, str]:
        out = {"gate": self.gate, "outcome": self.outcome}
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class WorkerFacts:
    worker_id: str
    category: str
    resolved_model: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"worker_id": self.worker_id, "category": self.category,
                "resolved_model": self.resolved_model}


@dataclass(frozen=True)
class ExplainResult:
    """The complete stable fact set of one preflight."""

    command: str                 # "explain" or "dry-run"
    status: str                  # WOULD_RUN / REFUSED
    target: str
    code: str | None = None
    message: str = ""
    reason_codes: tuple[str, ...] = ()
    task_id: str | None = None
    requested_task: str | None = None
    worker: WorkerFacts | None = None
    approval_required: bool | None = None
    metrics: dict | None = None
    gates: tuple[GateFact, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def would_run(self) -> bool:
        return self.status == WOULD_RUN

    @property
    def exit_code(self) -> int:
        if self.code in {PROJECT_PATH_INVALID, "TARGET_NOT_FOUND",
                         "TARGET_NOT_DIRECTORY"}:
            return 2
        return 0 if self.would_run else 1

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "command": self.command,
            "target": self.target,
            "would_run": self.would_run,
            "status": self.status,
            "code": self.code,
            "reason_codes": list(self.reason_codes),
            "message": self.message,
            "task": self.task_id,
            "worker": self.worker.to_dict() if self.worker else None,
            "approval_required": self.approval_required,
            "metrics": self.metrics,
            "gates": [gate.to_dict() for gate in self.gates],
            "warnings": list(self.warnings),
            "exit_code": self.exit_code,
        }
        if self.requested_task is not None:
            out["requested_task"] = self.requested_task
        return out


def preflight(instruction: str, path: str | Path | None = None, *,
              task_id: str | None = None, model: str | None = None,
              worker: str | None = None, adapter: Any = None,
              workflow: str | None = None,
              command: str = "explain") -> ExplainResult:
    """Run every pre-runtime gate exactly as `run` would, then stop."""
    target, error = _target(path)
    requested = task_id
    if error:
        return _project(command, error, requested_task=requested)
    if not isinstance(instruction, str) or not instruction.strip():
        return _project(
            command,
            _refusal(target, INSTRUCTION_REQUIRED,
                     "An instruction is required."),
            requested_task=requested)
    gates: list[GateFact] = [GateFact("target", "PASSED"),
                             GateFact("instruction", "PASSED")]

    try:
        health = status_project(target)
    except (ImportError, OSError, RuntimeError, ValueError):
        return _project(
            command,
            _refusal(target, INVALID_PROJECT,
                     "The project could not be inspected. Run "
                     "`tabu doctor` and correct the reported project issue."),
            gates=gates, requested_task=requested)
    gate = _health_gate(target, health)
    if gate:
        return _project(command, gate, gates=gates, requested_task=requested)
    gates.append(GateFact("project_health", "PASSED"))

    chosen_task, task_error = _choose_task(target, task_id)
    if task_error:
        return _project(command, task_error, gates=gates,
                        requested_task=requested)
    gates.append(GateFact("task_selection", "PASSED",
                          detail=chosen_task or ""))

    selected_model = (model or os.environ.get("TABU_MODEL") or "").strip()
    selected, selection_error = _select_worker(target, worker,
                                               selected_model, adapter)
    if selection_error:
        return _project(command, selection_error, gates=gates,
                        task_id=chosen_task, requested_task=requested)
    warnings: list[str] = []
    if selected_model and \
            getattr(selected.descriptor, "category", "") == \
            _NATIVE_WARNING_CATEGORY:
        warnings.append(MODEL_IGNORED_BY_NATIVE_WARNING)
    if getattr(selected.descriptor, "category", "") == _API_MODEL_CATEGORY \
            and not _litellm_present():
        # Factual environment notice in the doctor's capability spirit: the
        # execution would start and then fail until the API extra exists.
        warnings.append(
            "Warning: the optional API extra (litellm) is not installed in "
            'this environment; install it with pip install -e ".[api]" from '
            'a source checkout, or pip install "<release-wheel>[api]" from a '
            "downloaded artifact, before running with --worker api.")
    resolved_model = getattr(selected, "model_target", None)
    if selected_model and resolved_model is None:
        resolved_model = ModelTarget(selected_model)
    gates.append(GateFact("worker_selection", "PASSED",
                          detail=getattr(selected.descriptor, "worker_id",
                                         "")))

    plan = ExecutionPlan(
        project_root=str(target),
        task_id=chosen_task,
        intent=instruction.strip(),
        execution_id=_PREFLIGHT_EXECUTION_ID,
        checkpoint_path=str(_checkpoint_path(target)),
        workflow=(workflow if isinstance(workflow, str)
                  else ("single" if workflow is None else "")),
        adapter=selected,
        model_target=(resolved_model if getattr(selected.descriptor,
                                                "category", "") ==
                      _API_MODEL_CATEGORY else None),
    )
    from tabularium.engine.execution import explain_execution
    try:
        preflight_result = explain_execution(plan)
    except ImportError as exc:
        return _project(
            command,
            _refusal(target, WORKER_UNAVAILABLE,
                     "The selected worker/runtime is unavailable. Install "
                     "the existing Tabularium runtime extra and configure the "
                     "selected worker.",
                     detail=str(exc)),
            gates=gates, task_id=chosen_task, requested_task=requested,
            worker=selected)
    except (OSError, RuntimeError, ValueError) as exc:
        return _project(
            command,
            _refusal(target, WORKER_UNAVAILABLE,
                     "The selected worker could not be started. Configure "
                     "the selected worker and try again.",
                     detail=str(exc)),
            gates=gates, task_id=chosen_task, requested_task=requested,
            worker=selected)

    api_category = getattr(selected.descriptor, "category", "") == \
        _API_MODEL_CATEGORY
    worker_facts = WorkerFacts(
        getattr(selected.descriptor, "worker_id", ""),
        getattr(selected.descriptor, "category", ""),
        resolved_model=(getattr(resolved_model, "model", "")
                        if api_category else ""))

    if preflight_result.status == PREFLIGHT_REFUSED:
        # Reuse the REAL refusal mapping so codes, messages, and recovery
        # guidance match what the actual run would have said.
        synthetic = PipelineResult(
            status=PREPARATION_REJECTED,
            execution_id=_PREFLIGHT_EXECUTION_ID,
            task_id=preflight_result.task_id,
            reason_codes=preflight_result.reason_codes,
            metrics=preflight_result.metrics,
            diagnostics=preflight_result.diagnostics)
        projected = _from_pipeline(target, synthetic, warnings=warnings)
        result = _project(command, projected, gates=gates,
                          task_id=chosen_task, requested_task=requested)
        result_gates = (*result.gates,
                        GateFact("preparation", "REFUSED",
                                 detail=projected.code))
        return ExplainResult(
            command=result.command, status=result.status,
            target=result.target, code=result.code,
            message=result.message, reason_codes=result.reason_codes,
            task_id=result.task_id, requested_task=result.requested_task,
            worker=worker_facts,
            approval_required=preflight_result.approval_required,
            metrics=(preflight_result.metrics.to_dict()
                     if preflight_result.metrics else None),
            gates=result_gates, warnings=tuple(warnings))

    metrics = (preflight_result.metrics.to_dict()
               if preflight_result.metrics else None)
    return ExplainResult(
        command=command, status=WOULD_RUN, target=str(target),
        message="All preflight gates passed: this instruction WOULD start "
                "governed execution now.",
        task_id=chosen_task, requested_task=requested,
        worker=worker_facts,
        approval_required=preflight_result.approval_required,
        metrics=metrics,
        gates=tuple(gates) + (GateFact("preparation", "PASSED"),),
        warnings=tuple(warnings))


def _litellm_present() -> bool:
    """Read-only presence probe for the optional API extra (doctor's method)."""
    import importlib.util

    try:
        return importlib.util.find_spec("litellm") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _project(command: str, product, *, gates=(), task_id=None,
             requested_task=None, worker=None) -> ExplainResult:
    """Project a ProductResult-shaped outcome onto the explain fact set."""
    worker_facts = None
    if worker is not None:
        descriptor = getattr(worker, "descriptor", None)
        worker_facts = WorkerFacts(getattr(descriptor, "worker_id", ""),
                                   getattr(descriptor, "category", ""))
    return ExplainResult(
        command=command, status=REFUSED, target=str(product.target or ""),
        code=product.code, message=product.message,
        reason_codes=(product.code,) if product.code else (),
        task_id=task_id, requested_task=requested_task,
        worker=worker_facts,
        gates=tuple(gates),
        warnings=tuple(getattr(product, "warnings", ())))


def render_human(result: ExplainResult) -> str:
    origin = ("run --dry-run" if result.command == "dry-run"
              else result.command)
    lines = [f"Tabularium preflight ({origin})",
             f"Project: {result.target}"]
    for gate in result.gates:
        line = f"[{gate.outcome}] {gate.gate}"
        if gate.outcome != "PASSED" and gate.detail:
            line += f": {gate.detail}"
        lines.append(line)
    if result.task_id:
        lines.append(f"Task: {result.task_id}")
    if result.worker is not None:
        lines.append(f"Worker: {result.worker.worker_id} "
                     f"[{result.worker.category}]")
        if result.worker.resolved_model:
            lines.append(f"Model: {result.worker.resolved_model}")
    if result.approval_required:
        lines.append("Approval required during execution: yes")
    if result.metrics:
        governed = (result.metrics.get("governed_context") or {}).get(
            "estimated_tokens")
        payload = (result.metrics.get("payload_size") or {}).get(
            "estimated_tokens")
        reduction = result.metrics.get("reduction_pct")
        if governed is not None:
            lines.append(f"Context: {governed:,} governed estimated tokens")
        if payload is not None:
            lines.append(f"Worker payload: {payload:,} estimated tokens")
        if reduction is not None:
            lines.append(f"Reduction: {reduction}%")
    if result.would_run:
        lines.append("Status: would-run")
        lines.append(result.message)
        return "\n".join(lines)
    lines.append("Status: refused")
    if result.code:
        lines.append(f"Reason: {result.code}")
    lines.append(result.message)
    return "\n".join(lines)


def render_json(result: ExplainResult) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)


__all__ = ["WOULD_RUN", "REFUSED", "ExplainResult", "GateFact",
           "WorkerFacts", "preflight", "render_human", "render_json"]

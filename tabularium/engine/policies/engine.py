"""Executable policy enforcement engine (Step 5).

Rules Enforced:
1. Model Routing: If a Task declares `assigned_model`, any planned `model_target`
   MUST match it exactly. Fail-closed on mismatch (`MODEL_ASSIGNMENT_MISMATCH`).
2. Risk Management: If a Task has risk HIGH or CRITICAL, human approval is
   strictly required (`RISK_APPROVAL_REQUIRED`).
   If `approved_scope.approved_risk` is defined and Task `risk` exceeds that
   ceiling, fail-closed (`SCOPE_RISK_EXCEEDED`).
3. Security: No secret / token / key shapes may appear in the projected payload
   or intent (`SECRET_DETECTED_IN_CONTEXT`).

Token budget is NOT enforced here. The projection stage owns it and refuses
over-budget work before this engine runs (`tabularium/engine/projection/projector.py`);
a second ceiling here would be a duplicate authority, not a second defense.
"""

from __future__ import annotations

from typing import Any

from tabularium.engine.persistence.model import contains_secret
from .model import (
    MODEL_ASSIGNMENT_MISMATCH,
    POLICY_INPUT_INVALID,
    RISK_APPROVAL_REQUIRED,
    RISK_LEVELS,
    RISK_RANK,
    SCOPE_RISK_EXCEEDED,
    SECRET_DETECTED_IN_CONTEXT,
    PolicyVerdict,
    PolicyViolation,
)


MAX_SCAN_DEPTH = 64
"""Deepest section nesting the secret scan will walk.

Projector output is far shallower than this. The bound exists so that a
pathological structure cannot turn the scan into a RecursionError on the
execution path: exceeding it is reported as an unscannable section and
refuses the execution, never as a silent pass.
"""


class _TooDeep(Exception):
    """A section nested past MAX_SCAN_DEPTH; it cannot be proven secret-free."""


def _strings(value: Any, depth: int = 0):
    """Every string reachable in a projected section value.

    Payload sections are JSON-safe and finite (projector.py builds them). Keys
    are included: a secret can hide in a field name.
    """
    if depth > MAX_SCAN_DEPTH:
        raise _TooDeep
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item, depth + 1)
    elif value is not None:
        yield str(value)


def evaluate_policies(
    plan: Any,
    package: Any,
    projection: Any,
    constraints: Any = None,
) -> PolicyVerdict:
    """Evaluate all active policies against an execution plan and context package."""
    violations: list[PolicyViolation] = []
    requires_approval = False
    metadata: dict[str, Any] = {}

    root_task = getattr(package, "root_task", None) or {}
    # `.get("metadata", {})` would still hand back an explicit None. This engine
    # sits unguarded on the execution path, so a malformed record must produce a
    # refusal from the rules below, never an AttributeError out of the pipeline.
    task_meta = root_task.get("metadata") if isinstance(root_task, dict) else None
    if not isinstance(task_meta, dict):
        task_meta = {}

    # 1. Model Routing Policy Enforcement
    # If Task defines assigned_model, model_target must match it.
    assigned_model = task_meta.get("assigned_model")
    if assigned_model:
        metadata["assigned_model"] = assigned_model
        planned = plan.model_target
        # `model_target` is a ModelTarget on the API path and a bare string
        # elsewhere; pipeline.py:153 reads `.model` for the same reason.
        planned_model = getattr(planned, "model", planned)
        if planned is not None and planned_model != assigned_model:
            metadata["planned_model"] = planned_model
            violations.append(
                PolicyViolation(
                    policy="model-routing.md",
                    code=MODEL_ASSIGNMENT_MISMATCH,
                    detail=(
                        f"Task assigned model '{assigned_model}' does not match "
                        f"execution plan model target '{planned_model}'"
                    ),
                )
            )

    # 2. Risk Management Policy Enforcement
    task_risk = task_meta.get("risk")
    approval_reasons: list[str] = []
    if task_risk is not None:
        metadata["task_risk"] = task_risk
        if task_risk not in RISK_LEVELS:
            violations.append(
                PolicyViolation(
                    policy="risk-management.md",
                    code=POLICY_INPUT_INVALID,
                    detail=f"Task risk '{task_risk}' is not one of {RISK_LEVELS}",
                )
            )
        else:
            if task_risk in ("HIGH", "CRITICAL"):
                requires_approval = True
                approval_reasons.append(RISK_APPROVAL_REQUIRED)

            approved_scope = task_meta.get("approved_scope")
            if isinstance(approved_scope, dict) and "approved_risk" in approved_scope:
                approved_risk = approved_scope["approved_risk"]
                metadata["approved_risk_ceiling"] = approved_risk
                if approved_risk not in RISK_LEVELS:
                    violations.append(
                        PolicyViolation(
                            policy="risk-management.md",
                            code=POLICY_INPUT_INVALID,
                            detail=(
                                f"approved_scope.approved_risk '{approved_risk}' "
                                f"is not one of {RISK_LEVELS}"
                            ),
                        )
                    )
                elif RISK_RANK[task_risk] > RISK_RANK[approved_risk]:
                    violations.append(
                        PolicyViolation(
                            policy="risk-management.md",
                            code=SCOPE_RISK_EXCEEDED,
                            detail=(
                                f"Task risk '{task_risk}' exceeds approved scope "
                                f"risk ceiling '{approved_risk}'"
                            ),
                        )
                    )

    # 3. Security Policy Enforcement (Mandatory secret protection)
    # Check plan.intent
    if plan.intent and contains_secret(plan.intent):
        violations.append(
            PolicyViolation(
                policy="security.md",
                code=SECRET_DETECTED_IN_CONTEXT,
                detail="Secret or credential material detected in execution plan intent",
            )
        )

    # Check projection payload recursively
    payload = getattr(projection, "payload", None)
    if payload is not None:
        for section, content in getattr(payload, "sections", ()):
            try:
                found = any(contains_secret(text) for text in _strings(content))
            except _TooDeep:
                # Unscannable is not the same as clean: refuse rather than pass.
                violations.append(
                    PolicyViolation(
                        policy="security.md",
                        code=POLICY_INPUT_INVALID,
                        detail=(f"projected section '{section}' nests deeper than "
                                f"{MAX_SCAN_DEPTH} levels and cannot be scanned "
                                f"for secrets"),
                    )
                )
                break
            if found:
                violations.append(
                    PolicyViolation(
                        policy="security.md",
                        code=SECRET_DETECTED_IN_CONTEXT,
                        detail=f"Secret or credential material detected in projected section '{section}'",
                    )
                )
                break

    allowed = len(violations) == 0
    return PolicyVerdict(
        allowed=allowed,
        requires_approval=requires_approval if allowed else False,
        approval_reasons=tuple(approval_reasons) if allowed else (),
        violations=tuple(violations),
        metadata=metadata,
    )

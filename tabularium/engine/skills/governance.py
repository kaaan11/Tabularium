"""Effective skill authority, and worker/context compatibility (M3.4).

THE ONE RULE

    effective = control_plane_grant  INTERSECT  skill_sidecar

A skill may only NARROW what the control plane already permits. It can never
widen it. Every set-valued dimension here is computed with a set
intersection, so widening is not a rule that has to be enforced — it is not
expressible. Two dimensions are not sets and get explicit monotonic rules:

    approval_required   LOWER bound  -> logical OR
                        A skill may make execution stricter. A skill saying
                        `false` can never clear a control-plane `true`.

    persistence         UPPER bound  -> minimum by rank
                        A skill can only reduce what it is eligible to
                        request. It can never acquire write authority, and
                        the vocabulary has no value that would mean it.

These functions are pure. Once objects are parsed there is no filesystem, no
network, no model, and no mutation of any input.

This module computes eligibility. It does not ask a human, record an
approval, write anything, or select a worker: approval runtime, persistence,
and routing all belong to later milestones.
"""

from __future__ import annotations

from typing import Any

from .model import (APPROVAL_REQUIRED, CONTEXT_CLASS_DENIED,
                    NO_ELIGIBLE_WORKER_CATEGORY, OUTPUT_CONTRACT_MISMATCH,
                    PERSISTENCE_RANK, SKILL_ALLOWED, TOOL_CLASS_DENIED,
                    WORKER_CATEGORY_DENIED, Compatibility, ControlPlaneGrant,
                    EffectiveConstraints, GovernanceResult, SkillSidecar, code)


def _intersect(grant, skill) -> tuple[str, ...]:
    """Ordered, de-duplicated set intersection.

    Ordering follows the GRANT, not the skill, so a skill cannot influence
    anything by how it orders its own list. The result is deterministic.
    """
    allowed = set(skill)
    seen: list[str] = []
    for value in grant:
        if value in allowed and value not in seen:
            seen.append(value)
    return tuple(seen)


def evaluate(sidecar: SkillSidecar,
             grant: ControlPlaneGrant | None = None) -> GovernanceResult:
    """Combine a control-plane grant with a skill sidecar.

    Returns the effective constraints, or a denial when the intersection
    leaves the skill unable to run at all.
    """
    grant = grant or ControlPlaneGrant()

    workers = _intersect(grant.worker_categories,
                         sidecar.allowed_worker_categories)
    contexts = _intersect(grant.context_classes,
                          sidecar.allowed_context_classes)
    tools = _intersect(grant.tool_classes, sidecar.allowed_tool_classes)

    # Lower bound: either side may require approval; neither can waive it.
    approval = bool(grant.approval_required or sidecar.approval_required)

    # Upper bound: the weaker of the two, never the stronger.
    persistence = min(grant.persistence, sidecar.persistence,
                      key=lambda level: PERSISTENCE_RANK[level])

    codes: list[str] = []
    output: str | None = sidecar.output_contract
    if output not in grant.output_contracts:
        codes.append(code(OUTPUT_CONTRACT_MISMATCH, output))
        output = None

    if not workers:
        # Nothing left to execute on: the skill and the control plane share no
        # worker category.
        codes.append(NO_ELIGIBLE_WORKER_CATEGORY)

    constraints = EffectiveConstraints(
        worker_categories=workers, context_classes=contexts,
        tool_classes=tools, approval_required=approval,
        persistence=persistence, output_contract=output)

    if codes:
        return GovernanceResult(allowed=False,
                                reason_codes=tuple(sorted(set(codes))),
                                constraints=constraints)

    # APPROVAL_REQUIRED is advisory, not a denial: the skill is allowed, and
    # execution is gated on an approval that no code here grants.
    advisory = (SKILL_ALLOWED,) + ((APPROVAL_REQUIRED,) if approval else ())
    return GovernanceResult(allowed=True, reason_codes=advisory,
                            constraints=constraints)


def check_worker(constraints: EffectiveConstraints,
                 descriptor: Any) -> Compatibility:
    """Whether this worker is eligible under the effective constraints.

    Compatibility, not routing: this answers "may this worker run this skill",
    never "which worker should run it".
    """
    codes: list[str] = []
    if descriptor.category not in constraints.worker_categories:
        codes.append(code(WORKER_CATEGORY_DENIED, descriptor.category))
    return Compatibility(compatible=not codes, reason_codes=tuple(codes))


def check_payload(constraints: EffectiveConstraints,
                  payload: Any) -> Compatibility:
    """Whether a projected payload stays inside the skill's allowed classes.

    Context classes ARE payload section names, so this is an objective check
    against what the payload actually carries.

    Nothing is stripped and nothing is re-projected here. Projection is the
    only thing that decides payload content, and quietly removing a section
    after the fact would produce a payload whose reported economy metrics no
    longer describe it. An over-broad payload is an incompatibility to
    report, and the fix belongs upstream: intersect the skill's allowed
    classes into projection policy BEFORE projecting. Carrying the effective
    classes forward for that is an M3.5/M4 concern, and the data needed for it
    is exactly `constraints.context_classes`.
    """
    sections = payload.section_names() if hasattr(payload, "section_names") \
        else tuple(payload or ())
    allowed = set(constraints.context_classes)
    codes = [code(CONTEXT_CLASS_DENIED, name)
             for name in sections if name not in allowed]
    return Compatibility(compatible=not codes, reason_codes=tuple(codes))


def check_tools(constraints: EffectiveConstraints,
                requested: tuple[str, ...]) -> Compatibility:
    """Whether requested tool classes are within the effective set.

    Tools do not exist in M3. This is the skill-side upper bound only, kept so
    that M4 has a place to attach rather than a system to design from scratch.
    """
    allowed = set(constraints.tool_classes)
    codes = [code(TOOL_CLASS_DENIED, name)
             for name in requested if name not in allowed]
    return Compatibility(compatible=not codes, reason_codes=tuple(codes))

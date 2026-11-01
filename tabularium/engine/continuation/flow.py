"""Applying prepared continuation proposals (M4.5).

The ONLY durable write path reachable from here is
`persistence.apply_authorized_proposal`. This module imports no filesystem
write primitive — no `open` for writing, no `os.replace`, no `tempfile`. A
source scan asserts it. Each durable record needs its own exact
`PersistenceAuthorization`; an M4.3 execution approval cannot stand in for one.

Proposals are applied independently and their outcomes reported per record.
There is deliberately NO all-or-nothing multi-file transaction: a Handoff may
be WRITTEN while a Report is DENIED, and the aggregate says PARTIAL rather than
claiming success.
"""

from __future__ import annotations

from typing import Any

from ..persistence import writer as W
from . import model as M


def apply_continuation(prepared: list[M.PreparedProposal],
                       authorizations: dict[str, Any],
                       project_root: str,
                       used_bindings: tuple[str, ...] = (),
                       task_id: str = "",
                       execution_id: str = "",
                       collector: Any = None) -> M.ContinuationResult:
    """Apply each prepared proposal through the M4.4 gate. Per-record outcomes.

    `authorizations` maps a proposal id to its `PersistenceAuthorization` (or
    plain dict). A prepared proposal with no matching authorization is NOT
    written — an execution approval never substitutes for a persistence one.
    """
    from tabularium.engine.observability import Tracer
    from tabularium.engine.observability import events as EV

    exec_id = execution_id or _first_execution_id(prepared)
    tracer = Tracer(collector, exec_id, task_id)
    tracer.mark(EV.CONTINUATION_PLANNED, component=EV.CONTINUATION,
                attributes={"proposed": len(prepared),
                            "families": [p.family for p in prepared]})
    if not prepared:
        return M.ContinuationResult(
            execution_id=exec_id, task_id=task_id, status=M.NO_DURABLE_VALUE,
            reason_codes=(M.NO_DURABLE_VALUE_CODE,),
            diagnostics=({"code": M.NO_DURABLE_VALUE_CODE,
                          "detail": "the continuation plan requested no "
                                    "durable artifact"},))

    outcomes: list[M.ProposalOutcome] = []
    written_ids: list[str] = []
    consumed = list(used_bindings)

    for item in prepared:
        if not item.ok:
            outcomes.append(M.ProposalOutcome(
                family=item.family, status=M.BUILD_ERROR,
                reason_codes=(item.reason_code or M.CONTINUATION_PROPOSAL_FAILED,),
                detail=item.detail))
            continue

        proposal = item.proposal
        tracer.mark(EV.PERSISTENCE_PROPOSED, component=EV.PERSISTENCE,
                    proposal_id=proposal.proposal_id,
                    record_id=proposal.record_id,
                    attributes={"family": item.family,
                                "operation": proposal.operation,
                                "content_chars": len(proposal.content or "")})
        authorization = _match(authorizations, proposal)
        if authorization is None:
            tracer.mark(EV.PERSISTENCE_DENIED, component=EV.PERSISTENCE,
                        status=EV.ERROR, proposal_id=proposal.proposal_id,
                        record_id=proposal.record_id,
                        reason_codes=("PERSISTENCE_AUTHORIZATION_REQUIRED",))
            outcomes.append(M.ProposalOutcome(
                family=item.family, status=M.NOT_AUTHORIZED,
                record_id=proposal.record_id, target_path=proposal.target_path,
                reason_codes=("PERSISTENCE_AUTHORIZATION_REQUIRED",),
                detail="no persistence authorization supplied for this record"))
            continue

        result = W.apply_authorized_proposal(
            proposal, project_root, authorization,
            used_bindings=tuple(consumed))

        if result.written:
            # Emitted only after the M4.4 writer confirmed the write. The
            # event REPORTS the record; it does not create it.
            tracer.mark(EV.PERSISTENCE_AUTHORIZED, component=EV.PERSISTENCE,
                        proposal_id=proposal.proposal_id,
                        record_id=proposal.record_id)
            tracer.mark(EV.PERSISTENCE_WRITTEN, component=EV.PERSISTENCE,
                        proposal_id=proposal.proposal_id,
                        record_id=proposal.record_id,
                        attributes={"family": item.family,
                                    "target_path": proposal.target_path})
            written_ids.append(proposal.record_id)
            consumed.append(proposal.binding())
            outcomes.append(M.ProposalOutcome(
                family=item.family, status=M.WRITTEN,
                record_id=proposal.record_id, target_path=proposal.target_path))
        else:
            tracer.mark(EV.PERSISTENCE_DENIED if result.status == "DENIED"
                        else EV.PERSISTENCE_FAILED, component=EV.PERSISTENCE,
                        status=EV.ERROR, proposal_id=proposal.proposal_id,
                        record_id=proposal.record_id,
                        reason_codes=tuple(result.reason_codes))
            outcomes.append(M.ProposalOutcome(
                family=item.family,
                status=(M.DENIED if result.status == "DENIED" else M.FAILED),
                record_id=proposal.record_id, target_path=proposal.target_path,
                reason_codes=tuple(result.reason_codes),
                detail="; ".join(d.get("detail", "") for d in result.diagnostics)))

    return _aggregate(exec_id, task_id, outcomes, written_ids, prepared)


def _aggregate(execution_id, task_id, outcomes, written_ids,
               prepared) -> M.ContinuationResult:
    requested = [o for o in outcomes if o.status != M.NOT_REQUESTED]
    any_written = bool(written_ids)
    all_written = requested and all(o.written for o in requested)

    if all_written:
        status = M.COMPLETE
    elif any_written:
        status = M.PARTIAL
    elif any(o.status in (M.DENIED,) for o in requested):
        status = M.DENIED
    else:
        status = M.FAILED

    reason_codes: list[str] = []
    if status == M.PARTIAL:
        reason_codes.append(M.CONTINUATION_PARTIAL)
    for o in requested:
        if not o.written:
            for code in o.reason_codes:
                if code not in reason_codes:
                    reason_codes.append(code)

    next_step = _next_step(prepared)
    diagnostics = tuple({"family": o.family, "status": o.status,
                         "detail": o.detail} for o in outcomes if o.detail)

    return M.ContinuationResult(
        execution_id=execution_id, task_id=task_id, status=status,
        proposals=tuple(outcomes), written_record_ids=tuple(written_ids),
        next_step=next_step, reason_codes=tuple(reason_codes),
        diagnostics=diagnostics)


def _match(authorizations: dict[str, Any], proposal) -> Any:
    """Find the authorization for exactly this proposal, by proposal id."""
    if not isinstance(authorizations, dict):
        return None
    return authorizations.get(proposal.proposal_id)


def _next_step(prepared: list[M.PreparedProposal]) -> str:
    for item in prepared:
        if item.ok and item.family == M.HANDOFF:
            return (item.proposal.created_from or {}).get("next_step", "")
    return ""


def _first_execution_id(prepared: list[M.PreparedProposal]) -> str:
    for item in prepared:
        if item.ok:
            return item.proposal.execution_id
    return ""

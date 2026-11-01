"""Durable continuation flow (M4.5).

    COMPLETED EXECUTION
        -> explicit durable continuation mapping
        -> Handoff / Report / Task-state proposal(s)
        -> authorized persistence (via M4.4)
        -> next-load continuation evidence

Connects execution completion to durable continuation state so a later session
can load the project and see what was done, what remains, and where to
continue — WITHOUT treating raw worker output as project truth, and without any
autonomous classifier deciding a result is worth persisting.

Two explicit phases:

    build_proposals(outcome, plan, project_root)   read-only; assembles content
                                                   and allocates record ids
    apply_continuation(prepared, authorizations, …) writes ONLY through M4.4

Supported durable families are Handoff, Report, and an explicit Task state
update. Decision, Evidence, Investigation, and Question are NOT creatable here.
"""

from .flow import apply_continuation
from .model import (COMPLETE, CONTINUATION_STATUSES, DENIED, FAILED, HANDOFF,
                    NO_DURABLE_VALUE, PARTIAL, REASON_CODES, REPORT,
                    SUPPORTED_FAMILIES, TASK, ContinuationPlan,
                    ContinuationResult, ExecutionOutcome, HandoffSpec,
                    PreparedProposal, ProposalOutcome, ReportSpec,
                    TaskUpdateSpec)
from .planner import build_proposals

__all__ = ["ExecutionOutcome", "ContinuationPlan", "HandoffSpec", "ReportSpec",
           "TaskUpdateSpec", "PreparedProposal", "ProposalOutcome",
           "ContinuationResult", "build_proposals", "apply_continuation",
           "SUPPORTED_FAMILIES", "HANDOFF", "REPORT", "TASK",
           "CONTINUATION_STATUSES", "COMPLETE", "PARTIAL", "DENIED", "FAILED",
           "NO_DURABLE_VALUE", "REASON_CODES"]

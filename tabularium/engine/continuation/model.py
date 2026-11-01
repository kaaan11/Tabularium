"""Durable continuation flow — value objects (M4.5).

Connects a COMPLETED (or FAILED / REJECTED) execution to durable continuation
state, without ever treating raw worker output as project truth.

    WorkerResult
        != Handoff
        != Report
        != Task mutation
        != Decision
        != Evidence

No record family is inferred because a model "said so". The caller states,
explicitly, which durable artifacts to propose; this module deterministically
assembles their content and hands the result to the M4.4 persistence gate.
Nothing here writes to a project — see `flow.py`, whose only durable write
path is `persistence.apply_authorized_proposal`.

Normative sources (frozen, authoritative):
    tabularium/schemas/handoff.schema.yaml   tabularium/schemas/report.schema.yaml
    tabularium/schemas/task.schema.yaml
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# --- durable families M4.5 supports ----------------------------------------
#
# DELIBERATELY only these three. Decision, Evidence, Investigation, and
# Question are NOT creatable here: mapping an execution outcome to one of them
# is an explicit human/future decision, never an inference from worker output.

HANDOFF = "Handoff"
REPORT = "Report"
TASK = "Task"
SUPPORTED_FAMILIES = (HANDOFF, REPORT, TASK)

# --- continuation-result statuses (Tabularium-owned; never a WorkerResult status) -

COMPLETE = "COMPLETE"            # every requested durable write succeeded
PARTIAL = "PARTIAL"             # some requested writes succeeded, some did not
DENIED = "DENIED"              # nothing written; at least one gate denial
FAILED = "FAILED"             # nothing written; a build/authorization failure
NO_DURABLE_VALUE = "NO_DURABLE_VALUE"  # the plan requested no durable artifact

CONTINUATION_STATUSES = (COMPLETE, PARTIAL, DENIED, FAILED, NO_DURABLE_VALUE)

# --- per-proposal outcome states -------------------------------------------

WRITTEN = "WRITTEN"
NOT_REQUESTED = "NOT_REQUESTED"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
BUILD_ERROR = "BUILD_ERROR"
# plus DENIED / FAILED reused from above for the gate's own outcomes.

# --- reason codes (Tabularium-owned; M4.4 codes are reused, never re-spelled) -----

CONTINUATION_PLAN_REQUIRED = "CONTINUATION_PLAN_REQUIRED"
HANDOFF_CONTENT_REQUIRED = "HANDOFF_CONTENT_REQUIRED"
REPORT_CONTENT_REQUIRED = "REPORT_CONTENT_REQUIRED"
TASK_UPDATE_INVALID = "TASK_UPDATE_INVALID"
RECORD_ID_COLLISION = "RECORD_ID_COLLISION"
CONTINUATION_PROPOSAL_FAILED = "CONTINUATION_PROPOSAL_FAILED"
CONTINUATION_PARTIAL = "CONTINUATION_PARTIAL"
NO_DURABLE_VALUE_CODE = "NO_DURABLE_VALUE"
READBACK_FAILED = "READBACK_FAILED"

REASON_CODES = (
    CONTINUATION_PLAN_REQUIRED, HANDOFF_CONTENT_REQUIRED,
    REPORT_CONTENT_REQUIRED, TASK_UPDATE_INVALID, RECORD_ID_COLLISION,
    CONTINUATION_PROPOSAL_FAILED, CONTINUATION_PARTIAL, NO_DURABLE_VALUE_CODE,
    READBACK_FAILED)

# --- bounds -----------------------------------------------------------------
#
# A generated Handoff is a CONTINUATION SNAPSHOT, not a dump of the worker
# result. If deterministic assembly would exceed this bound, the build fails
# with HANDOFF_CONTENT_REQUIRED rather than silently truncating — the caller
# must supply a more concise summary. Schema-critical fields are never cut.

MAX_HANDOFF_BODY_CHARS = 4000


@dataclass(frozen=True)
class ExecutionOutcome:
    """The transport fields M4.5 reads from a finished execution.

    Populated explicitly by the caller from a `PipelineResult` / `RuntimeResult`.
    It is NOT the worker result and carries no authority: `output` is only the
    already-approved output, and a COMPLETED runtime status here does not, by
    itself, mean anything is worth persisting or that the Task is done.
    """

    execution_id: str
    task_id: str = ""
    worker_id: str = ""
    worker_category: str = ""
    runtime_status: str = ""
    output: str = ""

    @classmethod
    def from_pipeline_result(cls, result: dict[str, Any]) -> "ExecutionOutcome":
        """Copy transport fields from a `PipelineResult.to_dict()`.

        This moves data, it does not decide a record family. `output` is taken
        as-is (the pipeline already gates it to COMPLETED); nothing here maps
        it onto Handoff, Report, or anything else.
        """
        wr = result.get("worker_result") or {}
        return cls(
            execution_id=str(result.get("execution_id", "")),
            task_id=str(result.get("task_id", "")),
            worker_id=str(wr.get("worker_id", "")),
            worker_category=str(wr.get("category", wr.get("worker_category", ""))),
            runtime_status=str(result.get("status", "")),
            output=str(result.get("output", "")))


@dataclass(frozen=True)
class HandoffSpec:
    """Explicit, caller-supplied continuation content for one Handoff.

    Every substantive field is stated by the caller. Nothing is summarized by
    a hidden model call; the planner only arranges these strings into the
    frozen `## Current State` / `## Continuation` body.
    """

    title: str
    created_by: str
    created_at: str
    next_step: str
    what_attempted: str = ""
    what_completed: str = ""
    what_remains: str = ""
    limitations: str = ""
    intended_recipient: str = ""
    previous_handoff: str = ""
    investigations: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportSpec:
    """Explicit, caller-supplied result summary for one Report.

    Result-oriented and deliberately distinct from a Handoff: the three frozen
    sections (`## Outcome`, `## Validation`, `## Limitations`) are caller text.
    """

    title: str
    created_by: str
    created_at: str
    outcome: str
    validation: str = ""
    limitations: str = ""
    previous_report: str = ""
    investigations: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    questions: tuple[str, ...] = ()
    handoffs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskUpdateSpec:
    """An explicit Task state change.

    The caller supplies the COMPLETE replacement record content and the digest
    the update is built against. The planner invents no Task fields and does
    not derive completion from anything: a Task moves to COMPLETED / BLOCKED
    only because a human wrote a full valid record saying so and authorized the
    exact write. Worker completion never reaches this object on its own.
    """

    new_content: str
    expected_current_digest: str
    created_by: str = ""


@dataclass(frozen=True)
class ContinuationPlan:
    """What durable artifacts the caller / control plane chooses to propose.

    An empty plan is legitimate and yields NO_DURABLE_VALUE — most executions
    should persist nothing. There is deliberately no field for Decision,
    Evidence, Investigation, or Question.
    """

    handoff: HandoffSpec | None = None
    report: ReportSpec | None = None
    task_update: TaskUpdateSpec | None = None

    @property
    def requests_anything(self) -> bool:
        return any((self.handoff, self.report, self.task_update))


@dataclass(frozen=True)
class PreparedProposal:
    """One durable artifact prepared for review. READ-ONLY; nothing written.

    Either it carries a ready `PersistenceProposal` (with a content-bound
    digest a human can authorize) or it carries a build error explaining why it
    could not be prepared.
    """

    family: str
    proposal: Any = None                # persistence.PersistenceProposal | None
    reason_code: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.proposal is not None

    @property
    def content_digest(self) -> str:
        return self.proposal.content_digest if self.proposal else ""

    @property
    def binding(self) -> str:
        return self.proposal.binding() if self.proposal else ""

    def to_dict(self) -> dict[str, Any]:
        return {"family": self.family, "ok": self.ok,
                "proposal_id": self.proposal.proposal_id if self.proposal else "",
                "record_id": self.proposal.record_id if self.proposal else "",
                "target_path": self.proposal.target_path if self.proposal else "",
                "content_digest": self.content_digest,
                "binding": self.binding,
                "reason_code": self.reason_code, "detail": self.detail}


@dataclass(frozen=True)
class ProposalOutcome:
    """The result of attempting ONE durable write."""

    family: str
    status: str
    record_id: str = ""
    target_path: str = ""
    reason_codes: tuple[str, ...] = ()
    detail: str = ""

    @property
    def written(self) -> bool:
        return self.status == WRITTEN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self) | {"reason_codes": list(self.reason_codes)}


@dataclass(frozen=True)
class ContinuationResult:
    """Tabularium-owned aggregate of one continuation attempt. NOT an M1 record.

    It is operational audit — never Evidence, never written into a project —
    and it never reuses a WorkerResult status.
    """

    execution_id: str
    task_id: str
    status: str
    proposals: tuple[ProposalOutcome, ...] = ()
    written_record_ids: tuple[str, ...] = ()
    next_step: str = ""
    reason_codes: tuple[str, ...] = ()
    diagnostics: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"execution_id": self.execution_id, "task_id": self.task_id,
                "status": self.status,
                "proposals": [p.to_dict() for p in self.proposals],
                "written_record_ids": list(self.written_record_ids),
                "next_step": self.next_step,
                "reason_codes": list(self.reason_codes),
                "diagnostics": [dict(d) for d in self.diagnostics]}

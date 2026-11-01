"""Pydantic shape models for the seven frozen record families.

SHAPE ONLY. These models express what can be stated declaratively about a
record's frontmatter: which fields exist, their primitive types, closed
enumerations, list shapes, and canonical id grammar. Everything that depends
on lifecycle state, conditional presence, or Markdown body structure lives
outside these models — in `rules.py` and in the loader respectively.

Models run in STRICT mode. The frozen contracts distinguish a recorded value
from an omitted one, so implicit coercion (1 -> "1", "true" -> True) would
manufacture facts that were never recorded.

Two deliberate conventions:

* Every field carries a `None` default and is typed WITHOUT `Optional`.
  Absence is then valid (the field was not recorded) while an explicit null is
  a type error (the field was recorded as nothing). That is exactly the
  consumed boundary's distinction, and it is not what `Optional[str]` means.

* `schema_version`, `id`, `archived_outcome`, and `conclusion_status` are
  typed `Any` here. The first two are screened explicitly; the last two are
  enum-checked only inside a lifecycle branch, so a shape-layer enum check
  would report a rule that the consumed boundary does not apply.

Normative source: schemas/*.schema.yaml
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from .common import (APPROVAL_STATUS, DECISION_STATUS, EVIDENCE_RELIABILITY,
                     EVIDENCE_SOURCE_TYPE, EVIDENCE_STATUS, ID_PATTERN,
                     INVESTIGATION_STATUS, QUESTION_IMPACT, QUESTION_STATUS,
                     RISK, TASK_CREATED_BY, TASK_PRIORITY, TASK_STATUS,
                     TASK_TYPE, UNCERTAINTY)

# --- canonical id types -----------------------------------------------------


def _id_type(family: str):
    return Annotated[str, StringConstraints(pattern=ID_PATTERN[family])]


TaskId = _id_type("Task")
QuestionId = _id_type("Question")
DecisionId = _id_type("Decision")
InvestigationId = _id_type("Investigation")
EvidenceId = _id_type("Evidence")
HandoffId = _id_type("Handoff")
ReportId = _id_type("Report")

STRICT = ConfigDict(strict=True, extra="forbid", frozen=True)


class ShadowBase(BaseModel):
    model_config = STRICT


# --- field classes ----------------------------------------------------------
#
# How a shape error on this field is reported as a Tabularium reason code. Kept
# beside the models and asserted equal to the declared model fields by test,
# so the two cannot drift apart silently.

VERSION, ID, REQ_STR, OPT_STR = "VERSION", "ID", "REQ_STR", "OPT_STR"
ENUM, BOOL, STR_LIST, REF1, REFN = "ENUM", "BOOL", "STR_LIST", "REF1", "REFN"
SCOPE, RULE = "SCOPE", "RULE"


# --- Task v1.10 -------------------------------------------------------------


class ApprovedScope(ShadowBase):
    """Task v1.10 approved_scope. Every subfield is optional.

    An omitted subfield means that dimension was not bounded explicitly. It
    does NOT mean unbounded, and this model attaches no such meaning.
    """

    approved_files: list[str] = None
    approved_operations: list[str] = None
    approved_risk: Literal[RISK] = None
    approved_plan: str = None


class TaskShadow(ShadowBase):
    """Shadow of Task v1.10 (tabularium/schemas/task.schema.yaml)."""

    schema_version: Any = None
    id: Any = None
    type: Literal[TASK_TYPE] = None
    title: str = None
    objective: str = None
    priority: Literal[TASK_PRIORITY] = None
    risk: Literal[RISK] = None
    uncertainty_level: Literal[UNCERTAINTY] = None
    status: Literal[TASK_STATUS] = None
    archived_outcome: Any = None
    plan: str = None
    approved_scope: Any = None
    actual_changes: list[str] = None
    project: str = None
    parent_task: TaskId = None
    related_tasks: list[TaskId] = None
    context_items: list[str] = None
    decisions: list[DecisionId] = None
    investigations: list[InvestigationId] = None
    questions: list[QuestionId] = None
    evidence: list[EvidenceId] = None
    handoffs: list[HandoffId] = None
    reports: list[ReportId] = None
    evaluations: list[str] = None
    assigned_model: str = None
    approval_required: bool = None
    approval_status: Literal[APPROVAL_STATUS] = None
    approved_by: str = None
    approved_at: str = None
    result: str = None
    tests: list[str] = None
    report: ReportId = None
    created_at: str = None
    updated_at: str = None
    created_by: Literal[TASK_CREATED_BY] = None


TASK_FIELDS = {
    "schema_version": VERSION, "id": ID, "type": ENUM, "title": REQ_STR,
    "objective": REQ_STR, "priority": ENUM, "risk": ENUM,
    "uncertainty_level": ENUM, "status": ENUM, "archived_outcome": RULE,
    "plan": OPT_STR, "approved_scope": SCOPE, "actual_changes": STR_LIST,
    "project": OPT_STR, "parent_task": REF1, "related_tasks": REFN,
    "context_items": STR_LIST, "decisions": REFN, "investigations": REFN,
    "questions": REFN, "evidence": REFN, "handoffs": REFN, "reports": REFN,
    "evaluations": STR_LIST, "assigned_model": OPT_STR,
    "approval_required": BOOL, "approval_status": ENUM,
    "approved_by": OPT_STR, "approved_at": OPT_STR, "result": OPT_STR,
    "tests": STR_LIST, "report": REF1, "created_at": REQ_STR,
    "updated_at": OPT_STR, "created_by": ENUM,
}


# --- Question v1.0 ----------------------------------------------------------


class QuestionShadow(ShadowBase):
    """Shadow of Question v1.0 (tabularium/schemas/question.schema.yaml)."""

    schema_version: Any = None
    id: Any = None
    title: str = None
    question: str = None
    status: Literal[QUESTION_STATUS] = None
    impact: Literal[QUESTION_IMPACT] = None
    answer: str = None
    answered_by: str = None
    answered_at: str = None
    task: TaskId = None
    investigation: InvestigationId = None
    decision: DecisionId = None
    evidence: list[EvidenceId] = None
    related_questions: list[QuestionId] = None
    created_at: str = None
    updated_at: str = None
    created_by: str = None


QUESTION_FIELDS = {
    "schema_version": VERSION, "id": ID, "title": REQ_STR,
    "question": REQ_STR, "status": ENUM, "impact": ENUM, "answer": OPT_STR,
    "answered_by": OPT_STR, "answered_at": OPT_STR, "task": REF1,
    "investigation": REF1, "decision": REF1, "evidence": REFN,
    "related_questions": REFN, "created_at": REQ_STR, "updated_at": OPT_STR,
    "created_by": REQ_STR,
}


# --- Decision v1.2 ----------------------------------------------------------


class DecisionShadow(ShadowBase):
    """Shadow of Decision v1.2 (tabularium/schemas/decision.schema.yaml).

    `question` is the decision's own question statement, a plain required
    string. It is not a reference to a Question record.
    """

    schema_version: Any = None
    id: Any = None
    title: str = None
    question: str = None
    status: Literal[DECISION_STATUS] = None
    approved_by: str = None
    approved_at: str = None
    task: TaskId = None
    investigation: InvestigationId = None
    evidence: list[EvidenceId] = None
    related_decisions: list[DecisionId] = None
    supersedes: list[DecisionId] = None
    superseded_by: list[DecisionId] = None
    created_at: str = None
    updated_at: str = None
    created_by: str = None


DECISION_FIELDS = {
    "schema_version": VERSION, "id": ID, "title": REQ_STR,
    "question": REQ_STR, "status": ENUM, "approved_by": OPT_STR,
    "approved_at": OPT_STR, "task": REF1, "investigation": REF1,
    "evidence": REFN, "related_decisions": REFN, "supersedes": REFN,
    "superseded_by": REFN, "created_at": REQ_STR, "updated_at": OPT_STR,
    "created_by": REQ_STR,
}


# --- Investigation v1.4 -----------------------------------------------------


class InvestigationShadow(ShadowBase):
    """Shadow of Investigation v1.4 (tabularium/schemas/investigation.schema.yaml).

    Hypothesis fragments (`### Hn`) and `## Current conclusion` are Markdown
    body semantics owned by the loader. Nothing here parses a body.
    """

    schema_version: Any = None
    id: Any = None
    title: str = None
    objective: str = None
    status: Literal[INVESTIGATION_STATUS] = None
    conclusion_status: Any = None
    task: TaskId = None
    evidence: list[EvidenceId] = None
    supporting_evidence: list[EvidenceId] = None
    contradicting_evidence: list[EvidenceId] = None
    related_investigations: list[InvestigationId] = None
    questions: list[QuestionId] = None
    decisions: list[DecisionId] = None
    created_at: str = None
    updated_at: str = None
    created_by: str = None


INVESTIGATION_FIELDS = {
    "schema_version": VERSION, "id": ID, "title": REQ_STR,
    "objective": REQ_STR, "status": ENUM, "conclusion_status": RULE,
    "task": REF1, "evidence": REFN, "supporting_evidence": REFN,
    "contradicting_evidence": REFN, "related_investigations": REFN,
    "questions": REFN, "decisions": REFN, "created_at": REQ_STR,
    "updated_at": OPT_STR, "created_by": REQ_STR,
}


# --- Evidence v1.4 ----------------------------------------------------------


class EvidenceShadow(ShadowBase):
    """Shadow of Evidence v1.4 (tabularium/schemas/evidence.schema.yaml)."""

    schema_version: Any = None
    id: Any = None
    title: str = None
    source_type: Literal[EVIDENCE_SOURCE_TYPE] = None
    reliability: Literal[EVIDENCE_RELIABILITY] = None
    status: Literal[EVIDENCE_STATUS] = None
    observation: str = None
    source: str = None
    source_locator: str = None
    project: str = None
    task: TaskId = None
    investigation: InvestigationId = None
    decision: DecisionId = None
    questions: list[QuestionId] = None
    related_evidence: list[EvidenceId] = None
    supersedes: list[EvidenceId] = None
    superseded_by: list[EvidenceId] = None
    collected_at: str = None
    collected_by: str = None


EVIDENCE_FIELDS = {
    "schema_version": VERSION, "id": ID, "title": REQ_STR,
    "source_type": ENUM, "reliability": ENUM, "status": ENUM,
    "observation": REQ_STR, "source": OPT_STR, "source_locator": OPT_STR,
    "project": OPT_STR, "task": REF1, "investigation": REF1, "decision": REF1,
    "questions": REFN, "related_evidence": REFN, "supersedes": REFN,
    "superseded_by": REFN, "collected_at": REQ_STR, "collected_by": REQ_STR,
}


# --- Handoff v1.0 -----------------------------------------------------------


class HandoffShadow(ShadowBase):
    """Shadow of Handoff v1.0 (tabularium/schemas/handoff.schema.yaml).

    A Handoff is immutable and Task-bound: it carries no status field and no
    lifecycle, and none is invented here. Its required body sections
    (`## Current State`, `## Continuation`) are body semantics owned by the
    loader.
    """

    schema_version: Any = None
    id: Any = None
    title: str = None
    task: TaskId = None
    intended_recipient: str = None
    previous_handoff: HandoffId = None
    investigations: list[InvestigationId] = None
    decisions: list[DecisionId] = None
    evidence: list[EvidenceId] = None
    questions: list[QuestionId] = None
    created_at: str = None
    created_by: str = None


HANDOFF_FIELDS = {
    "schema_version": VERSION, "id": ID, "title": REQ_STR, "task": REF1,
    "intended_recipient": OPT_STR, "previous_handoff": REF1,
    "investigations": REFN, "decisions": REFN, "evidence": REFN,
    "questions": REFN, "created_at": REQ_STR, "created_by": REQ_STR,
}


# --- Report v1.0 ------------------------------------------------------------


class ReportShadow(ShadowBase):
    """Shadow of Report v1.0 (tabularium/schemas/report.schema.yaml).

    A Report is immutable and Task-bound: no status field, no lifecycle. Its
    required body sections (`## Outcome`, `## Validation`, `## Limitations`)
    are body semantics owned by the loader.
    """

    schema_version: Any = None
    id: Any = None
    title: str = None
    task: TaskId = None
    previous_report: ReportId = None
    investigations: list[InvestigationId] = None
    decisions: list[DecisionId] = None
    evidence: list[EvidenceId] = None
    questions: list[QuestionId] = None
    handoffs: list[HandoffId] = None
    created_at: str = None
    created_by: str = None


REPORT_FIELDS = {
    "schema_version": VERSION, "id": ID, "title": REQ_STR, "task": REF1,
    "previous_report": REF1, "investigations": REFN, "decisions": REFN,
    "evidence": REFN, "questions": REFN, "handoffs": REFN,
    "created_at": REQ_STR, "created_by": REQ_STR,
}


# --- registry ---------------------------------------------------------------

SHADOW_MODEL = {
    "Task": TaskShadow,
    "Question": QuestionShadow,
    "Decision": DecisionShadow,
    "Investigation": InvestigationShadow,
    "Evidence": EvidenceShadow,
    "Handoff": HandoffShadow,
    "Report": ReportShadow,
}

FIELD_CLASS = {
    "Task": TASK_FIELDS,
    "Question": QUESTION_FIELDS,
    "Decision": DECISION_FIELDS,
    "Investigation": INVESTIGATION_FIELDS,
    "Evidence": EVIDENCE_FIELDS,
    "Handoff": HANDOFF_FIELDS,
    "Report": REPORT_FIELDS,
}

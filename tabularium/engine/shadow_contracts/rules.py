"""Conditional and lifecycle rules for the shadow contracts.

These rules are deliberately NOT expressed as Pydantic validators. They are
status-keyed conditional-presence rules, and they must accumulate
independently of shape outcomes — a record with an unknown field still has a
reportable archival or approval violation. A model-level Pydantic validator
would short-circuit as soon as any field failed, so it would report a
different set of facts than the consumed boundary does.

Two invariants run through every rule here:

* PRESENCE IS KEY PRESENCE. `approval_required: false` is recorded, not
  absent. An empty string is recorded, not absent. Truthiness is never used
  to decide whether a field was stated.

* A MALFORMED STATUS DISABLES ITS OWN CONDITIONAL RULES. An invalid status is
  reported as an invalid enum and then treated as "no usable status", so
  status-keyed rules never run off a value the contract does not define.

What is NOT here, by design: supersession graph resolution, reciprocity
discovery, branch ambiguity, current-winner selection, body citation
traversal, hypothesis-fragment extraction, and current-conclusion extraction.
Those are M2 loader semantics, not contract shape.

Normative source: schemas/*.schema.yaml
"""

from __future__ import annotations

from .common import (ABANDONED_CONCLUSION_INVALID, ANSWER_PROVENANCE_FORBIDDEN,
                     ANSWER_PROVENANCE_REQUIRED, APPROVAL_PROVENANCE_EMPTY,
                     APPROVAL_PROVENANCE_FORBIDDEN,
                     APPROVAL_PROVENANCE_REQUIRED, APPROVAL_STATUS,
                     APPROVED_SCOPE_REQUIRES_APPROVED, ARCHIVED_OUTCOME,
                     ARCHIVED_OUTCOME_FORBIDDEN, ARCHIVED_OUTCOME_REQUIRED,
                     CONCLUSION_STATUS, CONCLUSION_STATUS_REQUIRED,
                     DECISION_PROVENANCE, DECISION_PROVENANCE_EMPTY,
                     DECISION_PROVENANCE_FORBIDDEN,
                     DECISION_PROVENANCE_REQUIRED, DECISION_STATUS,
                     ENUM_INVALID, INVESTIGATION_STATUS, QUESTION_STATUS,
                     SUPERSEDED_BY_FORBIDDEN, SUPERSEDED_BY_REQUIRED,
                     TASK_STATUS, code, is_present, nonempty_str,
                     valid_enum_value)

PROVENANCE_PAIR = ("approved_by", "approved_at")


def _provenance(codes: list, data: dict, rule: str, required_code: str,
                forbidden_code: str) -> None:
    """One provenance pair, keyed on a lifecycle status.

    The pair moves together: an approver with no time, or a time with no
    approver, is incomplete provenance in either direction.
    """
    present = [f for f in PROVENANCE_PAIR if is_present(data, f)]
    if rule == "PRESENT" and len(present) != len(PROVENANCE_PAIR):
        codes.append(required_code)
    if rule == "ABSENT" and present:
        codes.append(forbidden_code)


def _nonempty_pair(codes: list, data: dict, empty_code: str) -> None:
    for field in PROVENANCE_PAIR:
        if is_present(data, field) and not nonempty_str(data[field]):
            codes.append(code(empty_code, field))


def task(codes: list, data: dict) -> None:
    """Task v1.10 approval and archival rules."""
    status = valid_enum_value(data, "status", TASK_STATUS)
    astatus = valid_enum_value(data, "approval_status", APPROVAL_STATUS)

    # Provenance is keyed on approval_status. An ABSENT approval_status is
    # itself a state the rule covers: no approval is in force, so no approval
    # provenance may be carried. A malformed approval_status is not.
    if astatus is not None or not is_present(data, "approval_status"):
        _provenance(codes, data,
                    "PRESENT" if astatus == "APPROVED" else "ABSENT",
                    APPROVAL_PROVENANCE_REQUIRED,
                    APPROVAL_PROVENANCE_FORBIDDEN)
    if astatus == "APPROVED":
        _nonempty_pair(codes, data, APPROVAL_PROVENANCE_EMPTY)

    # approved_scope records the boundary of an approval that was granted, so
    # it cannot exist unless approval_status is APPROVED. This is presence,
    # not truthiness: an empty approved_scope mapping is still a claim that an
    # approval boundary was recorded.
    if is_present(data, "approved_scope") and astatus != "APPROVED":
        codes.append(APPROVED_SCOPE_REQUIRES_APPROVED)

    if status == "ARCHIVED":
        if not is_present(data, "archived_outcome"):
            codes.append(ARCHIVED_OUTCOME_REQUIRED)
        elif valid_enum_value(data, "archived_outcome",
                              ARCHIVED_OUTCOME) is None:
            codes.append(code(ENUM_INVALID, "archived_outcome"))
    elif status is not None and is_present(data, "archived_outcome"):
        codes.append(ARCHIVED_OUTCOME_FORBIDDEN)


def question(codes: list, data: dict) -> None:
    """Question v1.0 answer provenance."""
    status = valid_enum_value(data, "status", QUESTION_STATUS)
    provenance = ("answer", "answered_by", "answered_at")
    if status == "ANSWERED":
        for field in provenance:
            if not nonempty_str(data.get(field)):
                codes.append(code(ANSWER_PROVENANCE_REQUIRED, field))
    elif status is not None and any(is_present(data, f) for f in provenance):
        codes.append(ANSWER_PROVENANCE_FORBIDDEN)


def decision(codes: list, data: dict) -> None:
    """Decision v1.2 approval provenance (D1) and supersession fields.

    Only the FIELDS are modelled. Which Decision currently wins, whether a
    supersession is reciprocated, and how a branch ambiguity resolves are M2
    semantics and are not decided here.
    """
    status = valid_enum_value(data, "status", DECISION_STATUS)
    if status is not None:
        rule = DECISION_PROVENANCE[status]
        _provenance(codes, data, rule, DECISION_PROVENANCE_REQUIRED,
                    DECISION_PROVENANCE_FORBIDDEN)
        if rule == "PRESENT":
            _nonempty_pair(codes, data, DECISION_PROVENANCE_EMPTY)

    if status == "SUPERSEDED":
        superseded_by = data.get("superseded_by")
        if not isinstance(superseded_by, list) or not superseded_by:
            codes.append(SUPERSEDED_BY_REQUIRED)
    elif status is not None and is_present(data, "superseded_by"):
        codes.append(SUPERSEDED_BY_FORBIDDEN)


def investigation(codes: list, data: dict) -> None:
    """Investigation v1.4 conclusion rules."""
    status = valid_enum_value(data, "status", INVESTIGATION_STATUS)
    if status == "CONCLUDED":
        if not is_present(data, "conclusion_status"):
            codes.append(CONCLUSION_STATUS_REQUIRED)
        elif valid_enum_value(data, "conclusion_status",
                              CONCLUSION_STATUS) is None:
            codes.append(code(ENUM_INVALID, "conclusion_status"))
    elif status == "ABANDONED":
        if data.get("conclusion_status") != "NO_VALID_CONCLUSION":
            codes.append(ABANDONED_CONCLUSION_INVALID)
    elif status is not None and is_present(data, "conclusion_status"):
        if valid_enum_value(data, "conclusion_status",
                            CONCLUSION_STATUS) is None:
            codes.append(code(ENUM_INVALID, "conclusion_status"))


def evidence(codes: list, data: dict) -> None:
    """Evidence v1.4 carries no status-keyed conditional field.

    source_type, status, and reliability are closed enumerations checked in
    the shape layer. Supersession chain resolution is M2 semantics.
    """


def handoff(codes: list, data: dict) -> None:
    """Handoff v1.0 has no lifecycle and no conditional frontmatter field.

    Its required body sections are body semantics and are not checked here.
    """


def report(codes: list, data: dict) -> None:
    """Report v1.0 has no lifecycle and no conditional frontmatter field.

    Its required body sections are body semantics and are not checked here.
    """


RULES = {
    "Task": task,
    "Question": question,
    "Decision": decision,
    "Investigation": investigation,
    "Evidence": evidence,
    "Handoff": handoff,
    "Report": report,
}

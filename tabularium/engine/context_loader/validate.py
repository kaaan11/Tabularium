"""Consumed M1 invariant validation (F03).

Before an M1 record may participate in any semantics M2.4 consumes, enough of
its frozen contract is validated to interpret those semantics safely. This is
deliberately not a general schema engine: it checks exactly the fields, enums,
shapes, and conditional units the loader reads.

Nothing here normalizes, repairs, guesses, or migrates. An unsupported or
invalid record simply does not acquire current native semantics.
"""

from __future__ import annotations

import re
from typing import Any

from .markdown import iter_headings

SUPPORTED_VERSION = {
    "Task": "1.10", "Evidence": "1.4", "Investigation": "1.4",
    "Decision": "1.2", "Question": "1.0", "Handoff": "1.0", "Report": "1.0",
}

ID_RE = {
    "Task": re.compile(r"^TASK-[0-9]{4}-[0-9]{4}$"),
    "Question": re.compile(r"^QST-[0-9]{4}-[0-9]{4}$"),
    "Decision": re.compile(r"^DEC-[0-9]{4}-[0-9]{4}$"),
    "Investigation": re.compile(r"^INV-[0-9]{4}-[0-9]{4}$"),
    "Evidence": re.compile(r"^EVD-[0-9]{4}-[0-9]{4}$"),
    "Handoff": re.compile(r"^HND-[0-9]{4}-[0-9]{4}$"),
    "Report": re.compile(r"^RPT-[0-9]{4}-[0-9]{4}$"),
}

TASK_STATUS = ["INBOX", "ANALYZING", "PLANNED", "WAITING_APPROVAL",
               "IN_PROGRESS", "BLOCKED", "REVIEW", "COMPLETED", "CANCELLED",
               "ARCHIVED"]
APPROVAL_STATUS = ["NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED"]
ARCHIVED_OUTCOME = ["COMPLETED", "CANCELLED"]
QUESTION_STATUS = ["OPEN", "ANSWERED", "WITHDRAWN"]
QUESTION_IMPACT = ["BLOCKING", "NON_BLOCKING"]
DECISION_STATUS = ["PROPOSED", "UNDER_REVIEW", "APPROVED", "REJECTED",
                   "SUPERSEDED", "WITHDRAWN"]
EVIDENCE_STATUS = ["ACTIVE", "SUPERSEDED", "RETRACTED"]
EVIDENCE_SOURCE_TYPE = ["PRIMARY_SOURCE", "SECONDARY_SOURCE", "OBSERVATION",
                        "EXPERIMENT", "MODEL_ASSERTION", "USER_ASSERTION",
                        "INFERENCE"]
EVIDENCE_RELIABILITY = ["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
INVESTIGATION_STATUS = ["OPEN", "PAUSED", "CONCLUDED", "ABANDONED"]
CONCLUSION_STATUS = ["SUPPORTED", "INCONCLUSIVE", "NO_VALID_CONCLUSION"]
TASK_CREATED_BY = ["USER", "AGENT", "SYSTEM"]

BODY_SECTIONS = {
    "Handoff": ["Current State", "Continuation"],
    "Report": ["Outcome", "Validation", "Limitations"],
}

# Decision v1.2 D1: approval provenance keyed to lifecycle status.
DECISION_PROVENANCE = {
    "PROPOSED": "ABSENT", "UNDER_REVIEW": "ABSENT", "APPROVED": "PRESENT",
    "REJECTED": "ABSENT", "WITHDRAWN": "ABSENT", "SUPERSEDED": "PRESENT",
}

# family -> (single-id fields, list-of-id fields) consumed by the loader
SINGLE_REF = {
    "Task": {"parent_task": "Task", "report": "Report"},
    "Question": {"task": "Task", "investigation": "Investigation",
                 "decision": "Decision"},
    "Decision": {"task": "Task", "investigation": "Investigation"},
    "Investigation": {"task": "Task"},
    "Evidence": {"task": "Task", "investigation": "Investigation",
                 "decision": "Decision"},
    "Handoff": {"task": "Task", "previous_handoff": "Handoff"},
    "Report": {"task": "Task", "previous_report": "Report"},
}
LIST_REF = {
    "Task": {"related_tasks": "Task", "decisions": "Decision",
             "investigations": "Investigation", "questions": "Question",
             "evidence": "Evidence", "handoffs": "Handoff",
             "reports": "Report"},
    "Question": {"evidence": "Evidence", "related_questions": "Question"},
    "Decision": {"evidence": "Evidence", "related_decisions": "Decision",
                 "supersedes": "Decision", "superseded_by": "Decision"},
    "Investigation": {"evidence": "Evidence",
                      "supporting_evidence": "Evidence",
                      "contradicting_evidence": "Evidence",
                      "related_investigations": "Investigation",
                      "questions": "Question", "decisions": "Decision"},
    "Evidence": {"questions": "Question", "related_evidence": "Evidence",
                 "supersedes": "Evidence", "superseded_by": "Evidence"},
    "Handoff": {"investigations": "Investigation", "decisions": "Decision",
                "evidence": "Evidence", "questions": "Question"},
    "Report": {"investigations": "Investigation", "decisions": "Decision",
               "evidence": "Evidence", "questions": "Question",
               "handoffs": "Handoff"},
}

REQUIRED_STRINGS = {
    "Task": ["title", "objective", "status", "created_at", "created_by"],
    "Question": ["title", "question", "status", "impact", "created_at",
                 "created_by"],
    "Decision": ["title", "status", "question", "created_at", "created_by"],
    "Investigation": ["title", "status", "objective", "created_at",
                      "created_by"],
    "Evidence": ["title", "source_type", "status", "observation",
                 "collected_at", "collected_by"],
    "Handoff": ["title", "task", "created_at", "created_by"],
    "Report": ["title", "task", "created_at", "created_by"],
}

# Frozen frontmatter fields whose values are consumed or serialized by M2.4.
# These tables are deliberately local and version-specific: this is the narrow
# M2.4 consumer boundary, not a general schema engine or migration framework.
OPTIONAL_STRINGS = {
    "Task": {"plan", "project", "assigned_model", "result", "updated_at",
             "approved_by", "approved_at"},
    "Question": {"answer", "answered_by", "answered_at", "updated_at"},
    "Decision": {"approved_by", "approved_at", "updated_at"},
    "Investigation": {"updated_at"},
    "Evidence": {"source", "source_locator", "project"},
    "Handoff": {"intended_recipient"},
    "Report": set(),
}

PLAIN_STRING_LISTS = {
    "Task": {"actual_changes", "context_items", "evaluations", "tests"},
}

OPTIONAL_ENUMS = {
    "Task": {
        "type": ["GENERAL", "RESEARCH", "IMPLEMENTATION", "DEBUGGING",
                 "REVIEW", "ANALYSIS"],
        "priority": ["LOW", "NORMAL", "HIGH", "URGENT"],
        "risk": ["LOW", "MEDIUM", "HIGH", "CRITICAL"],
        "uncertainty_level": ["LOW", "MEDIUM", "HIGH"],
    },
}

ALLOWED_FIELDS = {
    "Task": {"schema_version", "id", "type", "title", "objective",
             "priority", "risk", "uncertainty_level", "status",
             "archived_outcome", "plan", "approved_scope", "actual_changes",
             "project", "parent_task", "related_tasks", "context_items",
             "decisions", "investigations", "questions", "evidence",
             "handoffs", "reports", "evaluations", "assigned_model",
             "approval_required", "approval_status", "approved_by",
             "approved_at", "result", "tests", "report", "created_at",
             "updated_at", "created_by"},
    "Question": {"schema_version", "id", "title", "question", "status",
                 "impact", "answer", "answered_by", "answered_at", "task",
                 "investigation", "decision", "evidence",
                 "related_questions", "created_at", "updated_at",
                 "created_by"},
    "Decision": {"schema_version", "id", "title", "question", "status",
                 "approved_by", "approved_at", "task", "investigation",
                 "evidence", "related_decisions", "supersedes",
                 "superseded_by", "created_at", "updated_at", "created_by"},
    "Investigation": {"schema_version", "id", "title", "objective",
                      "status", "conclusion_status", "task", "evidence",
                      "supporting_evidence", "contradicting_evidence",
                      "related_investigations", "questions", "decisions",
                      "created_at", "updated_at", "created_by"},
    "Evidence": {"schema_version", "id", "title", "source_type",
                 "reliability", "status", "observation", "source",
                 "source_locator", "project", "task", "investigation",
                 "decision", "questions", "related_evidence", "supersedes",
                 "superseded_by", "collected_at", "collected_by"},
    "Handoff": {"schema_version", "id", "title", "task",
                "intended_recipient", "previous_handoff", "investigations",
                "decisions", "evidence", "questions", "created_at",
                "created_by"},
    "Report": {"schema_version", "id", "title", "task", "previous_report",
               "investigations", "decisions", "evidence", "questions",
               "handoffs", "created_at", "created_by"},
}


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _enum(errors: list[str], fm: dict, field: str, allowed: list[str],
          required: bool) -> None:
    if field not in fm:
        if required:
            errors.append(f"{field} is required")
        return
    v = fm[field]
    if not isinstance(v, str) or v not in allowed:
        errors.append(f"{field} is not one of {'/'.join(allowed)}")


def _valid_enum_value(fm: dict, field: str,
                      allowed: list[str]) -> str | None:
    """Return an enum only after its primitive type and value are valid."""
    value = fm.get(field)
    return value if isinstance(value, str) and value in allowed else None


def _refs(errors: list[str], fm: dict, family: str) -> None:
    for field, target in SINGLE_REF.get(family, {}).items():
        if field not in fm:
            continue
        v = fm[field]
        if not isinstance(v, str) or not ID_RE[target].match(v):
            errors.append(f"{field} must be a single canonical {target} id")
    for field, target in LIST_REF.get(family, {}).items():
        if field not in fm:
            continue
        v = fm[field]
        if not isinstance(v, list):
            errors.append(f"{field} must be a list of canonical {target} ids")
            continue
        for entry in v:
            if not isinstance(entry, str) or not ID_RE[target].match(entry):
                errors.append(
                    f"{field} contains a value that is not a canonical "
                    f"{target} id")
                break


def _provenance(errors: list[str], fm: dict, status: Any, rule: str,
                fields: tuple[str, str], label: str) -> None:
    present = [f for f in fields if f in fm]
    if rule == "PRESENT" and len(present) != len(fields):
        errors.append(f"{label} {status} requires {' and '.join(fields)}")
    if rule == "ABSENT" and present:
        errors.append(f"{label} {status} requires {' and '.join(fields)} "
                      f"to be absent")


def _present_types(errors: list[str], fm: dict, family: str) -> None:
    for field in OPTIONAL_STRINGS.get(family, set()):
        if field in fm and not isinstance(fm[field], str):
            errors.append(f"{field} must be a string when present")
    for field in PLAIN_STRING_LISTS.get(family, set()):
        if field not in fm:
            continue
        value = fm[field]
        if not isinstance(value, list) or any(
                not isinstance(entry, str) for entry in value):
            errors.append(f"{field} must be a list of strings when present")
    for field, allowed in OPTIONAL_ENUMS.get(family, {}).items():
        _enum(errors, fm, field, allowed, required=False)


def _approved_scope(errors: list[str], fm: dict) -> None:
    if "approved_scope" not in fm:
        return
    scope = fm["approved_scope"]
    if not isinstance(scope, dict):
        errors.append("approved_scope must be an object when present")
        return
    allowed = {"approved_files", "approved_operations", "approved_risk",
               "approved_plan"}
    unknown = sorted(set(scope) - allowed)
    if unknown:
        errors.append("approved_scope contains unrecognized fields: " +
                      ", ".join(unknown))
    for field in ("approved_files", "approved_operations"):
        if field in scope and (not isinstance(scope[field], list) or any(
                not isinstance(entry, str) for entry in scope[field])):
            errors.append(f"approved_scope.{field} must be a list of strings")
    if "approved_risk" in scope and (
            not isinstance(scope["approved_risk"], str) or
            scope["approved_risk"] not in ["LOW", "MEDIUM", "HIGH",
                                            "CRITICAL"]):
        errors.append("approved_scope.approved_risk is not LOW/MEDIUM/HIGH/CRITICAL")
    if "approved_plan" in scope and not isinstance(scope["approved_plan"], str):
        errors.append("approved_scope.approved_plan must be a string")


def _level_two_sections(body: str) -> set[str]:
    """Return genuine level-two ATX headings, ignoring fenced code blocks."""
    return {text for _, level, text in iter_headings(body) if level == 2}


def validate_consumed(family: str, fm: dict, body: str = "") -> list[str]:
    """Errors preventing safe interpretation of this record's M1 semantics."""
    errors: list[str] = []

    version = fm.get("schema_version")
    supported = SUPPORTED_VERSION[family]
    if not isinstance(version, str):
        errors.append("schema_version must be an explicitly quoted string")
    elif version != supported:
        errors.append(f"schema_version {version!r} is not the supported "
                      f"{family} contract {supported!r}")

    rid = fm.get("id")
    if not isinstance(rid, str) or not ID_RE[family].match(rid):
        errors.append("id is not a canonical record id")

    for field in REQUIRED_STRINGS[family]:
        if not _nonempty_str(fm.get(field)):
            errors.append(f"{field} is required and must be a non-empty string")

    unknown = sorted(set(fm) - ALLOWED_FIELDS[family])
    if unknown:
        errors.append("unrecognized frontmatter fields: " + ", ".join(unknown))

    _present_types(errors, fm, family)
    _refs(errors, fm, family)

    if family == "Task":
        _enum(errors, fm, "status", TASK_STATUS, required=False)
        _enum(errors, fm, "created_by", TASK_CREATED_BY, required=False)
        status = _valid_enum_value(fm, "status", TASK_STATUS)
        if "approval_required" in fm and \
                not isinstance(fm["approval_required"], bool):
            errors.append("approval_required must be a boolean")
        if "approval_status" in fm:
            _enum(errors, fm, "approval_status", APPROVAL_STATUS, required=False)
        astatus = _valid_enum_value(fm, "approval_status", APPROVAL_STATUS)
        if astatus is not None or "approval_status" not in fm:
            _provenance(errors, fm, astatus,
                        "PRESENT" if astatus == "APPROVED" else "ABSENT",
                        ("approved_by", "approved_at"), "approval_status")
        if astatus == "APPROVED":
            for field in ("approved_by", "approved_at"):
                if field in fm and not _nonempty_str(fm[field]):
                    errors.append(f"approval_status APPROVED requires non-empty {field}")
        if "approved_scope" in fm and astatus != "APPROVED":
            errors.append(
                "approved_scope must be absent unless approval_status is APPROVED")
        _approved_scope(errors, fm)
        if status == "ARCHIVED":
            _enum(errors, fm, "archived_outcome", ARCHIVED_OUTCOME,
                  required=True)
        elif status is not None and "archived_outcome" in fm:
            errors.append("archived_outcome must be absent unless ARCHIVED")

    elif family == "Question":
        _enum(errors, fm, "status", QUESTION_STATUS, required=False)
        _enum(errors, fm, "impact", QUESTION_IMPACT, required=False)
        status = _valid_enum_value(fm, "status", QUESTION_STATUS)
        if status == "ANSWERED":
            for f in ("answer", "answered_by", "answered_at"):
                if not _nonempty_str(fm.get(f)):
                    errors.append(f"ANSWERED requires {f}")
        elif status is not None and any(
                f in fm for f in ("answer", "answered_by", "answered_at")):
            errors.append("answer provenance requires status ANSWERED")

    elif family == "Decision":
        _enum(errors, fm, "status", DECISION_STATUS, required=False)
        status = _valid_enum_value(fm, "status", DECISION_STATUS)
        if status is not None:
            _provenance(errors, fm, status, DECISION_PROVENANCE[status],
                        ("approved_by", "approved_at"), "status")
            if DECISION_PROVENANCE[status] == "PRESENT":
                for field in ("approved_by", "approved_at"):
                    if field in fm and not _nonempty_str(fm[field]):
                        errors.append(f"status {status} requires non-empty {field}")
        superseded_by = fm.get("superseded_by")
        if status == "SUPERSEDED":
            if not isinstance(superseded_by, list) or not superseded_by:
                errors.append("SUPERSEDED requires non-empty superseded_by")
        elif status is not None and "superseded_by" in fm:
            errors.append("superseded_by must be absent unless SUPERSEDED")

    elif family == "Evidence":
        _enum(errors, fm, "status", EVIDENCE_STATUS, required=False)
        _enum(errors, fm, "source_type", EVIDENCE_SOURCE_TYPE, required=False)
        if "reliability" in fm:
            _enum(errors, fm, "reliability", EVIDENCE_RELIABILITY,
                  required=False)

    elif family == "Investigation":
        _enum(errors, fm, "status", INVESTIGATION_STATUS, required=False)
        status = _valid_enum_value(fm, "status", INVESTIGATION_STATUS)
        if status == "CONCLUDED":
            _enum(errors, fm, "conclusion_status", CONCLUSION_STATUS,
                  required=True)
        elif status == "ABANDONED":
            if fm.get("conclusion_status") != "NO_VALID_CONCLUSION":
                errors.append(
                    "ABANDONED requires conclusion_status NO_VALID_CONCLUSION")
        elif status is not None and "conclusion_status" in fm:
            _enum(errors, fm, "conclusion_status", CONCLUSION_STATUS,
                  required=False)

    if family in BODY_SECTIONS:
        present = _level_two_sections(body)
        for section in BODY_SECTIONS[family]:
            if section not in present:
                errors.append(f"body requires exact section '## {section}'")

    return errors

"""Shared shadow vocabulary: result type, reason codes, and field classes.

Reason codes are Tabularium-owned and deterministic. They are deliberately NOT
Pydantic's human-readable error prose: Pydantic's wording is an
implementation detail of a third-party library and is not a Tabularium contract.
Only Pydantic's structured error *type* and *location* are consumed, and even
those are translated into the codes below before leaving this package.

A code has the form CATEGORY or CATEGORY:field. Codes are stable within this
shadow layer and carry no governance authority.

Normative source: schemas/*.schema.yaml
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# --- result -----------------------------------------------------------------


@dataclass(frozen=True)
class ShadowResult:
    """Deterministic outcome of one shadow validation.

    `reason_codes` is sorted and de-duplicated, so two runs over equal input
    always compare equal. An empty tuple means the record's consumed shape is
    interpretable; it does not mean the record is approved, actionable, or
    governed in any way.
    """

    family: str
    valid: bool
    reason_codes: tuple[str, ...]

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.valid


def result(family: str, codes) -> ShadowResult:
    unique = tuple(sorted(set(codes)))
    return ShadowResult(family=family, valid=not unique, reason_codes=unique)


# --- reason codes -----------------------------------------------------------

INPUT_NOT_MAPPING = "INPUT_NOT_MAPPING"

SCHEMA_VERSION_TYPE = "SCHEMA_VERSION_TYPE"
SCHEMA_VERSION_UNSUPPORTED = "SCHEMA_VERSION_UNSUPPORTED"
ID_INVALID = "ID_INVALID"

REQUIRED_STRING_INVALID = "REQUIRED_STRING_INVALID"
UNRECOGNIZED_FIELD = "UNRECOGNIZED_FIELD"
OPTIONAL_STRING_TYPE = "OPTIONAL_STRING_TYPE"
STRING_LIST_INVALID = "STRING_LIST_INVALID"
ENUM_INVALID = "ENUM_INVALID"
BOOLEAN_TYPE = "BOOLEAN_TYPE"
REF_SINGLE_INVALID = "REF_SINGLE_INVALID"
REF_LIST_TYPE = "REF_LIST_TYPE"
REF_LIST_ENTRY_INVALID = "REF_LIST_ENTRY_INVALID"

SCOPE_OBJECT_TYPE = "SCOPE_OBJECT_TYPE"
SCOPE_UNRECOGNIZED_FIELD = "SCOPE_UNRECOGNIZED_FIELD"
SCOPE_STRING_LIST_INVALID = "SCOPE_STRING_LIST_INVALID"
SCOPE_RISK_INVALID = "SCOPE_RISK_INVALID"
SCOPE_PLAN_TYPE = "SCOPE_PLAN_TYPE"

APPROVAL_PROVENANCE_REQUIRED = "APPROVAL_PROVENANCE_REQUIRED"
APPROVAL_PROVENANCE_FORBIDDEN = "APPROVAL_PROVENANCE_FORBIDDEN"
APPROVAL_PROVENANCE_EMPTY = "APPROVAL_PROVENANCE_EMPTY"
APPROVED_SCOPE_REQUIRES_APPROVED = "APPROVED_SCOPE_REQUIRES_APPROVED"
ARCHIVED_OUTCOME_REQUIRED = "ARCHIVED_OUTCOME_REQUIRED"
ARCHIVED_OUTCOME_FORBIDDEN = "ARCHIVED_OUTCOME_FORBIDDEN"

ANSWER_PROVENANCE_REQUIRED = "ANSWER_PROVENANCE_REQUIRED"
ANSWER_PROVENANCE_FORBIDDEN = "ANSWER_PROVENANCE_FORBIDDEN"

DECISION_PROVENANCE_REQUIRED = "DECISION_PROVENANCE_REQUIRED"
DECISION_PROVENANCE_FORBIDDEN = "DECISION_PROVENANCE_FORBIDDEN"
DECISION_PROVENANCE_EMPTY = "DECISION_PROVENANCE_EMPTY"
SUPERSEDED_BY_REQUIRED = "SUPERSEDED_BY_REQUIRED"
SUPERSEDED_BY_FORBIDDEN = "SUPERSEDED_BY_FORBIDDEN"

CONCLUSION_STATUS_REQUIRED = "CONCLUSION_STATUS_REQUIRED"
ABANDONED_CONCLUSION_INVALID = "ABANDONED_CONCLUSION_INVALID"

MANIFEST_ROOT_TYPE = "MANIFEST_ROOT_TYPE"
MANIFEST_UNKNOWN_KEY = "MANIFEST_UNKNOWN_KEY"
MANIFEST_MISSING_KEY = "MANIFEST_MISSING_KEY"
MANIFEST_VALUE_TYPE = "MANIFEST_VALUE_TYPE"
MANIFEST_VALUE_EMPTY = "MANIFEST_VALUE_EMPTY"
MANIFEST_VERSION_UNSUPPORTED = "MANIFEST_VERSION_UNSUPPORTED"


def code(category: str, field: str | None = None) -> str:
    """Build a deterministic reason code."""
    return category if field is None else f"{category}:{field}"


# --- canonical id patterns --------------------------------------------------
#
# Frozen id grammar, restated per family. Reference semantics themselves
# (nothing resolves a reference, references are project-local, a reference
# implies no authority) live in the frozen contracts and are not restated.

ID_PATTERN: dict[str, str] = {
    "Task": r"^TASK-[0-9]{4}-[0-9]{4}$",
    "Question": r"^QST-[0-9]{4}-[0-9]{4}$",
    "Decision": r"^DEC-[0-9]{4}-[0-9]{4}$",
    "Investigation": r"^INV-[0-9]{4}-[0-9]{4}$",
    "Evidence": r"^EVD-[0-9]{4}-[0-9]{4}$",
    "Handoff": r"^HND-[0-9]{4}-[0-9]{4}$",
    "Report": r"^RPT-[0-9]{4}-[0-9]{4}$",
}

# --- closed enumerations ----------------------------------------------------

TASK_STATUS = ("INBOX", "ANALYZING", "PLANNED", "WAITING_APPROVAL",
               "IN_PROGRESS", "BLOCKED", "REVIEW", "COMPLETED", "CANCELLED",
               "ARCHIVED")
TASK_CREATED_BY = ("USER", "AGENT", "SYSTEM")
TASK_TYPE = ("GENERAL", "RESEARCH", "IMPLEMENTATION", "DEBUGGING", "REVIEW",
             "ANALYSIS")
TASK_PRIORITY = ("LOW", "NORMAL", "HIGH", "URGENT")
RISK = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
UNCERTAINTY = ("LOW", "MEDIUM", "HIGH")
APPROVAL_STATUS = ("NOT_REQUIRED", "PENDING", "APPROVED", "REJECTED")
ARCHIVED_OUTCOME = ("COMPLETED", "CANCELLED")
QUESTION_STATUS = ("OPEN", "ANSWERED", "WITHDRAWN")
QUESTION_IMPACT = ("BLOCKING", "NON_BLOCKING")
DECISION_STATUS = ("PROPOSED", "UNDER_REVIEW", "APPROVED", "REJECTED",
                   "SUPERSEDED", "WITHDRAWN")
EVIDENCE_STATUS = ("ACTIVE", "SUPERSEDED", "RETRACTED")
EVIDENCE_SOURCE_TYPE = ("PRIMARY_SOURCE", "SECONDARY_SOURCE", "OBSERVATION",
                        "EXPERIMENT", "MODEL_ASSERTION", "USER_ASSERTION",
                        "INFERENCE")
EVIDENCE_RELIABILITY = ("LOW", "MEDIUM", "HIGH", "UNKNOWN")
INVESTIGATION_STATUS = ("OPEN", "PAUSED", "CONCLUDED", "ABANDONED")
CONCLUSION_STATUS = ("SUPPORTED", "INCONCLUSIVE", "NO_VALID_CONCLUSION")

# Decision v1.2 D1: approval provenance keyed to lifecycle status.
DECISION_PROVENANCE = {
    "PROPOSED": "ABSENT", "UNDER_REVIEW": "ABSENT", "APPROVED": "PRESENT",
    "REJECTED": "ABSENT", "WITHDRAWN": "ABSENT", "SUPERSEDED": "PRESENT",
}

# --- required non-empty strings ---------------------------------------------
#
# Requiredness in the consumed boundary means "present AND a non-empty string",
# which is narrower than Pydantic's notion of a required field. It is therefore
# screened explicitly rather than expressed as a Pydantic required field.

REQUIRED_STRINGS: dict[str, tuple[str, ...]] = {
    "Task": ("title", "objective", "status", "created_at", "created_by"),
    "Question": ("title", "question", "status", "impact", "created_at",
                 "created_by"),
    "Decision": ("title", "status", "question", "created_at", "created_by"),
    "Investigation": ("title", "status", "objective", "created_at",
                      "created_by"),
    "Evidence": ("title", "source_type", "status", "observation",
                 "collected_at", "collected_by"),
    "Handoff": ("title", "task", "created_at", "created_by"),
    "Report": ("title", "task", "created_at", "created_by"),
}

# --- reference fields -------------------------------------------------------

SINGLE_REF: dict[str, dict[str, str]] = {
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

LIST_REF: dict[str, dict[str, str]] = {
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


# --- helpers ----------------------------------------------------------------


def is_present(data: dict, field: str) -> bool:
    """Presence is key presence. Never truthiness.

    An explicit null, an empty string, and `false` are all PRESENT. The frozen
    contracts distinguish "omitted / not recorded" from "recorded as empty",
    so a truthiness test would silently collapse two different facts.
    """
    return field in data


def nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def valid_enum_value(data: dict, field: str, allowed) -> str | None:
    """Return the enum value only once its primitive type AND value are valid.

    Status-dependent rules must never run off a malformed status: an invalid
    primitive is reported as an invalid enum and then treated as "no usable
    status", exactly as the consumed boundary requires.
    """
    value = data.get(field)
    return value if isinstance(value, str) and value in allowed else None

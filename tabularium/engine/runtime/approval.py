"""Canonical human approval: resolution, scope, and validation (M4.3).

A RUNTIME INTERRUPT IS NOT APPROVAL. A RUNTIME RESUME IS NOT APPROVAL.

Approval is a HUMAN ACT with provenance. Continuing execution requires a
`HumanApprovalResolution` that says who decided, what they decided, when, and
over exactly what scope — bound to the one pending request. Nothing else
satisfies the gate: not a bare `"APPROVE"`, not `True`, not a dict without
provenance, not worker output, not skill text, not a LangGraph resume value.

COMPATIBLE WITH FROZEN M1 MEANING, WITHOUT TOUCHING M1

The frozen Task contract already defines what an approval means:
`approved_by` and `approved_at` are ONE PROVENANCE PAIR that moves together —
"an approver with no time, or a time with no approver, is incomplete
provenance" — `approved_scope` bounds what was authorized, and timestamps are
ISO 8601 with an explicit UTC offset. This module reuses that MEANING for
execution authority. It writes nothing to a Task, creates no record, and keeps
no approval ledger; M4.4 owns persistence.

EXECUTION-CANONICAL, NOT PROJECT-PERSISTED

A validated resolution is canonical for THIS EXECUTION's authority and lives
in the runtime checkpoint. It is not durable project truth.

ACTOR TRUST BOUNDARY

`decided_by` is an opaque, caller-supplied actor reference, exactly as
`approved_by` is opaque in M1: "no identity system exists, and none is defined
here". M4.3 introduces no authentication, no directory, and no RBAC. It
preserves the identity it was given, verbatim, and records that it was
trusted. Establishing that identity belongs to a later UX/auth layer.

"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# --- decision vocabulary ----------------------------------------------------
#
# Deliberately two values. Cancellation is a RUNTIME action (`runtime.cancel`),
# not a human approval decision, so it is not in this vocabulary: conflating
# "the human declined" with "the operator stopped it" would blur who decided.

APPROVE = "APPROVE"
REJECT = "REJECT"
DECISIONS = (APPROVE, REJECT)

# --- scope ------------------------------------------------------------------
#
# Shaped after the frozen Task `approved_scope`, deliberately not a policy
# language. There is NO wildcard grant in v0.1: an approval always names what
# it covers.

SCOPE_FIELDS = ("approved_files", "approved_operations", "approved_plan")
RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}

# --- reason codes -----------------------------------------------------------

APPROVAL_REQUEST_NOT_FOUND = "APPROVAL_REQUEST_NOT_FOUND"
APPROVAL_REQUEST_MISMATCH = "APPROVAL_REQUEST_MISMATCH"
APPROVAL_EXECUTION_MISMATCH = "APPROVAL_EXECUTION_MISMATCH"
APPROVAL_TASK_MISMATCH = "APPROVAL_TASK_MISMATCH"
APPROVAL_SCOPE_MISMATCH = "APPROVAL_SCOPE_MISMATCH"
APPROVAL_DECISION_INVALID = "APPROVAL_DECISION_INVALID"
APPROVAL_ACTOR_REQUIRED = "APPROVAL_ACTOR_REQUIRED"
APPROVAL_TIMESTAMP_INVALID = "APPROVAL_TIMESTAMP_INVALID"
APPROVAL_ALREADY_RESOLVED = "APPROVAL_ALREADY_RESOLVED"
APPROVAL_RESOLUTION_MALFORMED = "APPROVAL_RESOLUTION_MALFORMED"

APPROVAL_REASON_CODES = (
    APPROVAL_REQUEST_NOT_FOUND, APPROVAL_REQUEST_MISMATCH,
    APPROVAL_EXECUTION_MISMATCH, APPROVAL_TASK_MISMATCH,
    APPROVAL_SCOPE_MISMATCH, APPROVAL_DECISION_INVALID,
    APPROVAL_ACTOR_REQUIRED, APPROVAL_TIMESTAMP_INVALID,
    APPROVAL_ALREADY_RESOLVED, APPROVAL_RESOLUTION_MALFORMED)

# ISO 8601 with an EXPLICIT offset, matching the frozen Task convention. A
# naive timestamp is refused rather than reinterpreted as local time.
_OFFSET = re.compile(r"(Z|[+-][0-9]{2}:?[0-9]{2})$")


@dataclass(frozen=True)
class ApprovalScope:
    """What an approval covers. Never a global grant.

    Mirrors the frozen `approved_scope` subfields. An omitted dimension means
    that dimension was not bounded explicitly — it does NOT mean unbounded,
    which is why `covers` treats an omitted dimension on the REQUEST side as
    "nothing requested" and an omitted dimension on the RESOLUTION side as
    "nothing granted".
    """

    approved_files: tuple[str, ...] = ()
    approved_operations: tuple[str, ...] = ()
    approved_risk: str | None = None
    approved_plan: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"approved_files": list(self.approved_files),
                "approved_operations": list(self.approved_operations),
                "approved_risk": self.approved_risk,
                "approved_plan": self.approved_plan}

    @classmethod
    def from_dict(cls, data: Any) -> "ApprovalScope":
        if not isinstance(data, dict):
            return cls()
        return cls(
            approved_files=tuple(data.get("approved_files") or ()),
            approved_operations=tuple(data.get("approved_operations") or ()),
            approved_risk=data.get("approved_risk"),
            approved_plan=data.get("approved_plan") or "")

    def covers(self, requested: "ApprovalScope") -> bool:
        """Whether THIS granted scope covers everything requested.

        A resolution may narrow an approval but never widen it. Granting more
        than was asked is refused too: an approval that silently broadens its
        own reach is the failure mode this check exists to prevent.
        """
        if not set(requested.approved_files).issubset(self.approved_files):
            return False
        if not set(requested.approved_operations).issubset(
                self.approved_operations):
            return False
        # Granting a HIGHER risk ceiling than requested is widening.
        if self.approved_risk is not None:
            if requested.approved_risk is None:
                return False
            if RISK_RANK.get(self.approved_risk, 99) > \
                    RISK_RANK.get(requested.approved_risk, -1):
                return False
        return True

    def within(self, requested: "ApprovalScope") -> bool:
        """Whether this scope is equal to or narrower than the request."""
        if not set(self.approved_files).issubset(requested.approved_files):
            return False
        if not set(self.approved_operations).issubset(
                requested.approved_operations):
            return False
        if self.approved_risk is not None:
            if requested.approved_risk is None:
                return False
            if RISK_RANK.get(self.approved_risk, 99) > \
                    RISK_RANK.get(requested.approved_risk, -1):
                return False
        return True


@dataclass(frozen=True)
class HumanApprovalResolution:
    """One human decision, with the provenance that makes it accountable.

    `decided_by` and `decided_at` form ONE PROVENANCE PAIR, exactly as the
    frozen Task contract requires: an approver with no time, or a time with no
    approver, is incomplete provenance and is refused.

    Constructing one does not approve anything. It must still be VALIDATED
    against the pending request.
    """

    approval_request_id: str
    execution_id: str
    decision: str
    decided_by: str
    decided_at: str
    task_id: str = ""
    scope: ApprovalScope = field(default_factory=ApprovalScope)
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["scope"] = self.scope.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "HumanApprovalResolution | None":
        """Rebuild from plain data, or None if it is not one.

        Deliberately strict: anything that is not a mapping carrying the
        required provenance keys is NOT a resolution. This is what makes
        `resume(True)` and `resume("APPROVE")` structurally incapable of
        approving.
        """
        if not isinstance(data, dict):
            return None
        required = ("approval_request_id", "execution_id", "decision",
                    "decided_by", "decided_at")
        if not all(key in data for key in required):
            return None
        return cls(
            approval_request_id=str(data["approval_request_id"]),
            execution_id=str(data["execution_id"]),
            decision=str(data["decision"]),
            decided_by=str(data["decided_by"]),
            decided_at=str(data["decided_at"]),
            task_id=str(data.get("task_id") or ""),
            scope=ApprovalScope.from_dict(data.get("scope")),
            reason=str(data.get("reason") or ""))


@dataclass(frozen=True)
class ApprovalVerdict:
    """The deterministic outcome of validating a resolution."""

    valid: bool
    reason_codes: tuple[str, ...] = ()
    resolution: HumanApprovalResolution | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"valid": self.valid,
                "reason_codes": list(self.reason_codes),
                "resolution": self.resolution.to_dict()
                if self.resolution else None}


def valid_timestamp(value: Any) -> bool:
    """ISO 8601 with an EXPLICIT UTC offset, per the frozen convention.

    A naive timestamp is refused rather than assumed to be local or UTC:
    guessing a timezone would silently invent part of the provenance.
    """
    if not isinstance(value, str) or not _OFFSET.search(value.strip()):
        return False
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def validate(resolution: Any, request: Any, execution_id: str,
             task_id: str = "", resolved_request_ids: tuple[str, ...] = ()
             ) -> ApprovalVerdict:
    """Validate a human resolution against the ONE pending request.

    Fails closed on every ambiguity. Returns Tabularium-owned reason codes; no
    library or framework prose ever becomes the error identity.
    """
    parsed = (resolution if isinstance(resolution, HumanApprovalResolution)
              else HumanApprovalResolution.from_dict(resolution))
    if parsed is None:
        # A bare string, a bool, or a dict without provenance is not a
        # resolution at all — this is the structural reason a resume value
        # cannot approve anything.
        return ApprovalVerdict(False, (APPROVAL_RESOLUTION_MALFORMED,))

    codes: list[str] = []

    if request is None:
        codes.append(APPROVAL_REQUEST_NOT_FOUND)
    else:
        pending_id = getattr(request, "request_id", "") or ""
        if parsed.approval_request_id != pending_id:
            # Binding to the exact request is what stops a stale or
            # copy-pasted approval from satisfying a different one.
            codes.append(APPROVAL_REQUEST_MISMATCH)
        requested_scope = _request_scope(request)
        if not parsed.scope.within(requested_scope):
            codes.append(APPROVAL_SCOPE_MISMATCH)

    if parsed.approval_request_id in tuple(resolved_request_ids):
        codes.append(APPROVAL_ALREADY_RESOLVED)

    if parsed.execution_id != execution_id:
        codes.append(APPROVAL_EXECUTION_MISMATCH)
    if task_id and parsed.task_id and parsed.task_id != task_id:
        codes.append(APPROVAL_TASK_MISMATCH)
    if parsed.decision not in DECISIONS:
        codes.append(APPROVAL_DECISION_INVALID)
    if not parsed.decided_by or not parsed.decided_by.strip():
        # An unattributed approval is indistinguishable from an agent
        # approving its own recommendation (immutable principle 7).
        codes.append(APPROVAL_ACTOR_REQUIRED)
    if not valid_timestamp(parsed.decided_at):
        codes.append(APPROVAL_TIMESTAMP_INVALID)

    if codes:
        return ApprovalVerdict(False, tuple(dict.fromkeys(codes)))
    return ApprovalVerdict(True, (), parsed)


def _request_scope(request: Any) -> ApprovalScope:
    detail = getattr(request, "detail", None) or {}
    return ApprovalScope.from_dict(detail.get("scope"))

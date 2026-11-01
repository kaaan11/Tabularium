"""Data models and reason codes for the policy enforcement engine (Step 5).

Enforces approved principles without inventing unapproved scoring or automation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- Policy Reason Codes ---
RISK_APPROVAL_REQUIRED = "RISK_APPROVAL_REQUIRED"
SCOPE_RISK_EXCEEDED = "SCOPE_RISK_EXCEEDED"
MODEL_ASSIGNMENT_MISMATCH = "MODEL_ASSIGNMENT_MISMATCH"
SECRET_DETECTED_IN_CONTEXT = "SECRET_DETECTED_IN_CONTEXT"
POLICY_INPUT_INVALID = "POLICY_INPUT_INVALID"

POLICY_REASON_CODES = (
    RISK_APPROVAL_REQUIRED,
    SCOPE_RISK_EXCEEDED,
    MODEL_ASSIGNMENT_MISMATCH,
    SECRET_DETECTED_IN_CONTEXT,
    POLICY_INPUT_INVALID,
)

RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
RISK_RANK = {level: index for index, level in enumerate(RISK_LEVELS)}


@dataclass(frozen=True)
class PolicyViolation:
    """One concrete policy violation."""

    policy: str
    code: str
    detail: str


@dataclass(frozen=True)
class PolicyVerdict:
    """The aggregate verdict returned by the policy engine."""

    allowed: bool
    requires_approval: bool = False
    approval_reasons: tuple[str, ...] = ()
    violations: tuple[PolicyViolation, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def reason_codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)

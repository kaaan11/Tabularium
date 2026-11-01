"""Persistence gate and durable-value proposal flow (M4.4).

THE BOUNDARY BETWEEN EXECUTION OUTPUT AND PROJECT TRUTH.

    EXECUTION RESULT != PROJECT TRUTH
    HUMAN APPROVAL OF EXECUTION != AUTHORIZATION TO WRITE PROJECT STATE

A durable write happens only through `apply_authorized_proposal`, and only
when a proposal, a passing nine-condition gate evaluation, and a matching
human authorization are all present. Everything else in this package is
read-only.

The nine conditions are not reinterpreted here.

What M4.4 does NOT do: decide that any execution result is worth persisting.
Proposals are constructed explicitly by the caller; mapping an outcome to a
record family is M4.5's concern.
"""

from .gate import CONDITION_NAMES, evaluate, validate_record
from .model import (CREATE, DENIED, FAILED, FAMILIES, NONE, OPERATIONS,
                    PROPOSAL_READY, PROPOSE, REASON_CODES, RESULT_STATUSES,
                    UPDATE, WRITTEN, ConditionResult, GateEvaluation,
                    PersistenceAuthorization, PersistenceProposal,
                    PersistenceResult, contains_secret, digest,
                    valid_timestamp)
from .writer import apply_authorized_proposal, evaluate_only

__all__ = ["PersistenceProposal", "PersistenceAuthorization",
           "PersistenceResult", "GateEvaluation", "ConditionResult",
           "evaluate", "evaluate_only", "apply_authorized_proposal",
           "validate_record", "digest", "valid_timestamp", "contains_secret",
           "CONDITION_NAMES", "FAMILIES", "OPERATIONS", "CREATE", "UPDATE",
           "NONE", "PROPOSE", "PROPOSAL_READY", "DENIED", "WRITTEN", "FAILED",
           "RESULT_STATUSES", "REASON_CODES"]

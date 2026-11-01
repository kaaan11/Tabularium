"""Experimental single-file patch gate, not wired to the product CLI.

One authorized text update/creation, verification on a full project copy,
then one atomic replacement. Multi-file patches and deletions are refused.
Test commands run under bubblewrap and are refused without it; a custom
verifier is trusted code and keeps the operator's privileges.
Requires exclusive project access; no crash-safe multi-file rollback is claimed.
"""

from .model import (
    APPLIED,
    DENIED,
    DIFF_EMPTY,
    DIFF_SYNTAX_INVALID,
    FilePatch,
    GATE_FAILED,
    GateConditionResult,
    ISOLATED_TESTS_FAILED,
    PATCH_ACTOR_REQUIRED,
    PATCH_APPLY_FAILED,
    PATCH_SCOPE_UNSUPPORTED,
    VERIFICATION_REQUIRED,
    TARGET_CHANGED,
    SANDBOX_TREE_UNSUPPORTED,
    SANDBOX_UNAVAILABLE,
    PATCH_AUTHORIZATION_MISMATCH,
    PATCH_AUTHORIZATION_REQUIRED,
    PATCH_STATUSES,
    PATCH_TIMESTAMP_INVALID,
    PROPOSAL_READY,
    PROTECTED_TARGET_DENIED,
    PatchAuthorization,
    PatchGateEvaluation,
    PatchProposal,
    PatchResult,
    REASON_CODES,
    ROLLED_BACK,
    SECRET_MATERIAL_PRESENT,
    TARGET_OUTSIDE_PROJECT,
    TESTS_FAILED,
    digest,
)
from .parser import apply_patch_to_text, parse_unified_diff
from .gate import evaluate_patch
from .sandbox import apply_patch_transactional

__all__ = [
    "PatchProposal",
    "PatchAuthorization",
    "PatchResult",
    "PatchGateEvaluation",
    "GateConditionResult",
    "FilePatch",
    "parse_unified_diff",
    "apply_patch_to_text",
    "evaluate_patch",
    "apply_patch_transactional",
    "digest",
    "PROPOSAL_READY",
    "DENIED",
    "TESTS_FAILED",
    "APPLIED",
    "ROLLED_BACK",
    "PATCH_STATUSES",
    "REASON_CODES",
    "DIFF_EMPTY",
    "DIFF_SYNTAX_INVALID",
    "TARGET_OUTSIDE_PROJECT",
    "PROTECTED_TARGET_DENIED",
    "SECRET_MATERIAL_PRESENT",
    "PATCH_AUTHORIZATION_REQUIRED",
    "PATCH_AUTHORIZATION_MISMATCH",
    "PATCH_ACTOR_REQUIRED",
    "PATCH_TIMESTAMP_INVALID",
    "ISOLATED_TESTS_FAILED",
    "PATCH_APPLY_FAILED",
    "PATCH_SCOPE_UNSUPPORTED",
    "VERIFICATION_REQUIRED",
    "TARGET_CHANGED",
    "SANDBOX_UNAVAILABLE",
    "SANDBOX_TREE_UNSUPPORTED",
    "GATE_FAILED",
]

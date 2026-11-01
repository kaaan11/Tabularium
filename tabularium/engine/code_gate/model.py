"""Data models and reason codes for the experimental single-file write gate.

Protects the project codebase from arbitrary or unvalidated model modifications:
1. Unified Diff / Patch proposals are evaluated before any file mutation occurs.
2. Evaluates security rules: path confinement, protected paths, and secret scanning.
3. Requires explicit human PatchAuthorization bound to the SHA-256 digest of the diff.
4. Requires trusted verification in a scratch directory before one atomic replacement.
   This is not OS isolation or a multi-file transaction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# --- Result Statuses ---
PROPOSAL_READY = "PROPOSAL_READY"
DENIED = "DENIED"
TESTS_FAILED = "TESTS_FAILED"
APPLIED = "APPLIED"
ROLLED_BACK = "ROLLED_BACK"

PATCH_STATUSES = (PROPOSAL_READY, DENIED, TESTS_FAILED, APPLIED, ROLLED_BACK)

# --- Reason Codes ---
DIFF_EMPTY = "DIFF_EMPTY"
DIFF_SYNTAX_INVALID = "DIFF_SYNTAX_INVALID"
TARGET_OUTSIDE_PROJECT = "TARGET_OUTSIDE_PROJECT"
PROTECTED_TARGET_DENIED = "PROTECTED_TARGET_DENIED"
SECRET_MATERIAL_PRESENT = "SECRET_MATERIAL_PRESENT"
PATCH_AUTHORIZATION_REQUIRED = "PATCH_AUTHORIZATION_REQUIRED"
PATCH_AUTHORIZATION_MISMATCH = "PATCH_AUTHORIZATION_MISMATCH"
PATCH_ACTOR_REQUIRED = "PATCH_ACTOR_REQUIRED"
PATCH_TIMESTAMP_INVALID = "PATCH_TIMESTAMP_INVALID"
ISOLATED_TESTS_FAILED = "ISOLATED_TESTS_FAILED"
PATCH_APPLY_FAILED = "PATCH_APPLY_FAILED"
GATE_FAILED = "GATE_FAILED"
VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
PATCH_SCOPE_UNSUPPORTED = "PATCH_SCOPE_UNSUPPORTED"
TARGET_CHANGED = "TARGET_CHANGED"
SANDBOX_UNAVAILABLE = "SANDBOX_UNAVAILABLE"
SANDBOX_TREE_UNSUPPORTED = "SANDBOX_TREE_UNSUPPORTED"

REASON_CODES = (
    DIFF_EMPTY,
    DIFF_SYNTAX_INVALID,
    TARGET_OUTSIDE_PROJECT,
    PROTECTED_TARGET_DENIED,
    SECRET_MATERIAL_PRESENT,
    PATCH_AUTHORIZATION_REQUIRED,
    PATCH_AUTHORIZATION_MISMATCH,
    PATCH_ACTOR_REQUIRED,
    PATCH_TIMESTAMP_INVALID,
    ISOLATED_TESTS_FAILED,
    PATCH_APPLY_FAILED,
    GATE_FAILED,
    VERIFICATION_REQUIRED,
    PATCH_SCOPE_UNSUPPORTED,
    TARGET_CHANGED,
    SANDBOX_UNAVAILABLE,
    SANDBOX_TREE_UNSUPPORTED,
)


def digest(text: str) -> str:
    """SHA-256 digest over the canonical UTF-8 bytes of the diff."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FilePatch:
    """Parsed modification for one relative file path."""

    source_path: str       # e.g. a/scripts/foo.py or /dev/null
    target_path: str       # e.g. b/scripts/foo.py
    normalized_path: str   # e.g. scripts/foo.py
    added_lines: tuple[str, ...] = ()
    removed_lines: tuple[str, ...] = ()
    hunks: tuple[str, ...] = ()
    is_creation: bool = False
    is_deletion: bool = False


@dataclass(frozen=True)
class PatchProposal:
    """A PROPOSED code change represented as a unified diff."""

    proposal_id: str
    execution_id: str
    raw_diff: str
    target_files: tuple[str, ...]
    task_id: str = ""
    reason: str = ""

    @property
    def diff_digest(self) -> str:
        return digest(self.raw_diff)

    def binding(self) -> str:
        """Deterministic identity for authorization binding."""
        return digest(json.dumps({
            "proposal_id": self.proposal_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "diff_digest": self.diff_digest,
            "target_files": list(self.target_files),
        }, sort_keys=True))

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "execution_id": self.execution_id,
            "task_id": self.task_id,
            "reason": self.reason,
            "diff_digest": self.diff_digest,
            "target_files": list(self.target_files),
            "binding": self.binding(),
        }


@dataclass(frozen=True)
class PatchAuthorization:
    """Explicit human authorization to apply one exact patch proposal."""

    proposal_id: str
    execution_id: str
    diff_digest: str
    authorized_by: str
    authorized_at: str
    binding: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "PatchAuthorization | None":
        if not isinstance(data, dict):
            return None
        required = ("proposal_id", "execution_id", "diff_digest", "authorized_by", "authorized_at")
        if not all(key in data for key in required):
            return None
        return cls(**{key: str(data.get(key) or "") for key in required + ("binding", "note")})


@dataclass(frozen=True)
class GateConditionResult:
    name: str
    passed: bool
    reason_code: str = ""
    detail: str = ""


@dataclass(frozen=True)
class PatchGateEvaluation:
    allowed: bool
    conditions: tuple[GateConditionResult, ...] = ()
    reason_codes: tuple[str, ...] = ()

    @property
    def failed(self) -> tuple[GateConditionResult, ...]:
        return tuple(c for c in self.conditions if not c.passed)


@dataclass(frozen=True)
class PatchResult:
    status: str
    proposal_id: str = ""
    reason_codes: tuple[str, ...] = ()
    modified_files: tuple[str, ...] = ()
    authorized_by: str = ""
    authorized_at: str = ""
    resulting_digest: str = ""
    diagnostics: tuple[dict[str, str], ...] = ()
    test_output: str = ""

    @property
    def applied(self) -> bool:
        return self.status == APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "proposal_id": self.proposal_id,
            "reason_codes": list(self.reason_codes),
            "modified_files": list(self.modified_files),
            "authorized_by": self.authorized_by,
            "authorized_at": self.authorized_at,
            "resulting_digest": self.resulting_digest,
            "diagnostics": [dict(d) for d in self.diagnostics],
            "test_output": self.test_output,
        }
